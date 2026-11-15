"""
CyNote風 侵害検知 + トラフィック再現 (engine/traffic_analytics.py,
GET /api/traffic/history, GET /api/traffic/alerts) の回帰テスト
"""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.traffic_analytics import RULES, FlowRecorder  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


def _flow(rec, src='a', dst_ip='10.0.0.2', ok=True, pkts=5, size=100, t=1000.0, path=None):
    return rec.record_flow(src, dst_ip, None, path or [src], ok, pkts, size, now=t)


class TestDetector:
    def test_scan_needs_threshold_distinct_destinations(self):
        rec = FlowRecorder()
        n = RULES['scan']['threshold']
        for i in range(n - 1):
            _flow(rec, dst_ip=f'172.16.0.{i}', t=1000 + i)
        assert not [a for a in rec.alerts if a['rule'] == 'scan']
        _flow(rec, dst_ip='172.16.0.99', t=1000 + n)
        a = [a for a in rec.alerts if a['rule'] == 'scan']
        assert len(a) == 1 and a[0]['src'] == 'a' and len(a[0]['related']) == n

    def test_same_destination_repeated_is_not_scan(self):
        rec = FlowRecorder()
        for i in range(30):
            _flow(rec, t=1000 + i)
        assert not rec.alerts

    def test_scan_window_expires(self):
        rec = FlowRecorder()
        w = RULES['scan']['window']
        for i in range(RULES['scan']['threshold']):
            _flow(rec, dst_ip=f'172.16.0.{i}', t=1000 + i * (w // 2))   # 窓に収まらない間隔
        assert not [a for a in rec.alerts if a['rule'] == 'scan']

    def test_unreach_burst(self):
        rec = FlowRecorder()
        for i in range(RULES['unreach_burst']['threshold']):
            _flow(rec, ok=False, t=1000 + i)
        assert [a['rule'] for a in rec.alerts] == ['unreach_burst']

    def test_flood_by_bytes_and_cooldown(self):
        rec = FlowRecorder()
        big = RULES['flood']['threshold'] // 1500 + 1
        ev = _flow(rec, pkts=big, size=1500, t=1000)
        assert [a['rule'] for a in rec.alerts] == ['flood'] and ev['alerts'] == [1]
        _flow(rec, pkts=big, size=1500, t=1010)                     # クールダウン中
        assert len(rec.alerts) == 1
        _flow(rec, pkts=big, size=1500, t=1000 + RULES['flood']['cooldown'] + 1)
        assert len(rec.alerts) == 2

    def test_large_icmp(self):
        rec = FlowRecorder()
        _flow(rec, size=1500, t=1000)
        assert not rec.alerts                                       # MTU ちょうどは対象外
        _flow(rec, size=9000, t=1001)
        assert [a['rule'] for a in rec.alerts] == ['large_icmp']

    def test_link_down_alert_and_events(self):
        rec = FlowRecorder()
        rec.record_link('r1', 'r2', 'Gi0/0/0', 'down', now=1000)
        rec.record_link('r1', 'r2', 'Gi0/0/0', 'up', now=1005)
        assert [a['rule'] for a in rec.alerts] == ['link_down']
        assert [x['state'] for x in rec.link_events] == ['down', 'up']


class TestQuery:
    @pytest.fixture
    def rec(self):
        rec = FlowRecorder()
        _flow(rec, src='a', dst_ip='10.0.0.2', ok=True, t=1000)
        _flow(rec, src='b', dst_ip='10.0.0.3', ok=False, t=1010)
        _flow(rec, src='a', dst_ip='10.0.0.3', ok=True, t=1020)
        rec.record_link('a', 'b', 'Gi0', 'down', now=1005)
        rec.record_link('a', 'b', 'Gi0', 'up', now=1030)
        return rec

    def test_time_window(self, rec):
        d = rec.query(1005, 1015)
        assert [e['t'] for e in d['events']] == [1010]
        # リンク状態は「期間開始時点の状態」を作れるよう end までの全件
        assert [x['state'] for x in d['link_events']] == ['down']

    def test_filters(self, rec):
        assert [e['t'] for e in rec.query(src='a')['events']] == [1000, 1020]
        assert [e['t'] for e in rec.query(dst='10.0.0.3')['events']] == [1010, 1020]
        assert [e['t'] for e in rec.query(result='ng')['events']] == [1010]
        assert [e['t'] for e in rec.query(result='ok')['events']] == [1000, 1020]

    def test_limit_marks_truncated(self, rec):
        d = rec.query(limit=2)
        assert d['truncated'] and [e['t'] for e in d['events']] == [1010, 1020]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


class TestApi:
    def test_history_validation(self, client):
        assert client.get('/api/traffic/history?result=xx').status_code == 400
        assert client.get('/api/traffic/history?start=20&end=10').status_code == 400

    def test_end_to_end_scan_shutdown_and_replay_query(self, client):
        r1, r2 = f'ta-r1-{_RUN}', f'ta-r2-{_RUN}'
        client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'TA-R1'})
        client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'TA-R2'})
        client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                       'iface_b': 'GigabitEthernet0/0/0'})
        _cli(client, r1, 'configure terminal', 'interface GigabitEthernet0/0/0',
             'ip address 10.243.12.1 255.255.255.252', 'no shutdown', 'end')
        _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/0',
             'ip address 10.243.12.2 255.255.255.252', 'no shutdown', 'end')
        aseq = client.get('/api/traffic/alerts').json()['alert_seq']
        import time
        t0 = time.time()
        for i in range(RULES['scan']['threshold']):
            _cli(client, r1, f'ping 172.31.{i}.1')                 # 経路なし → 到達不可スイープ
        _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end')
        _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', 'no shutdown', 'end')
        new = client.get(f'/api/traffic/alerts?since={aseq}').json()['alerts']
        rules = {a['rule'] for a in new if a['src'] in (r1, r2)}
        assert {'scan', 'unreach_burst', 'link_down'} <= rules
        # flowmap もアラートを差分で返す
        fm = client.get(f'/api/traffic/flowmap?alert_since={aseq}').json()
        assert {a['seq'] for a in new} <= {a['seq'] for a in fm['alerts']}
        # 再現: 期間 + 送信元 + 到達不可で絞り込み
        h = client.get(f'/api/traffic/history?start={t0 - 1}&end={time.time() + 1}'
                       f'&src={r1}&result=ng').json()
        assert len(h['events']) == RULES['scan']['threshold']
        assert all(not e['ok'] and e['src'] == r1 for e in h['events'])
        states = [x['state'] for x in h['link_events'] if x['a'] == r2 and x['t'] >= t0]
        assert states == ['down', 'up']
        assert {'scan', 'link_down'} <= {a['rule'] for a in h['alerts']}

    def test_rules_exposed(self, client):
        rules = client.get('/api/traffic/alerts').json()['rules']
        assert set(rules) == set(RULES) and rules['scan']['threshold'] == RULES['scan']['threshold']
