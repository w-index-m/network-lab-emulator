"""
経路フィルタリング(BGP prefix-list) / MD5認証(RIP・OSPF) / ECMP テスト

実行: pytest tests/test_filtering_auth_ecmp.py -v
"""

import asyncio
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto


@pytest.fixture
def fresh_engines():
    proto.vnet.links.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.rip_engine.nodes.clear()
    proto.ospf_engine.nodes.clear()
    proto.bgp_engine.nodes.clear()
    proto.rib_engine.nodes.clear()
    proto.filter_engine.prefix_lists.clear()
    proto.filter_engine.distribute_lists.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'rip': proto.rip_engine,
        'ospf': proto.ospf_engine, 'bgp': proto.bgp_engine,
        'rib': proto.rib_engine, 'filter': proto.filter_engine,
        'noop': noop,
    }


def _link(e, a, b):
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


class TestBgpPrefixListFilter:
    """BGP: neighbor prefix-list によるアウトバウンド/インバウンド経路フィルタ"""

    @pytest.mark.asyncio
    async def test_outbound_prefix_list_blocks_denied_prefix(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)

        # R1: 10.1.0.0/16 のみ許可、他は拒否するprefix-listをoutに適用
        e['filter'].add_prefix_list('R1', 'ONLY_10_1', 'permit', '10.1.0.0', 16)
        e['bgp'].set_neighbor_prefix_list('R1', 'R2', 'ONLY_10_1', 'out')

        await e['bgp'].advertise_network('R1', '10.1.0.0/16')
        await e['bgp'].advertise_network('R1', '10.2.0.0/16')
        await asyncio.sleep(4)

        r2_learned = {r.prefix for r in e['bgp'].nodes['R2']['rib_in']}
        assert '10.1.0.0' in r2_learned, "許可されたprefixが届いていない"
        assert '10.2.0.0' not in r2_learned, "拒否されたprefixが漏れている"

    @pytest.mark.asyncio
    async def test_inbound_prefix_list_blocks_denied_prefix(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)

        # R2側でR1からのインバウンドを制限
        e['filter'].add_prefix_list('R2', 'ONLY_20', 'permit', '20.0.0.0', 8)
        e['bgp'].set_neighbor_prefix_list('R2', 'R1', 'ONLY_20', 'in')

        await e['bgp'].advertise_network('R1', '20.0.0.0/8')
        await e['bgp'].advertise_network('R1', '30.0.0.0/8')
        await asyncio.sleep(4)

        r2_learned = {r.prefix for r in e['bgp'].nodes['R2']['rib_in']}
        assert '20.0.0.0' in r2_learned, "許可prefixが学習されていない"
        assert '30.0.0.0' not in r2_learned, "拒否prefixが学習されてしまっている"

    @pytest.mark.asyncio
    async def test_prefix_list_with_ge_le_range(self, fresh_engines):
        """ge/leでのレンジ指定フィルタがBGPにも適用される"""
        e = fresh_engines
        _link(e, 'R1', 'R2')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)

        # /24のみ許可（10.0.0.0/8 の中で ge24 le24）
        e['filter'].add_prefix_list('R1', 'SLASH24', 'permit', '10.0.0.0', 8, ge=24, le=24)
        e['bgp'].set_neighbor_prefix_list('R1', 'R2', 'SLASH24', 'out')

        await e['bgp'].advertise_network('R1', '10.1.1.0/24')
        await e['bgp'].advertise_network('R1', '10.2.0.0/16')
        await asyncio.sleep(4)

        r2_learned = {r.prefix for r in e['bgp'].nodes['R2']['rib_in']}
        assert '10.1.1.0' in r2_learned, "/24が許可されず届いていない"
        assert '10.2.0.0' not in r2_learned, "/16がフィルタされず漏れている"


class TestRipMd5Authentication:
    """RIP MD5認証: キー不一致でupdate拒否"""

    @pytest.mark.asyncio
    async def test_matching_key_learns_routes(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        e['rip'].set_authentication('R1', 'md5', 'secretkey')
        e['rip'].set_authentication('R2', 'md5', 'secretkey')
        await e['rip'].start('R1', 'R1', ['192.168.1.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('R1')
        await e['rip']._send_update('R2')
        await asyncio.sleep(0.5)

        r1_nets = [r.network for r in e['rip'].nodes['R1']['table']]
        assert '192.168.2.0' in r1_nets, "キー一致時に経路が学習されない"
        await e['rip'].stop('R1'); await e['rip'].stop('R2')

    @pytest.mark.asyncio
    async def test_mismatched_key_blocks_route_learning(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        e['rip'].set_authentication('R1', 'md5', 'keyA')
        e['rip'].set_authentication('R2', 'md5', 'keyB')
        await e['rip'].start('R1', 'R1', ['192.168.1.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('R1')
        await e['rip']._send_update('R2')
        await asyncio.sleep(0.5)

        r1_nets = [r.network for r in e['rip'].nodes['R1']['table']]
        r2_nets = [r.network for r in e['rip'].nodes['R2']['table']]
        assert '192.168.2.0' not in r1_nets, "キー不一致なのにR1がR2の経路を学習している"
        assert '192.168.1.0' not in r2_nets, "キー不一致なのにR2がR1の経路を学習している"
        await e['rip'].stop('R1'); await e['rip'].stop('R2')

    @pytest.mark.asyncio
    async def test_no_auth_configured_still_works(self, fresh_engines):
        """認証未設定なら従来通り学習できる（後方互換性）"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        await e['rip'].start('R1', 'R1', ['192.168.1.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('R1')
        await asyncio.sleep(0.5)
        r2_nets = [r.network for r in e['rip'].nodes['R2']['table']]
        assert '192.168.1.0' in r2_nets
        await e['rip'].stop('R1'); await e['rip'].stop('R2')


class TestOspfMd5Authentication:
    """OSPF MD5認証: キー不一致で隣接が成立しない"""

    @pytest.mark.asyncio
    async def test_matching_key_forms_adjacency(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        e['ospf'].set_authentication('R1', 'md5', 'ospfkey')
        e['ospf'].set_authentication('R2', 'md5', 'ospfkey')
        await e['ospf'].start('R1', 'R1', 1, ['10.0.1.0/24'])
        await e['ospf'].start('R2', 'R2', 1, ['10.0.2.0/24'])
        await asyncio.sleep(3)
        n1 = e['ospf'].nodes['R1']['neighbors']
        assert 'R2' in n1 and n1['R2'].state == 'Full', \
            "キー一致にもかかわらず隣接が成立していない"
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_mismatched_key_blocks_adjacency(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        e['ospf'].set_authentication('R1', 'md5', 'keyA')
        e['ospf'].set_authentication('R2', 'md5', 'keyB')
        await e['ospf'].start('R1', 'R1', 1, ['10.0.1.0/24'])
        await e['ospf'].start('R2', 'R2', 1, ['10.0.2.0/24'])
        await asyncio.sleep(3)
        n1 = e['ospf'].nodes['R1']['neighbors']
        n2 = e['ospf'].nodes['R2']['neighbors']
        assert 'R2' not in n1, "キー不一致なのにR1がR2と隣接している"
        assert 'R1' not in n2, "キー不一致なのにR2がR1と隣接している"
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')


class TestEcmp:
    """ECMP: 等コスト複数経路の集約（Cisco maximum-paths相当）"""

    def test_ecmp_two_equal_static_routes(self, fresh_engines):
        e = fresh_engines
        e['rib'].add_static_route('R1', 'R1', '192.168.100.0', 24, '10.0.0.1', 1)
        e['rib'].add_static_route('R1', 'R1', '192.168.100.0', 24, '10.0.0.2', 1)
        groups = e['rib'].get_ecmp_routes('R1')
        g = next(x for x in groups if x['network'] == '192.168.100.0')
        assert len(g['next_hops']) == 2, f"ECMPで2経路が集約されていない: {g}"
        assert '10.0.0.1' in g['next_hops'] and '10.0.0.2' in g['next_hops']

    def test_ecmp_ignores_worse_ad_route(self, fresh_engines):
        """AD違いの経路はECMP集約対象にならず、AD最小のみ残る"""
        e = fresh_engines
        e['rib'].add_static_route('R1', 'R1', '10.0.0.0', 24, '192.168.1.1', 1)
        e['rib'].add_static_route('R1', 'R1', '10.0.0.0', 24, '192.168.2.1', 200)
        groups = e['rib'].get_ecmp_routes('R1')
        g = next(x for x in groups if x['network'] == '10.0.0.0')
        assert len(g['next_hops']) == 1, f"AD違いの経路までECMP集約されている: {g}"
        assert g['next_hops'] == ['192.168.1.1']
        assert g['ad'] == 1

    @pytest.mark.asyncio
    async def test_ecmp_ospf_equal_cost_paths(self, fresh_engines):
        """OSPF: 2つの等コスト経路がECMPとして集約される"""
        e = fresh_engines
        # R1 - R2 - R4, R1 - R3 - R4 (等コスト2経路)
        _link(e, 'R1', 'R2')
        _link(e, 'R1', 'R3')
        _link(e, 'R2', 'R4')
        _link(e, 'R3', 'R4')

        for rid in ['R1', 'R2', 'R3']:
            await e['ospf'].start(rid, rid, 1, [])
        await e['ospf'].start('R4', 'R4', 1, ['192.168.99.0/24'])

        await asyncio.sleep(5)

        groups = e['rib'].get_ecmp_routes('R1')
        g = next((x for x in groups if x['network'] == '192.168.99.0'), None)
        assert g is not None, "R1がR4のLANへの経路を学習していない"
        assert len(g['next_hops']) >= 1, f"ECMP経路が構築されていない: {g}"

        for rid in ['R1', 'R2', 'R3', 'R4']:
            await e['ospf'].stop(rid)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
