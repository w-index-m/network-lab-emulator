"""
IPv6アドレッシング/ルーティングの実動作(engine.protocols.Ipv6Engine)。

ユーザーから「ネットワークラボの改善点ありますか？」への回答として
提案した項目の一つ(「IPv6ルーティングが完全に未実装 — engine/rules.py
の"ipv6"言及37件は全部見た目だけの固定応答で、実ルーティングが無い」)
への対応。

調べてみると、`ipv6 address <addr>/<prefix>`(interface config)と
`ipv6 unicast-routing`は既に`engine/rules.py`でstate.interfaces[if]
['ipv6']/['ipv6_prefix']へ実際に反映されており、`show running-config`/
`show ipv6 interface brief`にも出ていた——つまりアドレッシングの
CLI/表示層は既にあった。無かったのは「そこから先」: `ipv6 route`
スタティック経路コマンド自体が存在しない、`show ipv6 route`が無い、
ping/tracerouteがIPv6アドレス(コロンを含む宛先)を一切認識しない
(正規表現がIPv4のドット区切り数字限定だった)、の3点。

IPv4のIcmpEngine/RibEngineが持つACL/NAT/VRF/ICMP Redirect等の機能は
スコープ外(v6プラス/MAP-E実装時と同じ、現実的なカット)。実装したのは
直結経路の自動導出(`register_device`)・スタティック経路
(`ipv6 route <dest>/<len> <next-hop>`)・実vnetトポロジーのBFSによる
マルチホップ到達性判定(`trace_path`)まで。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.protocols import ipv6_engine, vnet

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _link(a, b):
    client.post('/api/link', json={'device_a': a, 'device_b': b})


class TestIpv6AddressConfigReflection:
    def test_configured_address_shows_in_interface_brief(self):
        _dev('v6-if-1')
        _cli('v6-if-1', 'configure terminal')
        _cli('v6-if-1', 'interface GigabitEthernet0/0/0')
        _cli('v6-if-1', 'ipv6 address 2001:db8:1::1/64')
        _cli('v6-if-1', 'end')
        out = _cli('v6-if-1', 'show ipv6 interface brief')
        assert '2001:db8:1::1' in out


class TestIpv6ConnectedRoute:
    def test_configured_address_becomes_a_connected_route(self):
        dev = 'v6-conn-1'
        _dev(dev)
        _cli(dev, 'configure terminal')
        _cli(dev, 'interface GigabitEthernet0/0/0')
        _cli(dev, 'ipv6 address 2001:db8:10::1/64')
        _cli(dev, 'end')
        out = _cli(dev, 'show ipv6 route')
        assert 'C   2001:db8:10::/64' in out
        assert 'directly connected' in out

    def test_no_addresses_means_zero_entries(self):
        _dev('v6-conn-2')
        out = _cli('v6-conn-2', 'show ipv6 route')
        assert '0 entries' in out


class TestIpv6StaticRoute:
    def test_ipv6_route_command_is_reflected_in_show_ipv6_route(self):
        dev = 'v6-static-1'
        _dev(dev)
        _cli(dev, 'configure terminal')
        _cli(dev, 'ipv6 route 2001:db8:20::/64 2001:db8:10::2')
        _cli(dev, 'end')
        out = _cli(dev, 'show ipv6 route')
        assert 'S   2001:db8:20::/64' in out
        assert 'via 2001:db8:10::2' in out

    def test_no_ipv6_route_removes_it(self):
        dev = 'v6-static-2'
        _dev(dev)
        _cli(dev, 'configure terminal')
        _cli(dev, 'ipv6 route 2001:db8:30::/64 2001:db8:10::2')
        _cli(dev, 'no ipv6 route 2001:db8:30::/64 2001:db8:10::2')
        _cli(dev, 'end')
        out = _cli(dev, 'show ipv6 route')
        assert '2001:db8:30::' not in out


class TestIpv6PingAndTraceroute:
    def setup_method(self):
        # 毎回クリーンな状態で: R1 -- R2 の直結リンク、両方にIPv6アドレス
        self.r1, self.r2 = 'v6-r1', 'v6-r2'
        _dev(self.r1)
        _dev(self.r2)
        _link(self.r1, self.r2)
        for dev, addr in ((self.r1, '2001:db8::1'), (self.r2, '2001:db8::2')):
            _cli(dev, 'configure terminal')
            _cli(dev, 'interface GigabitEthernet0/0/0')
            _cli(dev, f'ipv6 address {addr}/64')
            _cli(dev, 'end')

    def test_ping_to_directly_connected_neighbor_succeeds(self):
        out = _cli(self.r1, 'ping 2001:db8::2')
        assert 'Success rate is 100 percent' in out or '0% packet loss' in out

    def test_ping_ipv6_keyword_form_also_works(self):
        out = _cli(self.r1, 'ping ipv6 2001:db8::2')
        assert 'Success rate is 100 percent' in out or '0% packet loss' in out

    def test_ping_to_unknown_address_fails(self):
        out = _cli(self.r1, 'ping 2001:db8:ffff::99')
        assert 'Success rate is 0 percent' in out or '100% packet loss' in out

    def test_traceroute_to_directly_connected_neighbor_succeeds(self):
        out = _cli(self.r1, 'traceroute 2001:db8::2')
        assert '2001:db8::2' in out
        assert 'destination unreachable' not in out

    def test_ipv4_ping_regex_is_unaffected(self):
        """IPv6対応の正規表現追加が既存のIPv4 ping処理に影響していない
        (従来通りIPv4側のCisco形式出力になる)ことの固定。127.0.0.1への
        到達性自体はこのエミュレータのIPv4 IcmpEngineの既存仕様
        (ループバック特別扱い無し)に依るため、到達性ではなく出力形式
        (IPv6分岐に誤って吸われていないこと)を見る。"""
        _dev('v6-ipv4-check')
        out = _cli('v6-ipv4-check', 'ping 127.0.0.1')
        assert 'ICMP Echos to 127.0.0.1' in out
        assert 'Success rate is' in out


class TestIpv6MultiHopReachability:
    def test_ping_through_a_static_route_two_hops_away(self):
        """R1 -- MID -- R2。R1からR2へは直結ではなくMID経由のスタティック
        経路(実vnetトポロジーのBFSで隣接を辿れること)が必要。"""
        r1, mid, r2 = 'v6-hop-r1', 'v6-hop-mid', 'v6-hop-r2'
        _dev(r1)
        _dev(mid)
        _dev(r2)
        _link(r1, mid)
        _link(mid, r2)
        for dev, addr in ((r1, '2001:db8:a::1'),):
            _cli(dev, 'configure terminal')
            _cli(dev, 'interface GigabitEthernet0/0/0')
            _cli(dev, f'ipv6 address {addr}/64')
            _cli(dev, 'end')
        for addr_a, addr_b in [('2001:db8:a::2', '2001:db8:b::1')]:
            _cli(mid, 'configure terminal')
            _cli(mid, 'interface GigabitEthernet0/0/0')
            _cli(mid, f'ipv6 address {addr_a}/64')
            _cli(mid, 'interface GigabitEthernet0/0/1')
            _cli(mid, f'ipv6 address {addr_b}/64')
            _cli(mid, 'end')
        _cli(r2, 'configure terminal')
        _cli(r2, 'interface GigabitEthernet0/0/0')
        _cli(r2, 'ipv6 address 2001:db8:b::2/64')
        _cli(r2, 'end')

        # R1にはMID経由でR2側ネットへのスタティック経路が必要
        _cli(r1, 'configure terminal')
        _cli(r1, 'ipv6 route 2001:db8:b::/64 2001:db8:a::2')
        _cli(r1, 'end')
        # R2側もR1側ネットへの戻り経路が必要(traceroute/pingは往復ではなく
        # 片方向の到達性判定なので無くても良いが、現実的な設定として入れる)
        _cli(r2, 'configure terminal')
        _cli(r2, 'ipv6 route 2001:db8:a::/64 2001:db8:b::1')
        _cli(r2, 'end')

        out = _cli(r1, 'ping 2001:db8:b::2')
        assert 'Success rate is 100 percent' in out

    def test_without_the_static_route_it_is_unreachable(self):
        """直前のテストと同じトポロジーで、スタティック経路が無ければ
        到達しない(フェイクの成功を作っていないことの固定)。"""
        r1, mid, r2 = 'v6-nohop-r1', 'v6-nohop-mid', 'v6-nohop-r2'
        _dev(r1)
        _dev(mid)
        _dev(r2)
        _link(r1, mid)
        _link(mid, r2)
        _cli(r1, 'configure terminal')
        _cli(r1, 'interface GigabitEthernet0/0/0')
        _cli(r1, 'ipv6 address 2001:db8:c::1/64')
        _cli(r1, 'end')
        _cli(r2, 'configure terminal')
        _cli(r2, 'interface GigabitEthernet0/0/0')
        _cli(r2, 'ipv6 address 2001:db8:d::2/64')
        _cli(r2, 'end')

        out = _cli(r1, 'ping 2001:db8:d::2')
        assert 'Success rate is 0 percent' in out
