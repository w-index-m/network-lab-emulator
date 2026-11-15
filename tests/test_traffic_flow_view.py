"""
NIRVANA風トラフィック可視化 (/traffic-flow, GET /api/traffic/flowmap) の回帰テスト

- ping ごとに経路(多段ホップ)・バイト数・成否のフローイベントが記録される
- 直結の宛先も経路に入り、カウンタは経路上のリンクにだけ載る
- 次ホップ側のIFをshutdownすると、その先のネットワークへも届かない
  (以前はゲートウェイ経由の次ホップ解決だけリンク断を見ておらず成功していた)
- ページは認証不要で開け、APIは dashboard と同じトークンキーで認証する
"""

import os
import re
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402

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
    """r1 ─ r2 ─ r3 (10.242.12.0/30, 10.242.23.0/30)、r3 LAN 10.242.3.0/24"""
    r1, r2, r3 = (f'tf-r{i}-{_RUN}' for i in (1, 2, 3))
    for d, h in ((r1, 'TF-R1'), (r2, 'TF-R2'), (r3, 'TF-R3')):
        client.post('/api/device', json={'id': d, 'type': 'cisco', 'hostname': h})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    client.post('/api/link', json={'a': r2, 'b': r3, 'iface_a': 'GigabitEthernet0/0/2',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.242.12.1 255.255.255.252', 'no shutdown', 'exit',
         'ip route 10.242.3.0 255.255.255.0 10.242.12.2', 'end')
    _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.242.12.2 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/2', 'ip address 10.242.23.1 255.255.255.252', 'no shutdown', 'exit',
         'ip route 10.242.3.0 255.255.255.0 10.242.23.2', 'end')
    _cli(client, r3, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.242.23.2 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.242.3.1 255.255.255.0', 'no shutdown', 'exit',
         'ip route 0.0.0.0 0.0.0.0 10.242.23.1', 'end')
    return client, r1, r2, r3


def _flowmap(c, since=0):
    return c.get(f'/api/traffic/flowmap?since={since}').json()


def _link(fm, a, b):
    for lk in fm['links']:
        if {lk['a'], lk['b']} == {a, b}:
            return lk
    raise AssertionError(f'link {a}-{b} not found')


def _bytes(lk, src, dst):
    return lk['ab_bytes'] if (lk['a'], lk['b']) == (src, dst) else lk['ba_bytes']


class TestFlowmap:
    def test_multi_hop_event_and_counters(self, lab):
        c, r1, r2, r3 = lab
        seq = _flowmap(c)['seq']
        before = _flowmap(c)
        out = _cli(c, r1, 'ping 10.242.3.1 repeat 10 size 1000')
        assert '(10/10)' in out
        fm = _flowmap(c, seq)
        ev = [e for e in fm['events'] if e['src'] == r1][-1]
        assert ev['path'] == [r1, r2, r3] and ev['ok'] and ev['bytes'] == 10000 and ev['dst'] == r3
        assert {n['id'] for n in fm['nodes']} >= {r1, r2, r3}
        for x, y in ((r1, r2), (r2, r3)):
            for s, d in ((x, y), (y, x)):        # 往路と復路
                diff = _bytes(_link(fm, x, y), s, d) - _bytes(_link(before, x, y), s, d)
                assert diff == 10000, (s, d, diff)

    def test_direct_neighbor_path_and_no_spill(self, lab):
        c, r1, r2, r3 = lab
        seq = _flowmap(c)['seq']
        before = _link(_flowmap(c), r2, r3)
        _cli(c, r2, 'ping 10.242.12.1 repeat 20 size 500')
        fm = _flowmap(c, seq)
        assert fm['events'][-1]['path'] == [r2, r1]
        after = _link(fm, r2, r3)
        # r2→r1 の ping で無関係な r2-r3 リンクは増えない
        assert (after['ab_bytes'], after['ba_bytes']) == (before['ab_bytes'], before['ba_bytes'])

    def test_unreachable_event(self, lab):
        c, r1, _r2, _r3 = lab
        seq = _flowmap(c)['seq']
        _cli(c, r1, 'ping 10.199.9.9')
        ev = _flowmap(c, seq)['events'][-1]
        assert ev['ok'] is False and ev['path'][0] == r1

    def test_since_returns_only_new_events(self, lab):
        c, r1, _r2, _r3 = lab
        seq = _flowmap(c)['seq']
        assert _flowmap(c, seq)['events'] == []
        _cli(c, r1, 'ping 10.242.12.2')
        assert len(_flowmap(c, seq)['events']) == 1


class TestShutdownBlocksForwarding:
    def test_far_side_shutdown_blocks_beyond_next_hop(self, lab):
        c, r1, r2, r3 = lab
        assert '(5/5)' in _cli(c, r1, 'ping 10.242.3.1')
        _cli(c, r3, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end')
        try:
            assert _link(_flowmap(c), r2, r3)['down'] is True
            # 次ホップ(r3 Gi0/0/0)が落ちているので、その先の r3 LAN にも届かない
            assert 'Success rate is 0 percent' in _cli(c, r2, 'ping 10.242.3.1')
            assert 'Success rate is 0 percent' in _cli(c, r1, 'ping 10.242.3.1')
        finally:
            _cli(c, r3, 'configure terminal', 'interface GigabitEthernet0/0/0', 'no shutdown', 'end')
        assert '(5/5)' in _cli(c, r1, 'ping 10.242.3.1')


class TestPage:
    def test_page_served_and_auth_exempt(self, client):
        r = client.get('/traffic-flow')
        assert r.status_code == 200 and 'TRAFFIC FLOW' in r.text
        assert '/traffic-flow' in app_module._NO_AUTH_PATHS

    def test_token_key_matches_index(self):
        def key(name):
            src = open(os.path.join(ROOT, 'static', name), encoding='utf-8').read()
            m = re.search(r"SESSION_KEY\s*=\s*'([^']+)'", src) or re.search(r"'(netlabToken_v\d+)'", src)
            return m.group(1)
        assert key('traffic_flow.html') == key('index.html')
