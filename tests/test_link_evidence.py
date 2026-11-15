"""
リンク断の検知経路(STATE / SYSLOG / TRAP / POLL)の回帰テスト

どの経路で気づいても /cyber-view の LINK DOWN になること、どの経路が断を示しているかが
links API / health に出ること、復旧メッセージで解除されることを固定する。
"""

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.link_evidence import (TRAP_OID_LINKDOWN, TRAP_OID_LINKUP, decode_trap, norm_if,  # noqa: E402
                                  parse_log_link_event, traps)
from engine.syslog_sender import build_snmp_v2c_trap, send_snmp_trap  # noqa: E402
from engine.traffic_analytics import recorder  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def lab(client, monkeypatch):
    """このテストの装置だけにする: r1 Gi0/0/0 ─ r2 Gi0/0/0 (10.246.12.0/30)"""
    monkeypatch.delenv('PROMETHEUS_URL', raising=False)
    monkeypatch.delenv('LOKI_URL', raising=False)
    saved = dict(app_module.device_sessions)
    links = {k: set(v) for k, v in app_module.vnet.links.items()}
    app_module.device_sessions.clear()
    app_module.vnet.links.clear()
    recorder.clear()
    traps.clear()
    r1, r2 = f'le-r1-{_RUN}', f'le-r2-{_RUN}'
    client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'LE-R1'})
    client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'LE-R2'})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    for d, ip in ((r1, '10.246.12.1'), (r2, '10.246.12.2')):
        for c in ('configure terminal', 'interface GigabitEthernet0/0/0',
                  f'ip address {ip} 255.255.255.252', 'no shutdown', 'end'):
            client.post('/api/cli', json={'device_id': d, 'command': c})
    recorder.clear()
    traps.clear()
    yield client, r1, r2
    for d in (r1, r2):
        for c in ('configure terminal', 'no snmp-server host 127.0.0.1', 'end'):
            client.post('/api/cli', json={'device_id': d, 'command': c})
    app_module.device_sessions.clear()
    app_module.device_sessions.update(saved)
    app_module.vnet.links.clear()
    app_module.vnet.links.update(links)
    recorder.clear()
    traps.clear()
    traps.stop()


def _link(c):
    d = c.get('/api/traffic/links').json()
    assert len(d['links']) == 1, d['links']
    return d['links'][0]


class TestParsing:
    def test_log_patterns(self):
        assert parse_log_link_event('%LINK-3-UPDOWN: Interface GigabitEthernet0/0/2, changed state to down') \
            == ('GigabitEthernet0/0/2', 'down')
        assert parse_log_link_event('%LINK-5-CHANGED: Interface Gi0/1, changed state to administratively down') \
            == ('Gi0/1', 'down')
        assert parse_log_link_event('%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/1, changed state to up') \
            == ('Gi0/1', 'up')
        assert parse_log_link_event('lan0 Link Down')[1] == 'down'           # Si-R
        assert parse_log_link_event('%SYS-5-CONFIG_I: Configured from console') is None

    def test_iface_normalization(self):
        assert norm_if('GigabitEthernet0/0/2') == norm_if('Gi0/0/2') == 'gi0/0/2'
        assert norm_if('TenGigabitEthernet1/1') == norm_if('Te1/1')

    def test_decode_emulator_trap(self):
        raw = build_snmp_v2c_trap('public', 'CR2', TRAP_OID_LINKDOWN, 'GigabitEthernet0/0/2 down')
        info = decode_trap(raw)
        assert info['trap_oid'] == TRAP_OID_LINKDOWN and info['descr'] == 'CR2: GigabitEthernet0/0/2 down'
        ev = traps.record('192.0.2.9', info)
        assert ev['host'] == 'CR2' and ev['iface'] == 'GigabitEthernet0/0/2' and ev['state'] == 'down'
        traps.clear()


class TestEachPathAlone:
    """ラボの装置自体(STATE)は up のまま、他の1経路だけで断が分かるケース(実機監視と同じ)"""

    def test_syslog_only(self, lab):
        c, r1, _r2 = lab
        recorder.record_log(r1, 'LE-R1', 3, '%LINK-3-UPDOWN: Interface GigabitEthernet0/0/0, changed state to down')
        ln = _link(c)
        assert ln['down'] and ln['down_sources'] == ['SYSLOG'] and ln['state'] == 'down'
        assert ln['evidence']['STATE'] == 'up' and ln['evidence']['TRAP'] is None
        recorder.record_log(r1, 'LE-R1', 3, '%LINK-3-UPDOWN: Interface GigabitEthernet0/0/0, changed state to up',
                            now=time.time() + 1)
        assert _link(c)['down'] is False

    def test_trap_only(self, lab):
        c, _r1, _r2 = lab
        traps.record('198.51.100.7', {'trap_oid': TRAP_OID_LINKDOWN, 'descr': 'LE-R2: Gi0/0/0 down', 'ifname': ''})
        ln = _link(c)
        assert ln['down_sources'] == ['TRAP']
        traps.record('198.51.100.7', {'trap_oid': TRAP_OID_LINKUP, 'descr': 'LE-R2: Gi0/0/0 up', 'ifname': ''},
                     now=time.time() + 1)
        assert _link(c)['down'] is False

    def test_trap_matched_by_source_ip_and_ifdescr(self, lab):
        """実機の trap は sysDescr ではなく ifDescr を持ち、送信元IPで装置を特定する"""
        c, _r1, _r2 = lab
        traps.record('10.246.12.1', {'trap_oid': TRAP_OID_LINKDOWN, 'descr': 'Cisco IOS XE',
                                     'ifname': 'GigabitEthernet0/0/0'})
        assert _link(c)['down_sources'] == ['TRAP']

    def test_poll_only(self, lab, monkeypatch):
        c, _r1, _r2 = lab

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                ifoper = 'ifOperStatus' in self.path
                res = [{'metric': {'instance': '10.246.12.1', 'ifName': 'Gi0/0/0'},
                        'value': [0, '2' if ifoper else '0']}]
                body = json.dumps({'status': 'success', 'data': {'result': res}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            monkeypatch.setenv('PROMETHEUS_URL', f'http://127.0.0.1:{srv.server_port}')
            ln = _link(c)
            assert ln['down_sources'] == ['POLL'] and ln['evidence']['POLL'] == 'down'
        finally:
            srv.shutdown()


def test_real_shutdown_lights_state_syslog_and_trap(lab):
    """CLI で shutdown → 内部状態・syslog・実際に UDP で届いた linkDown trap の3経路が揃う"""
    c, r1, _r2 = lab
    listen = traps.start('127.0.0.1:0')
    port = int(listen.rsplit(':', 1)[1])
    for cmd in ('configure terminal', f'snmp-server host 127.0.0.1 udp-port {port} traps public', 'end'):
        c.post('/api/cli', json={'device_id': r1, 'command': cmd})
    for cmd in ('configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end'):
        c.post('/api/cli', json={'device_id': r1, 'command': cmd})
    end = time.time() + 5
    while time.time() < end and not traps.snapshot():
        time.sleep(0.1)
    ln = _link(c)
    assert ln['down_sources'] == ['STATE', 'SYSLOG', 'TRAP'], ln['evidence']
    h = {x['key']: x for x in c.get('/api/traffic/health').json()['categories']}
    assert h['network']['state'] == 'crit'
    assert any('STATE / SYSLOG / TRAP' in d for d in h['network']['details'])
    for cmd in ('configure terminal', 'interface GigabitEthernet0/0/0', 'no shutdown', 'end'):
        c.post('/api/cli', json={'device_id': r1, 'command': cmd})
    end = time.time() + 5
    while time.time() < end and len(traps.snapshot()) < 2:
        time.sleep(0.1)
    ln = _link(c)
    assert ln['down'] is False and ln['evidence']['TRAP'] == 'up' and ln['evidence']['SYSLOG'] == 'up'


def test_quick_flap_is_not_lost_by_dedupe(lab):
    """down→up を1秒以内に繰り返しても、最後の up が重複扱いで落ちて断のまま残らない"""
    c, r1, _r2 = lab
    for _ in range(2):
        for cmd in ('configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'no shutdown', 'end'):
            c.post('/api/cli', json={'device_id': r1, 'command': cmd})
    ln = _link(c)
    assert ln['down'] is False and ln['evidence']['SYSLOG'] == 'up'


def test_send_snmp_trap_reaches_listener():
    listen = traps.start('127.0.0.1:0')
    try:
        port = int(listen.rsplit(':', 1)[1])
        assert send_snmp_trap('127.0.0.1', port, 'public', 'X1', TRAP_OID_LINKDOWN, 'Gi0/1 down')
        end = time.time() + 3
        while time.time() < end and not traps.snapshot():
            time.sleep(0.05)
        assert traps.snapshot()[-1]['iface'] == 'Gi0/1'
    finally:
        traps.stop()
        traps.clear()
