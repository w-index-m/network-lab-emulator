"""
ルータ間リンクのトラフィック/状態 (/api/traffic/links, /cyber-view の ROUTER LINKS) の回帰テスト

- 内部IFカウンタのサンプリングから window 平均の bps を出す
- 状態: IF断 / 使用率 / リンクを通ったpingの失敗 / 両端のアラート
- PROMETHEUS_URL があれば snmp_exporter の rate(ifHCOutOctets[1m]) を優先、
  不達なら内部カウンタにフォールバックしてエラーを返す
- 端末(pc)とのリンクは既定で除外
"""

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.link_stats import LinkSampler, classify  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


@pytest.fixture(scope='module')
def lab(client):
    """r1 Gi0/0/0 ─ r2 Gi0/0/0 (10.244.12.0/30), r1 Gi0/0/1 ─ pc"""
    r1, r2, pc = f'rl-r1-{_RUN}', f'rl-r2-{_RUN}', f'rl-pc-{_RUN}'
    client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'RL-R1'})
    client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'RL-R2'})
    client.post('/api/device', json={'id': pc, 'type': 'pc', 'hostname': 'RL-PC'})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    client.post('/api/link', json={'a': r1, 'b': pc, 'iface_a': 'GigabitEthernet0/0/1', 'iface_b': 'eth0'})
    _cli(client, r1, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'ip address 10.244.12.1 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.244.1.1 255.255.255.0', 'no shutdown', 'end')
    _cli(client, r2, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'ip address 10.244.12.2 255.255.255.252', 'no shutdown', 'end')
    return client, r1, r2, pc


def _link(c, a, b, **params):
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    d = c.get('/api/traffic/links' + (f'?{q}' if q else '')).json()
    return d, next((x for x in d['links'] if {x['a'], x['b']} == {a, b}), None)


class TestSampler:
    def test_rate_uses_window(self):
        s = LinkSampler()
        for t, ab, ba in ((0, 0, 0), (30, 3_000_000, 0), (60, 7_500_000, 750_000), (70, 7_500_000, 750_000)):
            s.sample([{'a': 'x', 'b': 'y', 'ab_bytes': ab, 'ba_bytes': ba}], now=t)
        ab, ba = s.rate(('x', 'y'), 60, now=70)
        # 60秒前に最も近い古いサンプル(t=0)から t=70 までの平均
        assert round(ab) == round(7_500_000 * 8 / 70) and round(ba) == round(750_000 * 8 / 70)
        ab10, _ = s.rate(('x', 'y'), 10, now=70)
        assert ab10 == 0                     # 直近10秒は増えていない
        assert s.rate(('nope', 'y'), 60) is None

    def test_counter_reset_is_not_negative(self):
        s = LinkSampler()
        s.sample([{'a': 'x', 'b': 'y', 'ab_bytes': 1000, 'ba_bytes': 1000}], now=0)
        s.sample([{'a': 'x', 'b': 'y', 'ab_bytes': 10, 'ba_bytes': 10}], now=10)
        assert s.rate(('x', 'y'), 60) == (0, 0)

    def test_classify(self):
        assert classify(0.1, True, 0, []) == 'down'
        assert classify(0.85, False, 0, []) == 'crit'
        assert classify(0.6, False, 0, []) == 'warn'
        assert classify(0.0, False, 0, []) == 'ok'
        # リンクの状態はリンク自体(断・使用率)だけで決める。35Mbps/1Gbps は正常、
        # 両端のアラートや経由pingの失敗ではリンクを警告にしない(別表示)
        assert classify(0.035, False, 0, []) == 'ok'
        assert classify(0.1, False, 0, [{'rule': 'flood'}]) == 'ok'
        assert classify(0.0, False, 2, []) == 'ok'


class TestLinksApi:
    def test_internal_bps_and_ok(self, lab, monkeypatch):
        c, r1, r2, _pc = lab
        monkeypatch.delenv('PROMETHEUS_URL', raising=False)
        _link(c, r1, r2)                                  # 基準サンプル
        _cli(c, r1, 'ping 10.244.12.2 repeat 100 size 1500')
        d, ln = _link(c, r1, r2, window=60)
        assert d['source'] == 'internal' and ln['source'] == 'internal'
        fwd = ln['ab_bps'] if ln['a'] == r1 else ln['ba_bps']
        rev = ln['ba_bps'] if ln['a'] == r1 else ln['ab_bps']
        assert fwd > 0 and rev > 0                        # Echo と Reply の両方向
        assert ln['state'] == 'ok' and ln['flows_ok'] >= 1 and ln['flows_ng'] == 0
        assert {ln['host_a'], ln['host_b']} == {'RL-R1', 'RL-R2'}

    def test_terminal_links_excluded_by_default(self, lab):
        c, r1, _r2, pc = lab
        assert _link(c, r1, pc)[1] is None
        assert _link(c, r1, pc, routers_only='false')[1] is not None

    def test_shutdown_is_down(self, lab):
        c, r1, r2, _pc = lab
        _cli(c, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end')
        try:
            _d, ln = _link(c, r1, r2)
            assert ln['down'] is True and ln['state'] == 'down'
        finally:
            _cli(c, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', 'no shutdown', 'end')

    def test_failed_ping_across_link_is_counted_but_link_stays_ok(self, lab):
        c, r1, r2, _pc = lab
        # r2 には 10.244.1.0/24 への戻り経路が無い → r1 から r2 経由の宛先は失敗
        _cli(c, r1, 'configure terminal', 'ip route 10.244.99.0 255.255.255.0 10.244.12.2', 'end')
        _cli(c, r1, 'ping 10.244.99.9')
        _d, ln = _link(c, r1, r2)
        assert ln['flows_ng'] >= 1 and ln['state'] == 'ok'

    def test_window_is_clamped(self, lab):
        c, _r1, _r2, _pc = lab
        assert c.get('/api/traffic/links?window=1').json()['window'] == 10
        assert c.get('/api/traffic/links?window=99999').json()['window'] == 3600


class _FakeProm(BaseHTTPRequestHandler):
    result = []

    def do_GET(self):  # noqa: N802
        body = json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': self.result}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestPrometheusSource:
    def test_prometheus_rates_are_preferred(self, lab, monkeypatch):
        c, r1, r2, _pc = lab
        _FakeProm.result = [
            {'metric': {'instance': '10.244.12.1', 'ifName': 'Gi0/0/0', 'job': 'cisco-snmp'}, 'value': [0, '600000000']},
            {'metric': {'instance': '10.244.12.2', 'ifName': 'Gi0/0/0', 'job': 'cisco-snmp'}, 'value': [0, '1000']},
        ]
        srv = HTTPServer(('127.0.0.1', 0), _FakeProm)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            monkeypatch.setenv('PROMETHEUS_URL', f'http://127.0.0.1:{srv.server_port}')
            d, ln = _link(c, r1, r2)
            assert d['source'] == 'prometheus' and ln['source'] == 'prometheus'
            fwd = ln['ab_bps'] if ln['a'] == r1 else ln['ba_bps']
            rev = ln['ba_bps'] if ln['a'] == r1 else ln['ab_bps']
            assert fwd == 600000000 and rev == 1000
            assert abs(ln['util'] - 0.6) < 1e-9
            assert ln['state'] == 'warn'                  # 使用率60%
        finally:
            srv.shutdown()

    def test_unreachable_prometheus_falls_back(self, lab, monkeypatch):
        c, r1, r2, _pc = lab
        monkeypatch.setenv('PROMETHEUS_URL', 'http://127.0.0.1:9')
        d, ln = _link(c, r1, r2)
        assert d['source'] == 'internal' and d['prometheus_error']
        assert ln['source'] == 'internal'


def test_page_has_router_links_mode():
    page = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert 'ROUTER LINKS' in page and '/api/traffic/links?window=' in page
