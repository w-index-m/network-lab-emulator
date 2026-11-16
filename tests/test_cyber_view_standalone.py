"""
Cyber View スタンドアロン版(cyber-view-standalone/)の回帰テスト

実機の監視データ(Prometheus / Loki / Zabbix)だけで画面用の API を返せることを、
偽の HTTP サーバー(実物の API と同じ形の応答)で確かめる。ネットワーク・docker 非依存。
- ラボ版と共有しているファイル(画面・検知ルール)がずれていないこと
- Prometheus: 流量・ifOperStatus(POLL)・up=0(応答なし)
- Loki: syslog の取り込み・LINK DOWN の SYSLOG 経路・error 以上のアラート・フラップ警告
- Zabbix: Bits received/sent・Operational status・管理者 shutdown で LLD 無効化された IF を down 扱い・問題→アラート
"""
import filecmp
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SA = os.path.join(ROOT, 'cyber-view-standalone')
sys.path.insert(0, SA)
import server as sa_server  # noqa: E402
from cvs import config as sa_config  # noqa: E402


def test_shared_files_are_in_sync_with_the_lab():
    """画面と検知ルールはラボ版と同じファイル。片方だけ直すとずれるので固定する"""
    assert filecmp.cmp(os.path.join(ROOT, 'static', 'cyber_view.html'), os.path.join(SA, 'web', 'cyber_view.html'), shallow=False)
    for f in ('flap_rules.py', 'link_evidence.py', 'attack_map.py', 'darknet.py', 'nexus.py', 'wan_watch.py',
              'flow_collector.py', 'sensor_syslog.py', 'route_watch.py'):
        assert filecmp.cmp(os.path.join(ROOT, 'engine', f), os.path.join(SA, 'cvs', f), shallow=False), f


def test_example_config_loads_and_validation_errors():
    cfg = sa_config.load(os.path.join(SA, 'config.example.yaml'))
    assert [d['id'] for d in cfg['devices']] == ['cr1', 'cr2', 'cr3'] and len(cfg['links']) == 2
    with pytest.raises(sa_config.ConfigError):
        sa_config.validate({'devices': [{'id': 'a'}], 'links': [{'a': 'a', 'b': 'x', 'a_if': 'g1', 'b_if': 'g1'}]})
    with pytest.raises(sa_config.ConfigError):
        sa_config.validate({'devices': [{'id': 'a'}, {'id': 'a'}]})


# ── 偽の監視サーバー ─────────────────────────────────
class Fake:
    """1つのポートで Prometheus / Loki / Zabbix の API を真似る"""

    def __init__(self):
        self.prom = {}       # クエリの先頭語 -> result 行
        self.loki = []       # (t, labels, line)
        self.zbx_items = []
        self.zbx_problems = []
        self.zbx_hosts = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == '/api/v1/query':
                    expr = q['query'][0]
                    for k, rows in fake.prom.items():
                        if expr.startswith(k):
                            return self._send({'status': 'success', 'data': {'result': rows}})
                    return self._send({'status': 'success', 'data': {'result': []}})
                if u.path == '/loki/api/v1/query_range':
                    s, e = int(q['start'][0]) / 1e9, int(q['end'][0]) / 1e9
                    streams = {}
                    for t, lab, line in fake.loki:
                        if s <= t <= e:
                            streams.setdefault(json.dumps(lab, sort_keys=True), []).append([str(int(t * 1e9)), line])
                    return self._send({'status': 'success', 'data': {'result': [
                        {'stream': json.loads(k), 'values': v} for k, v in streams.items()]}})
                self.send_response(404)
                self.end_headers()

            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                m = req['method']
                res = {'host.get': fake.zbx_hosts, 'item.get': fake.zbx_items,
                       'problem.get': [p for p in fake.zbx_problems if p['hostid'] in req['params'].get('hostids', [])]
                       }.get(m, [])
                self._send({'jsonrpc': '2.0', 'result': res, 'id': req['id']})

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_port}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def _prom_row(inst, ifn, v):
    return {'metric': {'instance': f'{inst}:161' if ':' not in inst else inst, 'ifName': ifn}, 'value': [0, str(v)]}


def _cfg(sources):
    return sa_config.validate({
        'devices': [
            {'id': 'r1', 'hostname': 'R1', 'type': 'cisco', 'mgmt_ip': '10.9.0.1',
             'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.9.12.1', 'prefix': 30}]},
            {'id': 'r2', 'hostname': 'R2', 'type': 'cisco', 'mgmt_ip': '10.9.0.2',
             'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.9.12.2', 'prefix': 30}]},
        ],
        'links': [{'a': 'r1', 'a_if': 'GigabitEthernet0/0/0', 'b': 'r2', 'b_if': 'GigabitEthernet0/0/0', 'speed': 100_000_000}],
        'sources': sources})


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.srv.shutdown()


def test_prometheus_and_loki(fake):
    fake.prom = {
        'rate(ifHCOutOctets': [_prom_row('10.9.0.1', 'Gi0/0/0', 60e6), _prom_row('10.9.0.2', 'Gi0/0/0', 2e6)],
        'rate(ifHCInOctets': [_prom_row('10.9.0.1', 'Gi0/0/0', 2e6), _prom_row('10.9.0.2', 'Gi0/0/0', 60e6)],
        'ifOperStatus': [_prom_row('10.9.0.1', 'Gi0/0/0', 1), _prom_row('10.9.0.2', 'Gi0/0/0', 2)],
        'up{': [{'metric': {'instance': '10.9.0.1'}, 'value': [0, '1']}, {'metric': {'instance': '10.9.0.2'}, 'value': [0, '1']}],
        'ifHC': [],
    }
    now = time.time()
    lab = {'job': 'syslog', 'host': 'R2'}
    fake.loki = [(now - 5 + i, lab, f'%LINK-3-UPDOWN: Interface GigabitEthernet0/0/0, changed state to {s}')
                 for i, s in enumerate(['down', 'up', 'down', 'up', 'down'])]
    app = sa_server.create_app(_cfg({'prometheus': {'url': fake.url, 'job': 'snmp'},
                                     'loki': {'url': fake.url}}), start_collector=False)
    col = app.state.collector
    col._log_until = now - 60
    col.tick()
    c = TestClient(app)
    d = c.get('/api/traffic/links').json()
    ln = d['links'][0]
    assert d['source'] == 'prometheus' and ln['ab_bps'] == 60e6 and ln['ba_bps'] == 2e6
    assert ln['util'] == pytest.approx(0.6) and ln['down'] is True       # 使用率60%だが断が優先
    assert ln['evidence'] == {'STATE': None, 'SYSLOG': 'down', 'TRAP': None, 'POLL': 'down'}
    assert ln['state'] == 'down' and ln['down_sources'] == ['SYSLOG', 'POLL']
    rules = [a['rule'] for a in c.get('/api/traffic/alerts').json()['alerts']]
    assert 'link_down' in rules and 'syslog_error' in rules and 'if_flap' in rules
    h = {x['key']: x for x in c.get('/api/traffic/health').json()['categories']}
    assert h['network']['state'] == 'crit' and 'リンク断 1本' in h['network']['summary']
    assert h['syslog']['state'] == 'crit' and 'フラップ' in h['syslog']['summary']
    # 画面上の呼び名: Prometheus は「ヴァルハラ」(API の source 値は prometheus のまま)
    assert '取得元 ヴァルハラ' in h['traffic']['summary']
    logs = c.get('/api/traffic/logs?since=0&limit=10').json()
    assert logs['source'] == 'loki' and logs['logs'][-1]['host'] == 'R2'
    hosts = c.get('/api/traffic/hosts').json()['hosts']
    assert {h['hostname'] for h in hosts} == {'R1', 'R2'}
    assert next(h for h in hosts if h['id'] == 'r2')['interfaces'][0]['up'] is False
    # 画面は同じファイル。デモはラボ専用なので 404(画面は DEMO ボタンを隠す)
    assert c.get('/api/demo/status').status_code == 404
    assert 'CYBER' in c.get('/').text


def test_prometheus_target_down_is_no_response(fake):
    fake.prom = {'up{': [{'metric': {'instance': '10.9.0.2:161'}, 'value': [0, '0']}]}
    app = sa_server.create_app(_cfg({'prometheus': {'url': fake.url, 'job': 'snmp'}}), start_collector=False)
    d = TestClient(app).get('/api/traffic/links').json()
    assert d['unreachable'] == [{'id': 'r2', 'hostname': 'R2', 'instance': '10.9.0.2'}]
    assert d['links'][0]['state'] == 'lost' and d['links'][0]['evidence']['POLL'] == 'lost'


def test_zabbix_only(fake):
    fake.zbx_hosts = [{'hostid': '1', 'host': 'R1', 'name': 'R1', 'interfaces': [{'available': '1', 'type': '2'}]},
                      {'hostid': '2', 'host': 'R2', 'name': 'R2', 'interfaces': [{'available': '1', 'type': '2'}]}]
    now = int(time.time())
    it = lambda hid, name, v, status='0', dis=None: {  # noqa: E731
        'hostid': hid, 'name': name, 'lastvalue': str(v), 'lastclock': str(now), 'status': status,
        'itemDiscovery': {'disable_source': dis} if dis else []}
    fake.zbx_items = [
        it('1', 'Interface Gi0/0/0(WAN to R2 (osaka)): Bits sent', 5_000_000),
        it('1', 'Interface Gi0/0/0(WAN to R2 (osaka)): Bits received', 1_000_000),
        it('1', 'Interface Gi0/0/0(WAN to R2 (osaka)): Operational status', 1),
        # R2 側は管理者 shutdown → Cisco テンプレートの LLD が IF を外してアイテムを無効化
        it('2', 'Interface Gi0/0/0(): Operational status', 2, status='1', dis='1'),
        it('2', 'Interface Gi0/0/0(): Bits sent', 0, status='1', dis='1'),
    ]
    fake.zbx_problems = [{'hostid': '2', 'eventid': '77', 'name': 'Cisco IOS: Interface Gi0/0/0(): Link down',
                          'severity': '3', 'clock': str(now)}]
    app = sa_server.create_app(_cfg({'zabbix': {'url': fake.url + '/api_jsonrpc.php', 'token': 'x'}}),
                               start_collector=False)
    app.state.collector.tick()
    c = TestClient(app)
    ln = c.get('/api/traffic/links').json()['links'][0]
    assert ln['source'] == 'zabbix' and ln['ab_bps'] == 5_000_000
    assert ln['down'] is True and ln['down_sources'] == ['POLL']
    al = c.get('/api/traffic/alerts').json()['alerts']
    zp = [a for a in al if a['rule'] == 'zabbix_problem']
    assert zp and zp[0]['severity'] == 'medium' and zp[0]['src'] == 'r2' and 'Link down' in zp[0]['detail']
    h = c.get('/api/traffic/health').json()
    assert h['sources']['metrics'] == 'zabbix'
    assert 'Zabbix の問題 1件' in h['categories'][0]['summary']


def test_auth_token(fake):
    cfg = _cfg({})
    cfg['auth'] = {'token': 'secret'}
    c = TestClient(sa_server.create_app(cfg, start_collector=False))
    assert c.get('/api/traffic/hosts').status_code == 401
    assert c.get('/api/traffic/hosts', headers={'X-Session-Token': 'secret'}).status_code == 200


def test_prometheus_is_displayed_as_valhalla():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert "SRC_DISPLAY = { prometheus: 'VALHALLA'" in html and 'ヴァルハラ 不達' in html


def test_security_features_netflow_darknet_attack_nexus_wan():
    """スタンドアロン版: NetFlow → ダークネット・ATT&CK、外部センサー(Loki のログ)、ネクサスの中央、回線チェック(TCP で測る)"""
    import socket as _s
    import struct as _st
    from cvs import security as sec_mod
    u = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    u.bind(('127.0.0.1', 0))
    nf_port = u.getsockname()[1]
    u.close()
    lis = _s.socket()
    lis.bind(('127.0.0.1', 0))
    lis.listen(8)
    tcp_port = lis.getsockname()[1]
    cfg = _cfg({'flow': {'netflow': nf_port, 'bind': '127.0.0.1'}, 'darknet': {'prefixes': ['10.250.7.0/24']},
                'nexus': {'site': 'NAGOYA', 'token': 'tok'},
                'wan_probe': [{'name': 'GW', 'target': f'127.0.0.1:{tcp_port}'}]})
    app = sa_server.create_app(cfg, start_collector=False)
    col = app.state.collector
    col.sec.flows.start()
    try:
        c = TestClient(app)
        hdr = _st.pack('!HHIIIIBBH', 5, 6, 0, int(time.time()), 0, 1, 0, 0, 0)
        body = b''.join(_s.inet_aton('198.51.100.5') + _s.inet_aton(f'10.250.7.{i}') + b'\0' * 8 +
                        _st.pack('!IIII', 1, 60, 0, 0) + _st.pack('!HH', 4000, 445) + bytes([0, 2, 6, 0]) + b'\0' * 8
                        for i in range(1, 7))
        _s.socket(_s.AF_INET, _s.SOCK_DGRAM).sendto(hdr + body, ('127.0.0.1', nf_port))
        for _ in range(30):
            if col.sec.flows.stats['flows'] >= 6:
                break
            time.sleep(0.1)
        d = c.get('/api/darknet').json()
        assert d['hour']['cats']['scan'] >= 1 and 'NetFlow' in d['sensor']['label']
        assert c.get('/api/traffic/flowmap').json()['events'][0]['via'] == 'netflow'
        # 外部センサー: Loki から来たログに Snort の行
        col.sec.on_log('r1', '[1:2001219:20] ET SCAN Potential SSH Scan [**] [Priority: 2] {TCP} 198.51.100.5:4000 -> 10.9.0.1:22', time.time())
        a = c.get('/api/attack').json()
        assert a['site'] == 'NAGOYA' and any(q['id'] == 'T1595' for m in a['matrix'] for q in m['techniques'])
        # ネクサスの中央: トークン違いは 403、正しければ所見を返す
        assert c.post('/api/nexus/report', json={'site': 'OSAKA'}, headers={'X-Nexus-Token': 'bad'}).status_code == 403
        r = c.post('/api/nexus/report', json={'site': 'OSAKA', 'alerts': [{'t': time.time(), 'rule': 'scan', 'src': '198.51.100.5'}]},
                   headers={'X-Nexus-Token': 'tok'}).json()
        assert r['ok']
        col.sec.probe_tick()
        w = c.get('/api/wan').json()
        assert w['simulated'] is False and w['paths'][0]['rtt_ms'] is not None and w['paths'][0]['loss'] == 0
        assert sec_mod.ping('127.0.0.1:1', count=1, timeout=0.3) == (None, 1.0)
    finally:
        col.sec.flows.stop()
        lis.close()
    # 設定が無ければ API は 404(画面のボタンは自動で隠れる)
    c2 = TestClient(sa_server.create_app(_cfg({}), start_collector=False))
    assert c2.get('/api/darknet').status_code == 404 and c2.get('/api/wan').status_code == 404


def test_snmp_poller_rfc1213_routes_and_cpu_fallback(monkeypatch):
    """古い機種: inetCidr / ipCidr が無ければ RFC1213 ipRouteTable、CPU は設定の OID → Cisco → hrProcessorLoad"""
    from cvs import snmp_poll as SP
    rows = {
        SP.RFC1213_ROUTE + '.7': [(SP.RFC1213_ROUTE + '.7.0.0.0.0', '192.168.1.1'), (SP.RFC1213_ROUTE + '.7.192.168.1.0', '0.0.0.0')],
        SP.RFC1213_ROUTE + '.9': [(SP.RFC1213_ROUTE + '.9.0.0.0.0', 3), (SP.RFC1213_ROUTE + '.9.192.168.1.0', 2)],
        SP.RFC1213_ROUTE + '.11': [(SP.RFC1213_ROUTE + '.11.0.0.0.0', '0.0.0.0'), (SP.RFC1213_ROUTE + '.11.192.168.1.0', '255.255.255.0')],
        SP.RFC1213_ROUTE + '.2': [(SP.RFC1213_ROUTE + '.2.0.0.0.0', 3), (SP.RFC1213_ROUTE + '.2.192.168.1.0', 2)],
        SP.IFNAME: [(SP.IFNAME + '.2', 'lan1'), (SP.IFNAME + '.3', 'pp1')],
        SP.HR_CPU: [(SP.HR_CPU + '.1', 30), (SP.HR_CPU + '.2', 50)],
    }
    monkeypatch.setattr(SP.SC, 'walk', lambda h, c, root, **k: rows.get(root, []))
    p = SP.SnmpPoller({'devices': [{'id': 'rtx', 'hostname': 'RTX', 'mgmt_ip': '127.0.0.1'}], 'sources': {'snmp': {}}}, None, {})
    d = p.devs[0]
    r = sorted((x['network'], x['prefix'], x['next_hop'], x['source'], x['iface']) for x in p.routes(d, 0))
    assert r == [('0.0.0.0', 0, '192.168.1.1', 'static', 'pp1'), ('192.168.1.0', 24, '', 'connected', 'lan1')]
    assert p.cpu(d, '127.0.0.1', 'public') == 40                 # hrProcessorLoad の平均
    rows['1.3.6.1.4.1.1182.9'] = [('1.3.6.1.4.1.1182.9.0', 77)]
    d['snmp_cpu_oid'] = '1.3.6.1.4.1.1182.9'
    assert p.cpu(d, '127.0.0.1', 'public') == 77                 # 設定した OID が優先
