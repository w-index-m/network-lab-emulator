"""
BGP Route Reflector(cluster-id / route-reflector-client)と
RTBH(Remote-Triggered Black-Hole、community起点のNull0ブラックホール
ルーティング)。

https://github.com/vincentbernat/network-lab の lab-routereflector /
lab-rtbh を見て、このエミュレータのBGPエンジンに持ち込めそうな2機能として
ユーザーの選択で実装した。

**実装方針**: 両方とも既存のBGPエンジン(`engine/protocols.py`の
`BgpEngine`)にある仕組みの上に素直に乗る形にした。
- Route Reflector: `_compute_adverts`の既存iBGP split-horizonチェック
  (`src_is_ibgp and not is_ebgp: continue`)に、送信先/学習元どちらかが
  `reflector_client`であれば反射する例外を追加しただけ。originator-id/
  cluster-listによるループ防止までは実装せず(スコープ外、コメントで明記)。
- RTBH: 既存のBGP community機構(`route-map ... set community`/
  `send-community`)はそのまま使い、受信側に新設した
  `bgp rtbh-community <AS:NUM>`で指定したcommunityを持つベストパスを、
  `rib_engine`へ`next_hop='Null0'`の静的経路として自動installするだけ。
  `IcmpEngine._resolve_next_hop_detail`に`next_hop == 'Null0'`なら
  転送せず不達とする分岐を追加し、実際にpingが失敗する(=破棄される)こと
  まで確認した。

このテストは `tests/test_bgp_advanced.py` と同じ方式(engine.protocols を
asyncio で直接操作)で書く — BGPのセッション確立は実際の非同期FSM
(Idle→Connect→OpenSent→OpenConfirm→Established、complete with
asyncio.sleep)なので、CLI経由のTestClient(同期HTTPリクエスト単位で
イベントループが閉じる)ではタイマーが進まず確立しない。
"""

import asyncio
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto
import app as app_module
from engine.rules import DeviceState


@pytest.fixture
def fresh_engines():
    proto.vnet.links.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.bgp_engine.nodes.clear()
    proto.rib_engine.nodes.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'bgp': proto.bgp_engine,
        'rib': proto.rib_engine, 'icmp': proto.icmp_engine, 'noop': noop,
    }


def _link(e, a, b):
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


class TestRouteReflector:
    """RR(RR1) -- クライアント(CL1, CL2)。CL1とCL2は直結していない
    (ハブ&スポーク)ため、RRが無ければCL2はCL1の経路を学習できない。"""

    @pytest.mark.asyncio
    async def test_without_reflector_client_ibgp_split_horizon_blocks_it(self, fresh_engines):
        """まず従来通りのiBGP split-horizonが健在であることを確認する
        (regression防止: reflector-client未設定なら反射しない)。"""
        e = fresh_engines
        _link(e, 'RR0', 'CL1a')
        _link(e, 'RR0', 'CL2a')

        await e['bgp'].start('RR0', 'RR0', 65000)
        await e['bgp'].start('CL1a', 'CL1a', 65000)
        await e['bgp'].start('CL2a', 'CL2a', 65000)

        await e['bgp'].add_neighbor('RR0', 'CL1a', 'CL1a', 65000)
        await e['bgp'].add_neighbor('CL1a', 'RR0', 'RR0', 65000)
        await e['bgp'].add_neighbor('RR0', 'CL2a', 'CL2a', 65000)
        await e['bgp'].add_neighbor('CL2a', 'RR0', 'RR0', 65000)

        await e['bgp'].advertise_network('CL1a', '192.168.50.0/24')
        await asyncio.sleep(4)

        learned = [r.prefix for r in e['bgp'].nodes['CL2a']['rib_in']]
        assert '192.168.50.0' not in learned

    @pytest.mark.asyncio
    async def test_reflector_client_reflects_route_to_other_client(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RR1', 'CL1')
        _link(e, 'RR1', 'CL2')

        await e['bgp'].start('RR1', 'RR1', 65000)
        await e['bgp'].start('CL1', 'CL1', 65000)
        await e['bgp'].start('CL2', 'CL2', 65000)

        await e['bgp'].add_neighbor('RR1', 'CL1', 'CL1', 65000)
        await e['bgp'].add_neighbor('CL1', 'RR1', 'RR1', 65000)
        await e['bgp'].add_neighbor('RR1', 'CL2', 'CL2', 65000)
        await e['bgp'].add_neighbor('CL2', 'RR1', 'RR1', 65000)

        e['bgp'].set_cluster_id('RR1', '1.1.1.1')
        e['bgp'].set_neighbor_route_reflector_client('RR1', 'CL1')
        e['bgp'].set_neighbor_route_reflector_client('RR1', 'CL2')

        await e['bgp'].advertise_network('CL1', '192.168.60.0/24')
        await asyncio.sleep(4)

        learned = {r.prefix: r for r in e['bgp'].nodes['CL2']['rib_in']}
        assert '192.168.60.0' in learned, 'RRがCL1→CL2へ経路を反射していない'
        # iBGP反射: as-pathはprependされず、next-hopも書き換えられない(実機のRR仕様)
        assert learned['192.168.60.0'].as_path == [65000]

        assert e['bgp'].nodes['RR1']['cluster_id'] == '1.1.1.1'
        assert e['bgp'].nodes['RR1']['sessions']['CL1'].reflector_client is True

    @pytest.mark.asyncio
    async def test_reflector_client_does_not_relearn_its_own_route_back(self, fresh_engines):
        """反射によってクライアント自身が広告した経路が自分に戻ってこない
        (学習元へは広告し返さない、既存ガードがRRでも効くこと)を確認。"""
        e = fresh_engines
        _link(e, 'RR2', 'CL3')
        _link(e, 'RR2', 'CL4')

        await e['bgp'].start('RR2', 'RR2', 65000)
        await e['bgp'].start('CL3', 'CL3', 65000)
        await e['bgp'].start('CL4', 'CL4', 65000)
        await e['bgp'].add_neighbor('RR2', 'CL3', 'CL3', 65000)
        await e['bgp'].add_neighbor('CL3', 'RR2', 'RR2', 65000)
        await e['bgp'].add_neighbor('RR2', 'CL4', 'CL4', 65000)
        await e['bgp'].add_neighbor('CL4', 'RR2', 'RR2', 65000)
        e['bgp'].set_neighbor_route_reflector_client('RR2', 'CL3')
        e['bgp'].set_neighbor_route_reflector_client('RR2', 'CL4')

        await e['bgp'].advertise_network('CL3', '192.168.70.0/24')
        await asyncio.sleep(4)

        cl3_learned = [r.prefix for r in e['bgp'].nodes['CL3']['rib_in']]
        assert '192.168.70.0' not in cl3_learned


class TestRtbh:
    @pytest.mark.asyncio
    async def test_matching_community_installs_null0_route(self, fresh_engines):
        e = fresh_engines
        _link(e, 'VIC1', 'UP1')

        await e['bgp'].start('VIC1', 'VIC1', 65001)
        await e['bgp'].start('UP1', 'UP1', 65002)
        await e['bgp'].add_neighbor('VIC1', 'UP1', 'UP1', 65002)
        await e['bgp'].add_neighbor('UP1', 'VIC1', 'VIC1', 65001)

        e['bgp'].add_route_map('VIC1', 'RTBH', communities=['65000:666'])
        e['bgp'].set_neighbor_route_map('VIC1', 'UP1', 'RTBH', 'out')
        e['bgp'].set_neighbor_send_community('VIC1', 'UP1', True)
        e['bgp'].set_rtbh_community('UP1', '65000:666')

        await e['bgp'].advertise_network('VIC1', '198.51.100.0/24')
        await asyncio.sleep(4)

        routes = e['rib']._node('UP1')['static_routes']
        null0 = [r for r in routes if r.network == '198.51.100.0' and r.next_hop == 'Null0']
        assert null0, 'rtbh-communityに一致する経路がNull0としてインストールされていない'
        assert null0[0].prefix == 24

    @pytest.mark.asyncio
    async def test_non_matching_community_does_not_install_null0(self, fresh_engines):
        e = fresh_engines
        _link(e, 'VIC2', 'UP2')
        await e['bgp'].start('VIC2', 'VIC2', 65001)
        await e['bgp'].start('UP2', 'UP2', 65002)
        await e['bgp'].add_neighbor('VIC2', 'UP2', 'UP2', 65002)
        await e['bgp'].add_neighbor('UP2', 'VIC2', 'VIC2', 65001)

        # rtbh-community未設定 -> 普通に経路学習されるだけでNull0化されない
        await e['bgp'].advertise_network('VIC2', '198.51.101.0/24')
        await asyncio.sleep(4)

        learned = [r.prefix for r in e['bgp'].nodes['UP2']['rib_in']]
        assert '198.51.101.0' in learned
        routes = e['rib']._node('UP2')['static_routes']
        assert not any(r.next_hop == 'Null0' for r in routes)

    @pytest.mark.asyncio
    async def test_withdrawal_removes_the_null0_route(self, fresh_engines):
        """経路が撤回(セッションダウン等)されたらNull0経路も解除される。"""
        e = fresh_engines
        _link(e, 'VIC3', 'UP3')
        await e['bgp'].start('VIC3', 'VIC3', 65001)
        await e['bgp'].start('UP3', 'UP3', 65002)
        await e['bgp'].add_neighbor('VIC3', 'UP3', 'UP3', 65002)
        await e['bgp'].add_neighbor('UP3', 'VIC3', 'VIC3', 65001)

        e['bgp'].add_route_map('VIC3', 'RTBH', communities=['65000:666'])
        e['bgp'].set_neighbor_route_map('VIC3', 'UP3', 'RTBH', 'out')
        e['bgp'].set_neighbor_send_community('VIC3', 'UP3', True)
        e['bgp'].set_rtbh_community('UP3', '65000:666')
        await e['bgp'].advertise_network('VIC3', '198.51.102.0/24')
        await asyncio.sleep(4)
        assert any(r.next_hop == 'Null0' for r in e['rib']._node('UP3')['static_routes'])

        await e['bgp'].session_down('UP3', 'VIC3', 'test teardown')
        await asyncio.sleep(1)
        assert not any(r.next_hop == 'Null0' for r in e['rib']._node('UP3')['static_routes'])

    def test_null0_nexthop_is_treated_as_unreachable_by_icmp_engine(self, fresh_engines):
        """Null0宛(ブラックホール)は実際にICMP的に不達になる
        (どこかの隣接機器へ誤って転送されない)ことを確認する。"""
        e = fresh_engines
        e['icmp'].device_ips['DROP1'] = {
            'hostname': 'DROP1',
            'ips': {'10.9.0.1': 24},
            'interfaces': {'Gi0/0': {'ip': '10.9.0.1', 'prefix': 24}},
        }
        e['rib']._node('DROP1')
        e['rib'].add_static_route('DROP1', 'DROP1', '198.51.103.0', 24, 'Null0', ad=1)
        dev, ip = e['icmp']._resolve_next_hop_detail('DROP1', '198.51.103.5')
        assert dev is None and ip is None


class TestCliParsing:
    """app.py側の新規CLI正規表現(bgp cluster-id / neighbor ...
    route-reflector-client / bgp rtbh-community)が bgp_engine を正しく
    呼び出すこと。セッション確立の非同期タイマーには依存しない。"""

    def _dev(self, dev_id, hostname='R'):
        proto.bgp_engine.nodes.pop(dev_id, None)
        st = DeviceState('cisco', hostname)
        app_module.device_sessions[dev_id] = st
        return dev_id, st

    def _aconf(self, dev, st, cmds):
        async def _go():
            out = ''
            for c in cmds:
                out = await app_module.handle_protocol_config(dev, c, st)
            return out
        return asyncio.run(_go())

    def test_bgp_cluster_id_command(self):
        dev, st = self._dev('cli-rr-1')
        self._aconf(dev, st, ['router bgp 65000', 'bgp cluster-id 9.9.9.9'])
        assert proto.bgp_engine.nodes[dev]['cluster_id'] == '9.9.9.9'

    def test_bgp_rtbh_community_command(self):
        dev, st = self._dev('cli-rtbh-1')
        self._aconf(dev, st, ['router bgp 65000', 'bgp rtbh-community 65000:666'])
        assert proto.bgp_engine.nodes[dev]['rtbh_community'] == '65000:666'

    def test_neighbor_route_reflector_client_command(self):
        dev, st = self._dev('cli-rr-2')
        peer, peer_st = self._dev('cli-rr-2-peer')
        self._aconf(dev, st, [
            'router bgp 65000',
            'neighbor 10.5.5.2 remote-as 65000',
        ])
        # _bgp_nbr_ipmap はトポロジー上のリンクが無い場合でも
        # neighbor コマンドの時点でIPだけ記録される。ピアが実在しない場合、
        # route-reflector-client は(他のneighborサブコマンド同様)
        # 何もせず無視される。ここでは実リンクを用意してピアを解決させる。
        proto.vnet.links.setdefault(dev, set()).add(peer)
        proto.vnet.links.setdefault(peer, set()).add(dev)

        async def _go():
            await app_module.handle_protocol_config(
                dev, 'neighbor 10.5.5.2 remote-as 65000', st)
            await app_module.handle_protocol_config(
                dev, 'neighbor 10.5.5.2 route-reflector-client', st)
        asyncio.run(_go())

        peer_id = st._bgp_nbr_ipmap.get('10.5.5.2')
        assert peer_id is not None
        session = proto.bgp_engine.nodes[dev]['sessions'][peer_id]
        assert session.reflector_client is True
