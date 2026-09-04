"""
BGP 高度な機能テスト: AS-path prepend / local-preference / MED / route-map / MD5認証

実行: pytest tests/test_bgp_advanced.py -v
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
    proto.bgp_engine.nodes.clear()
    proto.rib_engine.nodes.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'bgp': proto.bgp_engine,
        'rib': proto.rib_engine, 'noop': noop,
    }


def _link(e, a, b):
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


class TestAsPathPrepend:
    """AS-path prepend: 経路を意図的に長く見せて優先度を下げる"""

    @pytest.mark.asyncio
    async def test_prepend_lengthens_as_path(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)

        # セッション確立(delayed open)前に route-map と広告prefixを設定
        e['bgp'].add_route_map('R1', 'PREPEND', prepend=[65001, 65001, 65001])
        e['bgp'].set_neighbor_route_map('R1', 'R2', 'PREPEND', 'out')
        await e['bgp'].advertise_network('R1', '10.0.0.0/16')

        await asyncio.sleep(4)

        learned = [r for r in e['bgp'].nodes['R2']['rib_in'] if r.prefix == '10.0.0.0']
        assert learned, "R2がR1のprefixを学習していない"
        # 元のas-path(65001) + prepend 3回 = 4つ
        assert len(learned[0].as_path) >= 4, \
            f"as-path prependが反映されていない: {learned[0].as_path}"

    @pytest.mark.asyncio
    async def test_prepend_affects_path_selection(self, fresh_engines):
        """3AS構成: prependされた経路とされていない経路のas-path長を比較"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        _link(e, 'R3', 'R2')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].start('R3', 'R3', 65003)

        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)
        await e['bgp'].add_neighbor('R3', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R3', 'R3', 65003)

        # R3 は prepend 有り、R1 は prepend なし
        e['bgp'].add_route_map('R3', 'PREPEND', prepend=[65003, 65003])
        e['bgp'].set_neighbor_route_map('R3', 'R2', 'PREPEND', 'out')

        await e['bgp'].advertise_network('R1', '192.168.100.0/24')
        await e['bgp'].advertise_network('R3', '192.168.200.0/24')
        await asyncio.sleep(4)

        r2_learned = {r.prefix: r for r in e['bgp'].nodes['R2']['rib_in']}
        assert '192.168.100.0' in r2_learned
        assert '192.168.200.0' in r2_learned
        # prependされたR3経由の方がas-pathが長い
        assert len(r2_learned['192.168.200.0'].as_path) > len(r2_learned['192.168.100.0'].as_path)


class TestLocalPreference:
    """local-preference: AS内での経路優先度制御（値が大きい方が優先）"""

    @pytest.mark.asyncio
    async def test_local_pref_selects_best_path(self, fresh_engines):
        """同一prefixに複数経路がある場合、local-pref大が優先される"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        _link(e, 'R1', 'R3')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].start('R3', 'R3', 65003)

        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)
        await e['bgp'].add_neighbor('R1', 'R3', 'R3', 65003)
        await e['bgp'].add_neighbor('R3', 'R1', 'R1', 65001)

        # R1側で R2経由にlocal-pref 200（優先） / R3経由に100 を設定
        e['bgp'].add_route_map('R1', 'PREFER_R2', local_pref=200)
        e['bgp'].set_neighbor_route_map('R1', 'R2', 'PREFER_R2', 'in')
        e['bgp'].add_route_map('R1', 'DEFAULT_R3', local_pref=100)
        e['bgp'].set_neighbor_route_map('R1', 'R3', 'DEFAULT_R3', 'in')

        # R2, R3 が同一prefixを広告（マルチホーム想定）
        await e['bgp'].advertise_network('R2', '10.99.0.0/16')
        await e['bgp'].advertise_network('R3', '10.99.0.0/16')
        await asyncio.sleep(4)

        e['bgp']._recalc_best_path('R1')
        best = {r['prefix']: r for r in e['bgp'].nodes['R1']['loc_rib']}
        assert '10.99.0.0' in best, f"ベストパスが計算されていない: {best}"
        assert best['10.99.0.0']['learned_from'] == 'R2', \
            f"local-pref大(R2経由)が優先されていない: {best['10.99.0.0']}"


class TestMed:
    """MED (Multi-Exit Discriminator): 値が小さい方が優先"""

    @pytest.mark.asyncio
    async def test_med_influences_selection_when_pref_and_pathlen_equal(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        _link(e, 'R1', 'R3')

        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].start('R3', 'R3', 65003)

        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)
        await e['bgp'].add_neighbor('R1', 'R3', 'R3', 65003)
        await e['bgp'].add_neighbor('R3', 'R1', 'R1', 65001)

        e['bgp'].add_route_map('R2', 'MED_LOW', med=10)
        e['bgp'].set_neighbor_route_map('R2', 'R1', 'MED_LOW', 'out')
        e['bgp'].add_route_map('R3', 'MED_HIGH', med=100)
        e['bgp'].set_neighbor_route_map('R3', 'R1', 'MED_HIGH', 'out')

        await e['bgp'].advertise_network('R2', '10.55.0.0/16')
        await e['bgp'].advertise_network('R3', '10.55.0.0/16')
        await asyncio.sleep(4)

        e['bgp']._recalc_best_path('R1')
        best = {r['prefix']: r for r in e['bgp'].nodes['R1']['loc_rib']}
        assert '10.55.0.0' in best
        assert best['10.55.0.0']['learned_from'] == 'R2', \
            f"MED小(R2経由)が優先されていない: {best['10.55.0.0']}"


class TestBgpAuthentication:
    """BGP MD5認証: パスワード不一致でセッション確立不可"""

    @pytest.mark.asyncio
    async def test_matching_password_establishes(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)
        e['bgp'].set_neighbor_password('R1', 'R2', 'secret123')
        e['bgp'].set_neighbor_password('R2', 'R1', 'secret123')
        await asyncio.sleep(4)
        s1 = e['bgp'].nodes['R1']['sessions']['R2']
        assert s1.state == 'Established', f"一致パスワードでEstablishedにならない: {s1.state}"

    @pytest.mark.asyncio
    async def test_mismatched_password_blocks_session(self, fresh_engines):
        e = fresh_engines
        _link(e, 'R1', 'R2')
        await e['bgp'].start('R1', 'R1', 65001)
        await e['bgp'].start('R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R1', 'R2', 'R2', 65002)
        await e['bgp'].add_neighbor('R2', 'R1', 'R1', 65001)
        # セッション生成後にパスワードを設定（不一致）
        e['bgp'].set_neighbor_password('R1', 'R2', 'secretA')
        e['bgp'].set_neighbor_password('R2', 'R1', 'secretB')
        await asyncio.sleep(4)
        s1 = e['bgp'].nodes['R1']['sessions']['R2']
        assert s1.state != 'Established', \
            f"パスワード不一致なのにEstablishedになっている: {s1.state}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
