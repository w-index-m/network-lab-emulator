"""
実OSPFリスナーのネイバー表示が実態と合っているかのテスト

Si-R / Nexus に RouteInjector から経路を注入して確認した際に見つかった
食い違いへの回帰テスト。手順は docs/ospf-route-injection-howto.md 参照。
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from engine.protocols import RibEngine, icmp_engine


# ── ① OSPFを喋らせるインターフェースの選択 ────────────────

class _FakeState:
    def __init__(self, interfaces):
        self.interfaces = interfaces


def _pick(interfaces, networks):
    from engine.real_ospf_agent import _pick_ospf_ip
    return _pick_ospf_ip(_FakeState(interfaces), {'networks': networks})


def test_ospf_ip_prefers_the_configured_network_over_mgmt():
    """mgmt0を持つNexusでも、OSPFのセグメント側のIPで待ち受ける

    以前は管理IP優先の _pick_management_ip をそのまま使っていたため、
    Nexusの実リスナーが mgmt0 のアドレスに張り付き、OSPFセグメントの
    Helloを一切受け取れずネイバーが上がらなかった。
    """
    ifaces = {
        'mgmt0': {'ip': '192.168.100.1', 'prefix': 24},
        'GigabitEthernet0/0/1': {'ip': '10.30.30.1', 'prefix': 24},
    }
    assert _pick(ifaces, ['10.30.30.0/24']) == '10.30.30.1'


def test_ospf_ip_falls_back_to_management_when_network_unknown():
    """networkが未設定なら従来どおり管理IPを使う"""
    ifaces = {
        'mgmt0': {'ip': '192.168.100.1', 'prefix': 24},
        'GigabitEthernet0/0/1': {'ip': '10.30.30.1', 'prefix': 24},
    }
    assert _pick(ifaces, []) == '192.168.100.1'


def test_ospf_ip_ignores_interfaces_outside_the_network():
    ifaces = {'Vlan10': {'ip': '192.168.10.1', 'prefix': 24}}
    assert _pick(ifaces, ['10.30.30.0/24']) == '192.168.10.1'  # fallback


# ── ② セグメントの違う相手をネイバーにしない ──────────────

def _same_subnet(my_ip, mask, src):
    from engine.real_ospf_agent import DeviceOspfResponder
    stub = types.SimpleNamespace(my_ip=my_ip, mask=mask)
    return DeviceOspfResponder._same_subnet(stub, src)


def test_hello_from_another_segment_is_rejected():
    """全装置が lo を共有しているため、他セグメントのHelloも届いてしまう

    実機のOSPFは同一セグメントの相手としか隣接しない。この判定が無いと
    10.20.20.0/24 の Si-R が 10.30.30.0/24 の Nexus をネイバーとして
    表示していた（実際には繋がっていない相手が出る）。
    """
    assert not _same_subnet('10.20.20.1', '255.255.255.0', '10.30.30.1')


def test_hello_from_same_segment_is_accepted():
    assert _same_subnet('10.20.20.1', '255.255.255.0', '10.20.20.77')


# ── ③ 動的経路の出力インターフェース ──────────────────────

@pytest.fixture
def rib_nexus():
    icmp_engine.register_device('nx', 'N9K', {
        'mgmt0': {'ip': '192.168.100.1', 'prefix': 24},
        'GigabitEthernet0/0/1': {'ip': '10.30.30.1', 'prefix': 24},
    })
    return RibEngine()


def test_learned_route_iface_is_resolved_from_next_hop(rib_nexus):
    """RIP/OSPF/BGPで学習した経路の出力IFが 'lan0' 固定でないこと

    Si-R以外（Catalyst/Nexus/cisco）では実在しないインターフェース名が
    show ip route に出ていた。
    """
    assert rib_nexus._iface_for_nexthop('nx', '10.30.30.77') == \
        'GigabitEthernet0/0/1'
    assert rib_nexus._iface_for_nexthop('nx', '192.168.100.9') == 'mgmt0'


def test_next_hop_outside_every_segment_yields_no_iface(rib_nexus):
    assert rib_nexus._iface_for_nexthop('nx', '198.51.100.1') == ''


def test_directly_connected_next_hop_yields_no_iface(rib_nexus):
    assert rib_nexus._iface_for_nexthop('nx', '0.0.0.0') == ''


# ── ④ show ip ospf interface が実インターフェースを出す ──────

def test_ospf_interface_lists_the_real_participating_interface():
    """決め打ちの GigabitEthernet0/0/0 / 192.168.1.1 を出さないこと

    以前は装置に関係なく同じ1ブロックをハードコードしており、
    そのインターフェースを持たない装置でも同じ内容が出ていた。
    """
    from engine.protocols import OspfEngine
    icmp_engine.register_device('nx-oi', 'N9K', {
        'mgmt0': {'ip': '192.168.100.1', 'prefix': 24},
        'GigabitEthernet0/0/1': {'ip': '10.30.30.1', 'prefix': 24},
    })
    e = OspfEngine()
    n = e._node('nx-oi')
    n.update({'enabled': True, 'networks': ['10.30.30.0/24'],
              'area_id': '0.0.0.0', 'process_id': 1,
              'router_id': '10.30.30.1'})
    out = e.format_show_ospf_interface('nx-oi')
    assert out.startswith('GigabitEthernet0/0/1 is up'), out
    assert 'Internet Address 10.30.30.1/24' in out, out
    # OSPFに参加していないmgmt0は出さない
    assert 'mgmt0' not in out, out
    assert '192.168.1.1' not in out, out


def test_ospf_interface_says_so_when_no_interface_participates():
    from engine.protocols import OspfEngine
    icmp_engine.register_device('nx-none', 'N9K',
                                {'mgmt0': {'ip': '192.168.100.5', 'prefix': 24}})
    e = OspfEngine()
    n = e._node('nx-none')
    n.update({'enabled': True, 'networks': ['10.30.30.0/24'],
              'area_id': '0.0.0.0', 'process_id': 1, 'router_id': '1.1.1.1'})
    assert 'not enabled on any interface' in \
        e.format_show_ospf_interface('nx-none')
