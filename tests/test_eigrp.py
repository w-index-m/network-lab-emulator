"""
EIGRP（Catalyst / Cisco ルータ / Nexus）の実装に対する結合テスト。

2台構成を実際にAPI経由で組み、
  - ネイバーが上がるか
  - 経路が交換され show ip route に D として載るか
  - AS番号やKパラメータが違うときにネイバーが上がらないか
  - shutdown でネイバーが落ち、no shutdown で再確立するか
を確認する。

EIGRPは長らく「show コマンドだけあって実体が無い」状態だったため、
表示上は隣接して見えるのに通信できない、という食い違いが起きないよう
実エンジンの状態のみを出していることもあわせて確認する。
"""

import os
import sys
import time


os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.protocols import eigrp_classic_metric

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


def _wait(fn, timeout=12.0, interval=0.4):
    """条件が満たされるまで待つ。EIGRPのHelloは5秒間隔なので
    ネイバー確立には最大でその程度かかる。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _setup_pair(a, b, type_a, type_b, asn_a=100, asn_b=100, net='10.90.1'):
    _dev(a, type_a)
    _dev(b, type_b)
    _link(a, b, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
    for dev, ip, asn in ((a, f'{net}.1', asn_a), (b, f'{net}.2', asn_b)):
        _run(dev, [
            'conf t', 'interface GigabitEthernet0/0',
            f'ip address {ip} 255.255.255.0', 'no shutdown', 'exit',
            f'router eigrp {asn}', f'network {net}.0 0.0.0.255', 'end',
        ])


class TestEigrpMetric:
    def test_classic_metric_formula(self):
        # 256 × (10^7 / 帯域kbps + 遅延/10usec)
        # Gigabit(1000000kbps) + 遅延10usec → 256 × (10 + 1) = 2816
        assert eigrp_classic_metric(1_000_000, 1) == 2816
        # FastEthernet(100000kbps) + 遅延100usec → 256 × (100 + 10) = 28160
        assert eigrp_classic_metric(100_000, 10) == 28160

    def test_zero_bandwidth_is_unreachable(self):
        assert eigrp_classic_metric(0, 1) == 4294967295


class TestEigrpNeighbor:
    def test_neighbor_comes_up_between_cisco_and_catalyst(self):
        _setup_pair('eig-a', 'eig-b', 'cisco', 'catalyst', net='10.90.1')
        assert _wait(lambda: 'eig-b' in _cli('eig-a', 'show ip eigrp neighbors')
                     or '10.90.1.2' in _cli('eig-a', 'show ip eigrp neighbors')), \
            _cli('eig-a', 'show ip eigrp neighbors')
        out = _cli('eig-a', 'show ip eigrp neighbors')
        assert 'EIGRP-IPv4 Neighbors for AS(100)' in out
        assert '10.90.1.2' in out

    def test_not_configured_message_when_eigrp_absent(self):
        _dev('eig-none', 'cisco')
        out = _cli('eig-none', 'show ip eigrp neighbors')
        assert 'not configured' in out
        # 実体が無いのにサンプルのネイバーを出してはいけない
        assert '10.0.0' not in out

    def test_as_mismatch_prevents_adjacency(self):
        _setup_pair('eig-as1', 'eig-as2', 'cisco', 'cisco',
                    asn_a=100, asn_b=200, net='10.90.2')
        time.sleep(7)
        out = _cli('eig-as1', 'show ip eigrp neighbors')
        assert '10.90.2.2' not in out, f"AS不一致なのに隣接した: {out}"

    def test_k_value_mismatch_prevents_adjacency(self):
        _setup_pair('eig-k1', 'eig-k2', 'cisco', 'cisco', net='10.90.3')
        _run('eig-k2', ['conf t', 'router eigrp 100',
                        'metric weights 0 1 1 1 0 0', 'end'])
        time.sleep(7)
        out = _cli('eig-k1', 'show ip eigrp neighbors')
        assert '10.90.3.2' not in out, f"Kパラメータ不一致なのに隣接した: {out}"


class TestEigrpRouteExchange:
    def test_route_learned_and_shown_as_D(self):
        _setup_pair('eig-r1', 'eig-r2', 'cisco', 'cisco', net='10.91.1')
        # eig-r2 側にだけ存在するネットワークを広告させる
        _run('eig-r2', [
            'conf t', 'interface Loopback9',
            'ip address 172.30.9.1 255.255.255.0', 'exit',
            'router eigrp 100', 'network 172.30.9.0 0.0.0.255', 'end',
        ])
        assert _wait(lambda: '172.30.9.0' in _cli('eig-r1', 'show ip route')), \
            _cli('eig-r1', 'show ip route')
        out = _cli('eig-r1', 'show ip route')
        line = [ln for ln in out.splitlines() if '172.30.9.0' in ln][0]
        assert line.startswith('D'), f"EIGRP経路がDで表示されていない: {line}"
        # AD 90 で載ること
        assert '[90/' in line, line
        # next-hopはデバイスIDではなく実IPであること
        assert 'eig-r2' not in line, f"next-hopがデバイスIDのまま: {line}"
        assert '10.91.1.2' in line, line

    def test_topology_table_shows_fd_and_rd(self):
        _setup_pair('eig-t1', 'eig-t2', 'cisco', 'cisco', net='10.91.2')
        _run('eig-t2', [
            'conf t', 'interface Loopback9',
            'ip address 172.31.9.1 255.255.255.0', 'exit',
            'router eigrp 100', 'network 172.31.9.0 0.0.0.255', 'end',
        ])
        assert _wait(lambda: '172.31.9.0' in _cli('eig-t1', 'show ip eigrp topology'))
        out = _cli('eig-t1', 'show ip eigrp topology')
        assert 'Topology Table for AS(100)' in out
        assert 'successors, FD is' in out
        # 学習経路は via <IP> (FD/RD) の形式
        assert '(' in out and '/' in out


class TestEigrpInterfaceFlap:
    def test_neighbor_drops_on_shutdown_and_returns_on_no_shutdown(self):
        _setup_pair('eig-f1', 'eig-f2', 'cisco', 'cisco', net='10.92.1')
        assert _wait(lambda: '10.92.1.2' in _cli('eig-f1', 'show ip eigrp neighbors')), \
            "初期のネイバーが上がらなかった"

        _run('eig-f1', ['conf t', 'interface GigabitEthernet0/0', 'shutdown', 'end'])
        assert _wait(lambda: '10.92.1.2' not in _cli('eig-f1', 'show ip eigrp neighbors'),
                     timeout=8), \
            f"shutdownしたのにネイバーが残っている: {_cli('eig-f1', 'show ip eigrp neighbors')}"

        _run('eig-f1', ['conf t', 'interface GigabitEthernet0/0', 'no shutdown', 'end'])
        assert _wait(lambda: '10.92.1.2' in _cli('eig-f1', 'show ip eigrp neighbors')), \
            f"no shutdown後に再確立しない: {_cli('eig-f1', 'show ip eigrp neighbors')}"


class TestEigrpNexus:
    def test_nexus_requires_feature_eigrp(self):
        _dev('eig-nx', 'nexus')
        out = _cli('eig-nx', 'conf t')
        out = _cli('eig-nx', 'router eigrp 100')
        assert 'feature eigrp' in out, out

    def test_nexus_works_after_feature_enabled(self):
        _dev('eig-nx2', 'nexus')
        _dev('eig-nx3', 'nexus')
        _link('eig-nx2', 'eig-nx3', 'Ethernet1/1', 'Ethernet1/1')
        for dev, ip in (('eig-nx2', '10.93.1.1'), ('eig-nx3', '10.93.1.2')):
            _run(dev, [
                'conf t', 'feature eigrp', 'interface Ethernet1/1',
                'no switchport', f'ip address {ip}/24', 'no shutdown', 'exit',
                'router eigrp 100', 'network 10.93.1.0 0.0.0.255', 'end',
            ])
        assert _wait(lambda: '10.93.1.2' in _cli('eig-nx2', 'show ip eigrp neighbors')), \
            _cli('eig-nx2', 'show ip eigrp neighbors')
