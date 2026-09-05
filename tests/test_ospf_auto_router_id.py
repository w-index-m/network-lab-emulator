"""
回帰テスト: OSPF router-id 無指定時のCisco仕様自動選出。

以前は `router-id` を明示指定しない限り、実際のインタフェース構成とは
無関係なハッシュベースの疑似IDが使われていた。実機と同じく、稼働中の
Loopbackインタフェースの最大IPを優先し、無ければ稼働中の他インタフェース
の最大IPを使うよう修正した。
"""

import os
import sys
import time

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.real_ospf_agent import SCAPY_AVAILABLE

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


def test_auto_router_id_prefers_loopback_over_physical_ip():
    _dev('t-arid-1', 'cisco')
    _run('t-arid-1', [
        'conf t', 'interface GigabitEthernet0/4',
        'ip address 198.51.100.13 255.255.255.252', 'no shutdown', 'exit',
        'interface Loopback9', 'ip address 9.9.9.9 255.255.255.255',
        'no shutdown', 'exit',
        'router ospf 1', 'network 198.51.100.12 0.0.0.3 area 0', 'end',
    ])
    out = _cli('t-arid-1', 'show ip protocols')
    assert 'Router ID 9.9.9.9' in out, out


def test_explicit_router_id_is_not_overridden_by_auto_selection():
    _dev('t-arid-2', 'cisco')
    _run('t-arid-2', [
        'conf t', 'router ospf 1', 'router-id 7.7.7.7', 'exit',
        'interface GigabitEthernet0/5',
        'ip address 198.51.100.17 255.255.255.252', 'no shutdown', 'exit',
        'interface Loopback10', 'ip address 10.10.10.10 255.255.255.255',
        'no shutdown', 'exit',
        'router ospf 1', 'network 198.51.100.16 0.0.0.3 area 0', 'end',
    ])
    out = _cli('t-arid-2', 'show ip protocols')
    assert 'Router ID 7.7.7.7' in out, out


@pytest.mark.skipif(not SCAPY_AVAILABLE,
                     reason="OSPF neighbor establishment needs the real "
                            "raw-socket listener, which requires scapy "
                            "(not installed in CI)")
def test_auto_router_id_is_seen_by_the_neighbor():
    _dev('t-arid-3', 'cisco')
    _dev('t-arid-4', 'catalyst')
    _link('t-arid-3', 't-arid-4', 'GigabitEthernet0/6', 'GigabitEthernet1/0/6')
    _run('t-arid-3', [
        'conf t', 'interface GigabitEthernet0/6',
        'ip address 198.51.100.21 255.255.255.252', 'no shutdown', 'exit',
        'interface Loopback11', 'ip address 11.11.11.11 255.255.255.255',
        'no shutdown', 'exit',
        'router ospf 1', 'network 198.51.100.20 0.0.0.3 area 0', 'end',
    ])
    _run('t-arid-4', [
        'conf t', 'interface GigabitEthernet1/0/6', 'no switchport',
        'ip address 198.51.100.22 255.255.255.252', 'no shutdown', 'exit',
        'router ospf 1', 'network 198.51.100.20 0.0.0.3 area 0', 'end',
    ])
    time.sleep(10)
    out = _cli('t-arid-4', 'show ip ospf neighbor')
    assert '11.11.11.11' in out, out
