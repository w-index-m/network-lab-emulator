"""OSPF のコストを実機(IOS)と同じ決め方にした修正の回帰テスト。

以前はコストがルータ単位で一律 10、さらに SPF が DR の Network LSA を
たどるときだけ +1 していたため、Catalyst 3台の三角形で [110/20] と
[110/11] が混在していた(実機は GigabitEthernet も Vlan も cost 1 なので
相手の LAN は [110/2])。
"""
import os
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.protocols import OspfEngine, ospf_engine, vnet  # noqa: E402


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c

_RUN = uuid.uuid4().hex[:6]


def _id(d):
    return f'{d}-{_RUN}'


def _cli(c, d, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': _id(d), 'command': cmd}).json()['output']
    return out


def _ospf_routes(c, d):
    return {m.group(1): int(m.group(2)) for m in re.finditer(
        r'^O\s+(\S+) \[110/(\d+)\]', _cli(c, d, 'show ip route'), re.M)}


def _wait(pred, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.5)
    return False


def _triangle(c, tag):
    """CAT1-CAT2-CAT3 の三角形(L3 ポート)+ 各 Vlan10。サブネットはテストごとに別"""
    base = {'a': 61, 'b': 62, 'c': 63, 'd': 64, 'e': 65, 'f': 66}[tag]
    devs = {f'{tag}1': 1, f'{tag}2': 2, f'{tag}3': 3}
    names = list(devs)
    for d in names:
        c.post('/api/device', json={'id': _id(d), 'type': 'catalyst', 'hostname': d.upper()})
    links = [(names[0], 'GigabitEthernet1/0/1', names[1], 'GigabitEthernet1/0/2', 12),
             (names[1], 'GigabitEthernet1/0/1', names[2], 'GigabitEthernet1/0/2', 23),
             (names[2], 'GigabitEthernet1/0/1', names[0], 'GigabitEthernet1/0/2', 13)]
    nets = {d: [] for d in names}
    for a, ia, b, ib, n in links:
        c.post('/api/link', json={'a': _id(a), 'b': _id(b), 'iface_a': ia, 'iface_b': ib})
        lo = min(a, b)
        for d, i in ((a, ia), (b, ib)):
            ip = f'10.{base}.{n}.{1 if d == lo else 2}'
            _cli(c, d, 'configure terminal', f'interface {i}', 'no switchport',
                 f'ip address {ip} 255.255.255.252', 'no shutdown', 'end')
            nets[d].append(f'10.{base}.{n}.0 0.0.0.3')
    for d, k in devs.items():
        _cli(c, d, 'configure terminal', 'interface Vlan10',
             f'ip address 172.{base - 40}.{k}.1 255.255.255.0', 'no shutdown', 'end')
        nets[d].append(f'172.{base - 40}.{k}.0 0.0.0.255')
        _cli(c, d, 'configure terminal', 'ip routing', 'router ospf 1',
             f'router-id {k}.{k}.{k}.{k}', *[f'network {x} area 0' for x in nets[d]], 'end')
    assert _wait(lambda: all(len(_ospf_routes(c, d)) == 3 for d in names)), \
        {d: _ospf_routes(c, d) for d in names}
    return names, base


def test_gigabit_and_vlan_cost_1_gives_110_2_everywhere(client):
    names, base = _triangle(client, 'a')
    # 隣接が揃う途中は遠回り(3)が一時的に見えるので、収束を待つ
    assert _wait(lambda: all(set(_ospf_routes(client, d).values()) == {2} for d in names)), \
        {d: _ospf_routes(client, d) for d in names}   # 以前は 20 と 11 が混在


def test_ip_ospf_cost_is_per_interface_and_reroutes(client):
    (r1, r2, r3), base = _triangle(client, 'b')
    lan2 = f'172.{base - 40}.2.0/24'
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet1/0/1', 'ip ospf cost 50', 'end')
    # R1→R2 直接(50+1)より R1→R3→R2(1+1+1)が安い
    assert _wait(lambda: _ospf_routes(client, r1).get(lan2) == 3)
    out = _cli(client, r1, 'show ip ospf interface')
    assert re.search(r'GigabitEthernet1/0/1 is up.*?Cost: 50', out, re.S)
    assert re.search(r'GigabitEthernet1/0/2 is up.*?Cost: 1\b', out, re.S)
    assert ' ip ospf cost 50' in _cli(client, r1, 'show running-config')
    # ニルヴァーナの OSPF 画面(/api/traffic/ospf)も両端のIFコストを返す
    adj = client.get('/api/traffic/ospf').json()['adjacencies']
    a12 = next(a for a in adj if {a['a'], a['b']} == {_id(r1), _id(r2)})
    end1 = 'cost_a' if a12['a'] == _id(r1) else 'cost_b'
    end2 = 'cost_b' if end1 == 'cost_a' else 'cost_a'
    assert (a12[end1], a12[end2]) == (50, 1)
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet1/0/1', 'no ip ospf cost', 'end')
    assert _wait(lambda: _ospf_routes(client, r1).get(lan2) == 2)
    assert 'ip ospf cost' not in _cli(client, r1, 'show running-config')


def test_bandwidth_changes_cost(client):
    (r1, r2, r3), base = _triangle(client, 'c')
    lan3 = f'172.{base - 40}.3.0/24'
    # R2 の R3 向け Gi1/0/1 を 10Mbps 扱い → 100000/10000 = cost 10、R1 経由(3)に切り替わる
    _cli(client, r2, 'configure terminal', 'interface GigabitEthernet1/0/1', 'bandwidth 10000', 'end')
    assert _wait(lambda: _ospf_routes(client, r2).get(lan3) == 3)
    assert ' bandwidth 10000' in _cli(client, r2, 'show running-config')


def test_auto_cost_reference_bandwidth(client):
    (r1, r2, r3), base = _triangle(client, 'd')
    out = _cli(client, r3, 'configure terminal', 'router ospf 1', 'auto-cost reference-bandwidth 10000')
    assert 'Reference bandwidth is changed' in out
    _cli(client, r3, 'end')
    # 1Gbps の IF が 10000Mbps/1000Mbps = cost 10 になり、相手の LAN は 10+1
    assert _wait(lambda: set(_ospf_routes(client, r3).values()) == {11})
    assert ' auto-cost reference-bandwidth 10000' in _cli(client, r3, 'show running-config')


def test_bandwidth_table_matches_ios_defaults():
    bw = OspfEngine.if_bandwidth_kbps
    assert bw('cisco', 'GigabitEthernet0/0/0') == 1_000_000
    assert bw('catalyst', 'TenGigabitEthernet1/1/1') == 10_000_000
    assert bw('catalyst', 'Vlan10') == 1_000_000
    assert bw('cisco', 'FastEthernet0/1') == 100_000
    assert bw('cisco', 'Serial0/0/0') == 1544
    assert bw('cisco', 'Loopback0') == 8_000_000
    assert bw('nexus', 'Ethernet1/1') == 10_000_000


def test_vendor_defaults():
    d_ios, d_nx, d_eos = (f'cost-ios-{_RUN}', f'cost-nx-{_RUN}', f'cost-eos-{_RUN}')
    for d, t in ((d_ios, 'cisco'), (d_nx, 'nexus'), (d_eos, 'arista')):
        vnet.device_types[d] = t
        ospf_engine._node(d)
    try:
        assert ospf_engine.cost_for_iface(d_ios, 'GigabitEthernet0/0/0') == 1
        assert ospf_engine.cost_for_iface(d_ios, 'FastEthernet0/1') == 1
        assert ospf_engine.cost_for_iface(d_ios, 'Serial0/0/0') == 64
        assert ospf_engine.cost_for_iface(d_ios, 'Tunnel0') == 1000
        assert ospf_engine.cost_for_iface(d_nx, 'Ethernet1/1') == 4    # NX-OS は参照帯域 40G
        assert ospf_engine.cost_for_iface(d_eos, 'Ethernet1') == 10    # EOS の既定
        ospf_engine.set_cost(d_ios, 30)                                 # IF外の ip ospf cost
        assert ospf_engine.cost_for_iface(d_ios, 'GigabitEthernet0/0/0') == 30
    finally:
        for d in (d_ios, d_nx, d_eos):
            ospf_engine.nodes.pop(d, None)
            vnet.device_types.pop(d, None)
