"""
/cyber-view の経路 Info 表示(経路の追加・削除・変更)の回帰テスト

- 最初に見た装置は基準を取るだけ(起動直後に全経路が「追加」にならない)
- スタティック経路の追加/削除、OSPF で学習した経路の追加が /api/traffic/routes に出る
- OSPF の次ホップは router-id ではなく実IP(show ip route と同じ)
"""

import os
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.route_watch import RouteWatcher, describe, watcher  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    for cmd in cmds:
        c.post('/api/cli', json={'device_id': dev, 'command': cmd})


class TestWatcherUnit:
    def test_baseline_then_diff(self):
        w = RouteWatcher()
        c = {'network': '10.0.0.0', 'prefix': 24, 'next_hop': '0.0.0.0', 'source': 'connected', 'iface': 'Gi0/0'}
        assert w.observe('d', 'D', [c]) == []                       # 基準のみ
        s = {'network': '10.9.0.0', 'prefix': 16, 'next_hop': '10.0.0.2', 'source': 'static', 'iface': 'Gi0/0'}
        ev = w.observe('d', 'D', [c, s])
        assert [(e['kind'], e['prefix'], e['code'], e['next_hop']) for e in ev] == [('add', '10.9.0.0/16', 'S', '10.0.0.2')]
        assert describe(ev[0]) == 'S 10.9.0.0/16 via 10.0.0.2 [Gi0/0]'
        o = dict(s, source='ospf', next_hop='10.0.0.3')
        ev = w.observe('d', 'D', [c, o])
        assert ev[0]['kind'] == 'change' and ev[0]['old']['code'] == 'S' and ev[0]['code'] == 'O'
        ev = w.observe('d', 'D', [c])
        assert ev[0]['kind'] == 'del' and ev[0]['prefix'] == '10.9.0.0/16'
        assert [e['seq'] for e in w.since(1)] == [2, 3]


def test_static_route_add_and_delete_via_api(client):
    dev = f'rw-s-{_RUN}'
    client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': 'RW-S'})
    _cli(client, dev, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.248.1.1 255.255.255.0', 'no shutdown', 'end')
    app_module._route_watch_tick()
    seq = watcher.seq
    _cli(client, dev, 'configure terminal', 'ip route 10.248.99.0 255.255.255.0 10.248.1.2', 'end')
    app_module._route_watch_tick()
    evs = [e for e in client.get(f'/api/traffic/routes?since={seq}').json()['events'] if e['dev'] == dev]
    assert [(e['kind'], e['prefix'], e['code']) for e in evs] == [('add', '10.248.99.0/24', 'S')]
    assert evs[0]['text'].startswith('S 10.248.99.0/24 via 10.248.1.2') and evs[0]['host'] == 'RW-S'
    seq = watcher.seq
    _cli(client, dev, 'configure terminal', 'no ip route 10.248.99.0 255.255.255.0 10.248.1.2', 'end')
    app_module._route_watch_tick()
    evs = [e for e in client.get(f'/api/traffic/routes?since={seq}').json()['events'] if e['dev'] == dev]
    assert [(e['kind'], e['prefix']) for e in evs] == [('del', '10.248.99.0/24')]


def test_ospf_learned_route_is_reported_with_real_next_hop(client):
    r1, r2 = f'rw-o1-{_RUN}', f'rw-o2-{_RUN}'
    client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'RW-O1'})
    client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'RW-O2'})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    for d, ip in ((r1, '10.248.12.1'), (r2, '10.248.12.2')):
        _cli(client, d, 'configure terminal', 'interface GigabitEthernet0/0/0',
             f'ip address {ip} 255.255.255.252', 'no shutdown', 'end')
    _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/1',
         'ip address 10.248.20.1 255.255.255.0', 'no shutdown', 'end')
    app_module._route_watch_tick()
    seq = watcher.seq
    _cli(client, r1, 'configure terminal', 'router ospf 1', 'network 10.248.12.0 0.0.0.3 area 0', 'end')
    _cli(client, r2, 'configure terminal', 'router ospf 1', 'network 10.248.12.0 0.0.0.3 area 0',
         'network 10.248.20.0 0.0.0.255 area 0', 'end')
    end = time.time() + 30
    evs = []
    while time.time() < end:
        app_module._route_watch_tick()
        evs = [e for e in watcher.since(seq, 500) if e['dev'] == r1 and e['prefix'] == '10.248.20.0/24']
        if evs:
            break
        time.sleep(0.5)
    assert evs, 'OSPF で学習した 10.248.20.0/24 の追加が記録されていない'
    assert evs[0]['kind'] == 'add' and evs[0]['code'] == 'O' and evs[0]['next_hop'] == '10.248.12.2'
