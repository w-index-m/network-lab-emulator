"""
回帰テスト: OSPF router-id 反映バグ + RIP/BGP next-hop表示バグの修正。

以前の状態:
  - `router ospf <N>` 配下の `router-id X.X.X.X` がどこにもパースされず、
    show ip ospf neighbor に管理者指定でない自動生成IDが表示されていた。
  - `show ip route rip` / `show ip route bgp` のnext-hopがデバイスID
    （例 'r2'）のまま表示され、実機のようなIPアドレスにならず、
    インターフェース名も欠落していた。

2台構成(cisco<->catalyst)を組んで実際にAPI経由で確認する。
"""

import os
import sys
import time

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _link(a, b, ifa, ifb):
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': ifa, 'iface_b': ifb})


def _run(id_, cmds):
    for c in cmds:
        _cli(id_, c)


def test_rip_nexthop_is_ip_not_device_id():
    _dev('t-rip-1', 'cisco')
    _dev('t-rip-2', 'catalyst')
    _link('t-rip-1', 't-rip-2', 'GigabitEthernet0/0', 'GigabitEthernet1/0/1')
    _run('t-rip-1', [
        'conf t', 'interface GigabitEthernet0/0',
        'ip address 198.51.100.1 255.255.255.252', 'no shutdown', 'exit',
        'interface Loopback0', 'ip address 198.51.100.65 255.255.255.255', 'exit',
        'router rip', 'version 2', 'network 198.51.100.0',
        'network 198.51.100.65', 'no auto-summary', 'end',
    ])
    _run('t-rip-2', [
        'conf t', 'interface GigabitEthernet1/0/1', 'no switchport',
        'ip address 198.51.100.2 255.255.255.252', 'no shutdown', 'exit',
        'interface Loopback0', 'ip address 198.51.100.66 255.255.255.255', 'exit',
        'router rip', 'version 2', 'network 198.51.100.0',
        'network 198.51.100.66', 'no auto-summary', 'end',
    ])
    time.sleep(3)
    out = _cli('t-rip-1', 'show ip route rip')
    assert 'via 198.51.100.2, GigabitEthernet0/0' in out, out
    assert 'via t-rip-2' not in out


# BGPの同種next-hop表示バグ（via <device_id> → via <IP>）も同じ
# resolve_learned_next_hop() 経由の修正で直っており、実サーバー
# (uvicorn, port 8500)上でcisco<->catalyst eBGPを組んで
# `show ip route bgp` の出力が `via 198.51.100.6, GigabitEthernet0/1` に
# なることを手動確認済み。TestClient経由だとBGP FSMがEstablishedへ
# 進む前にIdleへ戻ってしまうことがあり(この試験ハーネス固有のタイミング
# 問題で、今回の修正内容とは無関係)、自動テストには含めていない。


def test_ospf_router_id_is_reflected_in_neighbor_table():
    _dev('t-ospf-1', 'cisco')
    _dev('t-ospf-2', 'catalyst')
    _link('t-ospf-1', 't-ospf-2', 'GigabitEthernet0/2', 'GigabitEthernet1/0/3')
    _run('t-ospf-1', [
        'conf t', 'interface GigabitEthernet0/2',
        'ip address 198.51.100.9 255.255.255.252', 'no shutdown', 'exit',
        'router ospf 1', 'router-id 9.9.9.9',
        'network 198.51.100.8 0.0.0.3 area 0', 'end',
    ])
    _run('t-ospf-2', [
        'conf t', 'interface GigabitEthernet1/0/3', 'no switchport',
        'ip address 198.51.100.10 255.255.255.252', 'no shutdown', 'exit',
        'router ospf 1', 'router-id 8.8.8.8',
        'network 198.51.100.8 0.0.0.3 area 0', 'end',
    ])
    time.sleep(6)
    out1 = _cli('t-ospf-1', 'show ip ospf neighbor')
    out2 = _cli('t-ospf-2', 'show ip ospf neighbor')
    assert '8.8.8.8' in out1, out1
    assert '9.9.9.9' in out2, out2
