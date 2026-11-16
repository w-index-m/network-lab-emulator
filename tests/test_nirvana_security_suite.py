"""
ニルヴァーナの A〜E(NIRVANA改 / NIRVANA-R / NICTER の要素)

B 警報からの遮断・隔離: access-class で本物の SSH を拒否 / Null0 / 隣の装置の shutdown / 自動対処 / 解除
C ATT&CK: 総当たり → 正規アカウント → 横展開 → 持ち出し と段階が進む、表と進行度
E ネクサス: 2拠点の同じ攻撃元を中央が見つけて返す、拠点側の送信
A 回線チェック: BAS の混雑で PPPoE だけ遅くなる(IPoE は影響なし)、注
D NetFlow v5 / sFlow v5 の解析と本物の UDP 受信、他社セキュリティ製品の syslog、メール通知(本物の SMTP 会話)、pcap
"""
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as app_module  # noqa: E402
from engine import attack_map, capture, flow_collector, nexus, sensor_syslog, wan_watch  # noqa: E402
from engine.darknet import darknet  # noqa: E402
from engine.response import store  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def _dev(c, dev, typ, host):
    c.post('/api/device', json={'id': dev, 'type': typ, 'hostname': host})


def _addr(c, dev, ifn, ip, mask='255.255.255.0'):
    _cli(c, dev, 'configure terminal', f'interface {ifn}', f'ip address {ip} {mask}', 'no shutdown', 'end')


def _alert(rule, src, dst, detail='x'):
    a = app_module._flow_recorder.external_alert(rule, f'{src}-{uuid.uuid4().hex[:4]}', src, dst, detail)
    assert a is not None
    return a


# ── B ──────────────────────────────────────────────
class TestResponse:
    def test_vty_access_class_really_refuses_ssh_and_undo(self, client):
        sw = f'rs-{_RUN}'
        _dev(client, sw, 'catalyst', 'RS-SW')
        _addr(client, sw, 'Vlan10', '10.246.1.1')
        _cli(client, sw, 'configure terminal', 'crypto key generate rsa modulus 2048', 'end')
        st = app_module.device_sessions[sw]
        from engine import ssh_cli_agent
        assert ssh_cli_agent.ensure_ssh_cli_agent(sw, app_module.device_sessions, app_module._run_cli_sync), 'SSH リスナーが起動しない'
        srv = ssh_cli_agent._servers[sw]
        host, port = srv.ip, srv.port

        def banner(src):
            s = socket.socket()
            s.bind((src, 0))
            s.settimeout(3)
            s.connect((host, port))
            try:
                return s.recv(64)
            except (socket.timeout, ConnectionResetError):
                return b''
            finally:
                s.close()
        assert banner('127.0.0.66').startswith(b'SSH-2.0')
        a = _alert('conn_scan', '127.0.0.66', sw)
        plan = client.get(f'/api/response/plan?seq={a["seq"]}').json()
        acts = {x['kind']: x for x in plan['actions']}
        assert 'vty_block' in acts and 'blackhole' in acts
        rec = client.post('/api/response/run', json={'seq': a['seq'], 'action': acts['vty_block']['id']}).json()
        assert rec['status'] == 'active' and rec['target'] == '127.0.0.66'
        rc = _cli(client, sw, 'show running-config')
        assert 'access-class NIRVANA-BLOCK in' in rc
        assert getattr(st, 'line_vty_access_class') == 'NIRVANA-BLOCK'
        assert banner('127.0.0.66') == b''                     # 本当に切られる
        assert banner('127.0.0.67').startswith(b'SSH-2.0')     # ほかの送信元は通る
        undo = client.post('/api/response/undo', json={'id': rec['id']}).json()
        assert undo['status'] == 'undone'
        assert banner('127.0.0.66').startswith(b'SSH-2.0')

    def test_blackhole_and_isolate(self, client):
        r1, r2 = f'rb1-{_RUN}', f'rb2-{_RUN}'
        _dev(client, r1, 'cisco', 'RB1')
        _dev(client, r2, 'cisco', 'RB2')
        client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
        _addr(client, r1, 'GigabitEthernet0/0/0', '10.246.2.1')
        _addr(client, r2, 'GigabitEthernet0/0/0', '10.246.2.2')
        assert '!!!!!' in _cli(client, r1, 'ping 10.246.2.2')
        # 隔離: r2 を疑い → r1 の r2 向き IF を shutdown
        a = _alert('scan', r2, None)
        plan = client.get(f'/api/response/plan?seq={a["seq"]}').json()
        iso = next(x for x in plan['actions'] if x['kind'] == 'isolate')
        rec = client.post('/api/response/run', json={'seq': a['seq'], 'action': iso['id']}).json()
        assert '!!!!!' not in _cli(client, r1, 'ping 10.246.2.2')
        client.post('/api/response/undo', json={'id': rec['id']})
        assert '!!!!!' in _cli(client, r1, 'ping 10.246.2.2')
        # ブラックホール: 宛先を Null0 に
        _cli(client, r1, 'configure terminal', 'ip route 10.246.2.2 255.255.255.255 Null0', 'end')
        assert '!!!!!' not in _cli(client, r1, 'ping 10.246.2.2')
        _cli(client, r1, 'configure terminal', 'no ip route 10.246.2.2 255.255.255.255 Null0', 'end')
        assert '!!!!!' in _cli(client, r1, 'ping 10.246.2.2')

    def test_auto_rule(self, client):
        r1, r2 = f'ra1-{_RUN}', f'ra2-{_RUN}'
        _dev(client, r1, 'cisco', 'RA1')
        _dev(client, r2, 'cisco', 'RA2')
        client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
        _addr(client, r1, 'GigabitEthernet0/0/0', '10.246.3.1')
        _addr(client, r2, 'GigabitEthernet0/0/0', '10.246.3.2')
        assert client.post('/api/response/auto', json={'rules': [{'rule': 'x', 'action': 'bad'}]}).status_code == 400
        client.post('/api/response/auto', json={'rules': [{'rule': 'unreach_burst', 'action': 'isolate'}]})
        try:
            _alert('unreach_burst', r2, None)
            import asyncio
            asyncio.run(app_module._response_auto_tick())
            recs = [r for r in store.records if r['auto'] and r['target'] == r2]
            assert recs and recs[-1]['status'] == 'active'
            assert '!!!!!' not in _cli(client, r1, 'ping 10.246.3.2')
            client.post('/api/response/undo', json={'id': recs[-1]['id']})
        finally:
            client.post('/api/response/auto', json={'rules': []})


# ── C ──────────────────────────────────────────────
class TestAttack:
    def test_chain_progresses_to_exfiltration(self):
        t = attack_map.AttackTracker(org='LAB')
        t.record('conn_scan', '203.0.113.9', 'r1', now=1)
        t.record('login_brute', '203.0.113.9', 'r1', now=2)
        t.record('login_after_fail', '203.0.113.9', 'r1', now=3)
        ev = t.record('scan', 'r1', None, now=4)                          # 入られた装置が探索 → 横展開も
        assert {e['technique'] for e in ev} == {'T1046', 'T1021'}
        t.record('exfil', 'r1', '198.51.100.7', now=5)
        snap = t.snapshot(now=10)
        r1 = next(x for x in snap['targets'] if x['target'] == 'r1')
        assert r1['max_ja'] == '窃取' and r1['tactics'][0] == 'TA0043'
        org = snap['orgs'][0]
        assert org['org'] == 'LAB' and org['max_en'] == 'Exfiltration'
        col = {m['id']: m for m in snap['matrix']}
        assert col['TA0006']['techniques'][0]['id'] == 'T1110'
        assert attack_map.sensor_rows('ET SCAN Nmap')[0][1] == 'T1595'
        assert attack_map.sensor_rows('SQL injection attempt')[0][1] == 'T1190'

    def test_login_hooks_and_api(self, client):
        dev = f'ak-{_RUN}'
        _dev(client, dev, 'cisco', 'AK')
        for _ in range(3):
            app_module._on_auth(dev, '127.0.0.91', False, 'admin', time.time())
        app_module._on_auth(dev, '127.0.0.91', True, 'admin', time.time())
        rules = [a['rule'] for a in app_module._flow_recorder.alerts if a.get('dst') == dev]
        assert 'login_brute' in rules and 'login_after_fail' in rules
        d = client.get('/api/attack').json()
        x = next(t for t in d['targets'] if t['target'] == dev)
        assert 'TA0001' in x['tactics'] and 'TA0006' in x['tactics'] and x['host'] == 'AK'


# ── E ──────────────────────────────────────────────
class TestNexus:
    def test_hub_common_source_and_technique(self):
        h = nexus.NexusHub()
        now = time.time()
        h.ingest('TOKYO', {'alerts': [{'t': now, 'rule': 'darknet_scan', 'src': '198.51.100.23', 'detail': 'x'}],
                           'attack': [{'t': now, 'tactic': 'TA0043', 'technique': 'T1595', 'name': 'scan', 'target': 'TOKYO'}]})
        f = h.ingest('OSAKA', {'alerts': [{'t': now, 'rule': 'conn_scan', 'src': '198.51.100.23', 'detail': 'y'}],
                               'attack': [{'t': now, 'tactic': 'TA0043', 'technique': 'T1595', 'name': 'scan', 'target': 'r'}]})
        kinds = {x['kind'] for x in f}
        assert {'common_src', 'same_technique'} <= kinds
        cs = next(x for x in f if x['kind'] == 'common_src')
        assert cs['sites'] == ['OSAKA', 'TOKYO'] and '198.51.100.23' in cs['text']
        with pytest.raises(ValueError):
            h.ingest('', {})

    def test_report_endpoint_and_reporter(self, client, monkeypatch):
        r = client.post('/api/nexus/report', json={'site': f'S-{_RUN}', 'alerts': [{'t': time.time(), 'rule': 'scan', 'src': '192.0.2.99'}]})
        assert r.status_code == 200 and r.json()['ok']
        assert any(s['name'] == f'S-{_RUN}' for s in client.get('/api/nexus').json()['sites'])
        got = []

        class _R:
            def __init__(self, b):
                self.b = b

            def read(self):
                return self.b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        sent = []

        def fake_urlopen(req, timeout=0):
            sent.append(json.loads(req.data))
            return _R(json.dumps({'findings': [{'id': 'common_src:1.2.3.4', 'kind': 'common_src', 'sites': ['A', 'B'], 'text': 't'}]}).encode())
        monkeypatch.setattr(nexus.urllib.request, 'urlopen', fake_urlopen)
        rep = nexus.NexusReporter('http://central', 'A', 'tok', lambda: [{'seq': 1, 't': 1, 'rule': 'scan'}], lambda: [], got.append)
        assert rep.tick() and got[0]['id'] == 'common_src:1.2.3.4'
        assert sent[0]['site'] == 'A' and len(sent[0]['alerts']) == 1
        assert rep.tick() == []                    # 同じ所見は2回出さない
        assert sent[1]['alerts'] == []             # 送ったものは2回送らない


# ── A ──────────────────────────────────────────────
class TestWan:
    def test_simulated_congestion_only_hits_pppoe(self):
        import random
        rnd = random.Random(1).random
        assert wan_watch.simulated_rtt('pppoe', 0, rnd)[0] < 5
        hi = [wan_watch.simulated_rtt('pppoe', 100, rnd) for _ in range(20)]
        assert min(r for r, _ in hi) > 30 and max(lo for _, lo in hi) > 0
        assert max(wan_watch.simulated_rtt('ipoe', 100, rnd)[0] for _ in range(20)) < 3

    def test_series_notice(self):
        fired = []
        s = wan_watch.WanSeries(fire=lambda *a: fired.append(a))
        for k in range(3):
            s.add('x', 50.0, 0.0, now=k, label='X')
        assert fired and fired[0][0] == 'wan_slow'
        s.add('x', 2.0, 0.0, now=4)
        for k in range(3):
            s.add('x', None, 1.0, now=5 + k)
        assert fired[-1][0] == 'wan_loss'

    def test_lab_pppoe_vs_ipoe(self, client):
        bas, rtx = f'wb-{_RUN}', f'wr-{_RUN}'
        _dev(client, bas, 'bas', 'W-BAS')
        _dev(client, rtx, 'yamaha', 'W-RTX')
        client.post('/api/link', json={'a': rtx, 'b': bas, 'iface_a': 'lan2', 'iface_b': 'wan1'})
        _cli(client, bas, 'configure terminal', 'ip pool 100.64.77.10 100.64.77.20/24', 'pppoe-user u1 p1',
             'map-e ipv4-pool 203.0.116.70 203.0.116.71', 'map-e br-address 2001:db8::77', 'pppoe congestion 90', 'end')
        assert 'pppoe congestion 90' in _cli(client, bas, 'show running-config')
        _cli(client, rtx, 'administrator', 'pp select 1', 'pppoe use lan2', 'pp auth myname u1 p1', 'exit', 'pp enable 1',
             'map-e use lan2')
        for _ in range(4):
            app_module._wan_tick()
        d = client.get('/api/wan').json()
        paths = {p['kind']: p for p in d['paths'] if p['cpe'] == rtx}
        assert paths['pppoe']['rtt_ms'] > 30 and paths['ipoe']['rtt_ms'] < 5
        c = next(x for x in d['cpes'] if x['dev'] == rtx)
        assert c['pppoe'] and c['ipoe']
        assert any(a['rule'] == 'wan_slow' and rtx in a['detail'] or a['rule'] in ('wan_slow', 'wan_loss')
                   for a in app_module._flow_recorder.alerts)


# ── D ──────────────────────────────────────────────
def _netflow_v5(recs):
    hdr = struct.pack('!HHIIIIBBH', 5, len(recs), 0, int(time.time()), 0, 1, 0, 0, 0)
    body = b''
    for src, dst, sp, dp, proto, pk, by in recs:
        body += socket.inet_aton(src) + socket.inet_aton(dst) + b'\0' * 4 + struct.pack('!HH', 0, 0) + \
            struct.pack('!IIII', pk, by, 0, 0) + struct.pack('!HH', sp, dp) + bytes([0, 0x02, proto, 0]) + b'\0' * 8
    return hdr + body


def _sflow_v5(src, dst, dport, rate=100):
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 40, 0, 0, 64, 6, 0, socket.inet_aton(src), socket.inet_aton(dst))
    tcp = struct.pack('!HHIIBBHHH', 40000, dport, 0, 0, 5 << 4, 0x02, 0, 0, 0)
    hdr = b'\0' * 12 + b'\x08\x00' + ip + tcp
    pad = b'\0' * (-len(hdr) % 4)                      # sFlow(XDR)は4バイト境界に詰める
    rec = struct.pack('!IIII', 1, 60, 4, len(hdr)) + hdr + pad
    sample = struct.pack('!IIIIIIII', 1, 1, rate, 1000, 0, 1, 2, 1) + struct.pack('!II', 1, len(rec)) + rec
    return struct.pack('!II4sIIII', 5, 1, socket.inet_aton('192.0.2.1'), 0, 1, 1000, 1) + struct.pack('!II', 1, len(sample)) + sample


class TestFlowsSensorsMailCapture:
    def test_parse_netflow_and_sflow(self):
        f = flow_collector.parse_netflow_v5(_netflow_v5([('198.51.100.5', '10.250.9.9', 4000, 445, 6, 3, 180)]), 'x')
        assert f[0]['src'] == '198.51.100.5' and f[0]['dport'] == 445 and f[0]['proto'] == 'tcp' and f[0]['bytes'] == 180
        s = flow_collector.parse_sflow_v5(_sflow_v5('198.51.100.6', '10.250.9.8', 22, rate=200))
        assert s[0]['src'] == '198.51.100.6' and s[0]['dport'] == 22 and s[0]['bytes'] == 60 * 200
        assert flow_collector.parse_netflow_v5(b'\x00\x09' + b'\0' * 30) == []

    def test_real_udp_collector_feeds_darknet(self, client):
        got = []
        fc = flow_collector.FlowCollector(got.append, bind='127.0.0.1', netflow=0, sflow=0)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        fc.ports = {'netflow': port, 'sflow': 0}
        fc.start()
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.sendto(_netflow_v5([('198.51.100.5', '10.250.9.9', 4000, 445, 6, 1, 60)]), ('127.0.0.1', port))
            for _ in range(30):
                if got:
                    break
                time.sleep(0.1)
            assert got and got[0]['exporter'] == '127.0.0.1'
        finally:
            fc.stop()
        darknet.set_prefixes(['10.250.9.0/24'])
        try:
            darknet.clear()
            app_module._on_flow(got[0])
            assert darknet.snapshot()['events'][0]['via'] == 'netflow'
        finally:
            darknet.set_prefixes([])
            darknet.clear()

    def test_vendor_syslog(self, client):
        fg = ('date=2026-11-17 time=02:00:00 devname="FG" type="utm" subtype="ips" level="alert" srcip=198.51.100.44 '
              'dstip=10.1.1.5 dstport=80 attack="HTTP.URI.SQL.Injection" action="dropped" crlevel="critical"')
        p = sensor_syslog.parse(fg)
        assert p['product'] == 'FortiGate' and p['src'] == '198.51.100.44' and p['severity'] == 'high'
        assert sensor_syslog.parse('%ASA-4-733101: Host 192.0.2.66 is attacking. Current burst rate is 20 per second')['src'] == '192.0.2.66'
        sn = sensor_syslog.parse('[1:2010935:3] ET SCAN Suspicious inbound to MSSQL port 1433 [**] [Priority: 2] {TCP} 203.0.113.7:4444 -> 10.2.2.2:1433')
        assert sn['product'] == 'Snort/Suricata' and sn['dst'] == '10.2.2.2'
        assert sensor_syslog.parse('%SYS-5-CONFIG_I: Configured from console') is None
        s = app_module._on_sensor_line(fg, '127.0.0.1')
        assert s['product'] == 'FortiGate'
        a = [x for x in app_module._flow_recorder.alerts if x['rule'] == 'ext_sensor'][-1]
        assert a['source'] == 'SENSOR' and a['severity'] == 'high'
        d = client.get('/api/attack').json()
        assert any(q['id'] == 'T1190' for m in d['matrix'] for q in m['techniques'])

    def test_email_contact_real_smtp_conversation(self):
        from engine import nirvana_board as NB
        got = {}

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                w = lambda s: self.wfile.write((s + '\r\n').encode())  # noqa: E731
                w('220 test')
                data = False
                lines = []
                for raw in self.rfile:
                    line = raw.decode().rstrip('\r\n')
                    if data:
                        if line == '.':
                            data = False
                            got['data'] = '\n'.join(lines)
                            w('250 ok')
                        else:
                            lines.append(line)
                        continue
                    cmd = line.split(' ')[0].upper()
                    if cmd in ('EHLO', 'HELO'):
                        w('250 test')
                    elif cmd == 'MAIL':
                        w('250 ok')
                    elif cmd == 'RCPT':
                        got.setdefault('rcpt', []).append(line)
                        w('250 ok')
                    elif cmd == 'DATA':
                        data = True
                        w('354 go')
                    elif cmd == 'QUIT':
                        w('221 bye')
                        return
                    else:
                        w('250 ok')
        srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = NB.validate_contact({'name': 'noc', 'type': 'email', 'to': 'noc@example.com; sec@example.com',
                                     'smtp_host': '127.0.0.1', 'smtp_port': srv.server_address[1]})
            assert c['to'] == 'noc@example.com, sec@example.com'
            code = NB.send_contact(c, NB.webhook_payload({'uid': 'r', 'title': 'リンク断', 'severity': 'critical'},
                                                         {'key': 'k', 'labels': {'a': 'r1'}, 'value': 3, 'since': time.time()}, 'firing'))
            assert code == 250 and len(got['rcpt']) == 2
            assert 'FIRING' in got['data'] and 'a = r1' in got['data'].replace('=\n', '')
        finally:
            srv.shutdown()
        with pytest.raises(ValueError):
            NB.validate_contact({'name': 'x', 'type': 'email', 'to': 'bad', 'smtp_host': 'h'})

    def test_pcap_and_capture_api(self, client):
        ring = capture.PacketRing()
        ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 40, 0, 0, 64, 6, 0, socket.inet_aton('127.0.0.66'), socket.inet_aton('10.246.1.1'))
        ring.add(100.0, b'\0' * 12 + b'\x08\x00' + ip + b'\0' * 20)
        ring.add(100.5, b'\0' * 12 + b'\x86\xdd' + b'\0' * 40)           # IPv6 は覚えない
        fr = ring.select({'127.0.0.66'}, 90, 110)
        assert len(fr) == 1
        pc = capture.to_pcap(fr)
        assert pc[:4] == struct.pack('<I', 0xa1b2c3d4) and struct.unpack('<I', pc[20:24])[0] == 1
        assert struct.unpack('<I', pc[24:28])[0] == 100
        a = _alert('conn_scan', '127.0.0.66', None)
        j = client.get(f'/api/alerts/{a["seq"]}/capture?format=json').json()
        assert j['alert']['seq'] == a['seq'] and '127.0.0.66' in j['ips']
        assert client.get('/api/alerts/999999999/capture').status_code == 404
