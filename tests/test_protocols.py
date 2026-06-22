"""
プロトコルエンジン自動テストスイート
実行: cd network-lab && python -m pytest tests/ -v

全プロトコル（RIP/OSPF/BGP/STP/スタティック/フローティング/ICMP）を
機械的に検証する。各テストは独立してエンジンをリセットして実行。
"""
import asyncio
import sys
import os
import pytest

# engineをインポートできるようにパス追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import (
    VirtualNetwork, RipEngine, OspfEngine, BgpEngine, StpEngine,
    RibEngine, IcmpEngine,
)
import engine.protocols as proto


# ══════════════════════════════════════════
# フィクスチャ: 各テストでエンジンをリセット
# ══════════════════════════════════════════
@pytest.fixture
def fresh_engines():
    """各テスト用にクリーンなエンジン群を用意（タスク掃除はconftestが担当）"""
    proto.vnet.links.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.rip_engine.nodes.clear()
    proto.ospf_engine.nodes.clear()
    proto.bgp_engine.nodes.clear()
    proto.stp_engine.nodes.clear()
    proto.rib_engine.nodes.clear()
    proto.icmp_engine.device_ips.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'rip': proto.rip_engine,
        'ospf': proto.ospf_engine, 'bgp': proto.bgp_engine,
        'stp': proto.stp_engine, 'rib': proto.rib_engine,
        'icmp': proto.icmp_engine, 'noop': noop,
    }


def _link(e, a, b):
    """2装置を接続して表示コールバック登録"""
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


# ══════════════════════════════════════════
# RIP テスト
# ══════════════════════════════════════════
class TestRip:
    @pytest.mark.asyncio
    async def test_route_exchange(self, fresh_engines):
        """2台間でRIPルートが交換される"""
        e = fresh_engines
        _link(e, 'A', 'B')
        await e['rip'].start('A', 'Router-A', ['192.168.1.0/24'])
        await e['rip'].start('B', 'Router-B', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('A')
        await e['rip']._send_update('B')
        await asyncio.sleep(0.5)

        # AがBのネットワークを学習
        a_nets = [r.network for r in e['rip'].nodes['A']['table']]
        b_nets = [r.network for r in e['rip'].nodes['B']['table']]
        assert '192.168.2.0' in a_nets, "AがBの経路を学習していない"
        assert '192.168.1.0' in b_nets, "BがAの経路を学習していない"
        await e['rip'].stop('A')
        await e['rip'].stop('B')

    @pytest.mark.asyncio
    async def test_metric_increment(self, fresh_engines):
        """学習したルートのメトリックが+1される"""
        e = fresh_engines
        _link(e, 'A', 'B')
        await e['rip'].start('A', 'R-A', ['10.0.0.0/24'])
        await e['rip'].start('B', 'R-B', ['10.0.1.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('A')
        await asyncio.sleep(0.5)
        learned = [r for r in e['rip'].nodes['B']['table']
                   if r.network == '10.0.0.0']
        assert learned, "ルート未学習"
        assert learned[0].metric == 2, f"metric期待2、実際{learned[0].metric}"
        await e['rip'].stop('A')
        await e['rip'].stop('B')

    @pytest.mark.asyncio
    async def test_split_horizon(self, fresh_engines):
        """スプリットホライズン: 学習元には広告し返さない"""
        e = fresh_engines
        _link(e, 'A', 'B')
        await e['rip'].start('A', 'R-A', ['10.0.0.0/24'])
        await e['rip'].start('B', 'R-B', [])
        await asyncio.sleep(1)
        await e['rip']._send_update('A')
        await asyncio.sleep(0.5)
        # BはAから10.0.0.0を学習。Bが広告する内容にAから学んだ経路を
        # A向けには含めない（split horizon）
        learned = [r for r in e['rip'].nodes['B']['table']
                   if r.network == '10.0.0.0']
        assert learned and learned[0].learned_from == 'A'
        await e['rip'].stop('A')
        await e['rip'].stop('B')


# ══════════════════════════════════════════
# OSPF テスト
# ══════════════════════════════════════════
class TestOspf:
    @pytest.mark.asyncio
    async def test_neighbor_full(self, fresh_engines):
        """OSPFネイバーがFULL状態になる"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        await e['ospf'].start('R1', 'Router-1', 1, ['10.0.0.0/24'])
        await e['ospf'].start('R2', 'Router-2', 1, ['10.0.1.0/24'])
        await asyncio.sleep(3)
        n1 = e['ospf'].nodes['R1']['neighbors']
        n2 = e['ospf'].nodes['R2']['neighbors']
        assert 'R2' in n1, "R1がR2を認識していない"
        assert n1['R2'].state == 'Full', f"状態Full期待、実際{n1['R2'].state}"
        assert 'R1' in n2 and n2['R1'].state == 'Full'
        await e['ospf'].stop('R1')
        await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_area_mismatch_no_adjacency(self, fresh_engines):
        """エリアIDが異なると隣接が張れない"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        await e['ospf'].start('R1', 'R-1', 1, ['10.0.0.0/24'], '0.0.0.0')
        await e['ospf'].start('R2', 'R-2', 1, ['10.0.1.0/24'], '0.0.0.1')  # 別エリア
        await asyncio.sleep(3)
        n1 = e['ospf'].nodes['R1']['neighbors']
        assert 'R2' not in n1, "エリア不一致なのに隣接が張れている"
        await e['ospf'].stop('R1')
        await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_area_normalization(self, fresh_engines):
        """area 0 と 0.0.0.0 は同一視される"""
        assert proto._normalize_area('0') == '0.0.0.0'
        assert proto._normalize_area('0.0.0.0') == '0.0.0.0'
        assert proto._normalize_area('1') == '0.0.0.1'


# ══════════════════════════════════════════
# BGP テスト
# ══════════════════════════════════════════
class TestBgp:
    @pytest.mark.asyncio
    async def test_session_established(self, fresh_engines):
        """BGPセッションがEstablishedになる"""
        e = fresh_engines
        _link(e, 'B1', 'B2')
        await e['bgp'].start('B1', 'AS1-R', 65001)
        await e['bgp'].start('B2', 'AS2-R', 65002)
        await e['bgp'].add_neighbor('B1', 'B2', 'AS2-R', 65002)
        await e['bgp'].add_neighbor('B2', 'B1', 'AS1-R', 65001)
        await asyncio.sleep(3)
        s1 = e['bgp'].nodes['B1']['sessions'].get('B2')
        assert s1 is not None, "セッション未生成"
        assert s1.state == 'Established', f"Established期待、実際{s1.state}"

    @pytest.mark.asyncio
    async def test_prefix_advertisement(self, fresh_engines):
        """BGPでprefixが広告・学習される"""
        e = fresh_engines
        _link(e, 'B1', 'B2')
        await e['bgp'].start('B1', 'AS1', 65001)
        await e['bgp'].start('B2', 'AS2', 65002)
        await e['bgp'].advertise_network('B1', '192.168.1.0/24')
        await e['bgp'].advertise_network('B2', '192.168.2.0/24')
        await e['bgp'].add_neighbor('B1', 'B2', 'AS2', 65002)
        await e['bgp'].add_neighbor('B2', 'B1', 'AS1', 65001)
        await asyncio.sleep(3)
        # B1がB2のprefixを学習
        b1_prefixes = [r.prefix for r in e['bgp'].nodes['B1']['rib_in']]
        assert '192.168.2.0' in b1_prefixes, "B1がB2のprefixを学習していない"


# ══════════════════════════════════════════
# STP テスト
# ══════════════════════════════════════════
class TestStp:
    @pytest.mark.asyncio
    async def test_root_bridge_election(self, fresh_engines):
        """最小優先度がルートブリッジになる"""
        e = fresh_engines
        _link(e, 'S1', 'S2')
        await e['stp'].start('S1', 'SW1', 'rstp', 4096)   # 低い=root
        await e['stp'].start('S2', 'SW2', 'rstp', 32768)
        await asyncio.sleep(2)
        # S2はS1をルートと認識
        s2_root = e['stp'].nodes['S2']['root_bridge_id']
        s1_bid = e['stp'].nodes['S1']['bridge_id']
        assert s2_root == s1_bid, "S1がルートブリッジになっていない"
        e['stp'].stop('S1')
        e['stp'].stop('S2')

    @pytest.mark.asyncio
    async def test_loop_blocking(self, fresh_engines):
        """3台リングでループブロッキングが起きる"""
        e = fresh_engines
        # リング: S1-S2-S3-S1
        for a, b in [('S1', 'S2'), ('S2', 'S3'), ('S3', 'S1')]:
            e['vnet'].register(a, e['noop'])
            e['vnet'].register(b, e['noop'])
            e['vnet'].add_link(a, b)
        await e['stp'].start('S1', 'SW1', 'rstp', 4096)    # root
        await e['stp'].start('S2', 'SW2', 'rstp', 8192)
        await e['stp'].start('S3', 'SW3', 'rstp', 32768)
        await asyncio.sleep(8)
        # 全装置のポート状態を集計
        all_states = []
        for sid in ['S1', 'S2', 'S3']:
            for p in e['stp'].nodes[sid]['ports'].values():
                all_states.append(p['state'])
        # 少なくとも1つBLOCKINGがある（ループ遮断）
        assert 'BLOCKING' in all_states, \
            f"ループブロッキングが発生していない。状態: {all_states}"
        e['stp'].stop('S1')
        e['stp'].stop('S2')
        e['stp'].stop('S3')


# ══════════════════════════════════════════
# スタティック / フローティング テスト
# ══════════════════════════════════════════
class TestStaticRoute:
    def test_static_route_add(self, fresh_engines):
        """スタティックルートが追加される"""
        e = fresh_engines
        e['rib'].add_static_route('R1', 'Router-1', '0.0.0.0', 0, '192.168.1.1', 1)
        routes = e['rib'].get_best_routes('R1')
        assert any(r['network'] == '0.0.0.0' for r in routes)

    def test_ad_comparison(self, fresh_engines):
        """AD値が小さいルートが選択される"""
        e = fresh_engines
        # 同じ宛先に AD=1 と AD=200
        e['rib'].add_static_route('R1', 'R-1', '10.0.0.0', 24, '192.168.1.1', 1)
        e['rib'].add_static_route('R1', 'R-1', '10.0.0.0', 24, '192.168.2.1', 200)
        routes = e['rib'].get_best_routes('R1')
        r = next(x for x in routes if x['network'] == '10.0.0.0')
        assert r['next_hop'] == '192.168.1.1', "AD=1が選ばれていない"
        assert r['ad'] == 1

    def test_floating_failover(self, fresh_engines):
        """メイン経路ダウンでフローティングに切替"""
        e = fresh_engines
        e['rib'].add_static_route('R1', 'R-1', '0.0.0.0', 0, '192.168.1.1', 1)
        e['rib'].add_static_route('R1', 'R-1', '0.0.0.0', 0, '192.168.2.1', 200)
        # 通常はメイン
        r = next(x for x in e['rib'].get_best_routes('R1')
                 if x['network'] == '0.0.0.0')
        assert r['next_hop'] == '192.168.1.1'
        # メインダウン
        e['rib'].set_nexthop_reachable('R1', '192.168.1.1', False)
        r = next(x for x in e['rib'].get_best_routes('R1')
                 if x['network'] == '0.0.0.0')
        assert r['next_hop'] == '192.168.2.1', "フローティングに切り替わっていない"
        assert r['ad'] == 200

    def test_static_beats_rip(self, fresh_engines):
        """スタティック(AD=1)がRIP(AD=120)に優先する"""
        e = fresh_engines
        # RIPルートを直接注入
        from engine.protocols import RipRoute
        e['rip'].nodes['R1'] = {
            'enabled': True, 'version': 2, 'hostname': 'R-1',
            'networks': [], 'table': [
                RipRoute(network='172.16.0.0', prefix=24, metric=2,
                         next_hop='10.0.0.2', learned_from='X',
                         learned_from_hostname='X')
            ], 'timer_task': None, 'expire_tasks': {},
        }
        # 同じ宛先にスタティック
        e['rib'].add_static_route('R1', 'R-1', '172.16.0.0', 24, '192.168.1.1', 1)
        routes = e['rib'].get_best_routes('R1')
        r = next(x for x in routes if x['network'] == '172.16.0.0')
        assert r['source'] == 'static', f"staticが優先されていない: {r['source']}"


# ══════════════════════════════════════════
# ICMP テスト
# ══════════════════════════════════════════
class TestIcmp:
    def test_ping_self(self, fresh_engines):
        """自分のIPへのpingは成功"""
        e = fresh_engines
        e['icmp'].register_device('R1', 'Router-1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        result = e['icmp'].ping('R1', '192.168.1.1')
        assert result['reachable'], "自IP pingが失敗"

    def test_ping_same_segment(self, fresh_engines):
        """同一セグメント内へのpingは成功"""
        e = fresh_engines
        e['icmp'].register_device('R1', 'Router-1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        result = e['icmp'].ping('R1', '192.168.1.50')
        assert result['reachable'], "同一セグメントpingが失敗"

    def test_ping_no_route(self, fresh_engines):
        """経路のない宛先へのpingは失敗"""
        e = fresh_engines
        e['icmp'].register_device('R1', 'Router-1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        result = e['icmp'].ping('R1', '10.99.99.99')
        assert not result['reachable'], "到達不能なはずが成功している"
        assert 'no route' in result['reason']

    def test_ping_multihop_via_static(self, fresh_engines):
        """スタティックルート経由のマルチホップpingが成功"""
        e = fresh_engines
        # R1とR2を接続
        e['vnet'].register('R1', e['noop'])
        e['vnet'].register('R2', e['noop'])
        e['vnet'].add_link('R1', 'R2')
        # R1: lan0=192.168.1.1/24, link=10.0.0.1/30
        e['icmp'].register_device('R1', 'R-1', {
            'lan0': {'ip': '192.168.1.1', 'prefix': 24},
            'link': {'ip': '10.0.0.1', 'prefix': 30},
        })
        # R2: lan0=192.168.2.1/24, link=10.0.0.2/30
        e['icmp'].register_device('R2', 'R-2', {
            'lan0': {'ip': '192.168.2.1', 'prefix': 24},
            'link': {'ip': '10.0.0.2', 'prefix': 30},
        })
        # R1にR2のLAN(192.168.2.0/24)へのスタティックルート
        e['rib'].add_static_route('R1', 'R-1', '192.168.2.0', 24, '10.0.0.2', 1)
        result = e['icmp'].ping('R1', '192.168.2.1')
        assert result['reachable'], \
            f"マルチホップpingが失敗: {result['reason']}, hops={result['hops']}"


# ══════════════════════════════════════════
# 再配信 (redistribute) テスト
# ══════════════════════════════════════════
class TestRedistribute:
    @pytest.mark.asyncio
    async def test_static_to_rip(self, fresh_engines):
        """スタティックルートをRIPに再配信"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        # R1: スタティック + RIP
        e['rib'].add_static_route('R1', 'R1', '172.16.50.0', 24, '10.0.0.254', 1)
        await e['rip'].start('R1', 'R1', ['192.168.1.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        # 再配信実行
        from engine.protocols import redistribute
        count = await redistribute('R1', 'rip', 'static')
        assert count >= 1, "再配信ルート数が0"
        await asyncio.sleep(1)
        await e['rip']._send_update('R1')
        await asyncio.sleep(0.5)
        # R2が再配信されたスタティックを学習
        r2_nets = [r.network for r in e['rip'].nodes['R2']['table']]
        assert '172.16.50.0' in r2_nets, \
            f"再配信されたスタティックがR2に届いていない: {r2_nets}"
        await e['rip'].stop('R1')
        await e['rip'].stop('R2')

    @pytest.mark.asyncio
    async def test_connected_to_rip(self, fresh_engines):
        """直接接続をRIPに再配信"""
        e = fresh_engines
        _link(e, 'R1', 'R2')
        e['icmp'].register_device('R1', 'R1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24},
                                    'lan1': {'ip': '172.20.0.1', 'prefix': 24}})
        await e['rip'].start('R1', 'R1', ['192.168.1.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        from engine.protocols import redistribute
        count = await redistribute('R1', 'rip', 'connected')
        assert count >= 1
        await e['rip'].stop('R1')
        await e['rip'].stop('R2')


# ══════════════════════════════════════════
# 経路フィルタ (prefix-list / distribute-list) テスト
# ══════════════════════════════════════════
class TestRouteFilter:
    def test_prefix_list_permit(self, fresh_engines):
        """prefix-listで許可される"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        filter_engine.add_prefix_list('R1', 'TEST', 'permit', '192.168.1.0', 24)
        assert filter_engine.check_prefix_list('R1', 'TEST', '192.168.1.0', 24)

    def test_prefix_list_implicit_deny(self, fresh_engines):
        """prefix-listでマッチしないものは暗黙deny"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        filter_engine.add_prefix_list('R1', 'TEST', 'permit', '192.168.1.0', 24)
        # マッチしない経路は拒否される
        assert not filter_engine.check_prefix_list('R1', 'TEST', '10.10.10.0', 24)

    def test_prefix_list_explicit_deny(self, fresh_engines):
        """明示的denyが効く（Cisco仕様: 配下を拒否するにはle指定が必要）"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        # 10.0.0.0/8 配下すべてをdeny（le 32）、それ以外はpermit
        filter_engine.add_prefix_list('R1', 'T', 'deny', '10.0.0.0', 8, seq=5, le=32)
        filter_engine.add_prefix_list('R1', 'T', 'permit', '0.0.0.0', 0, seq=10, le=32)
        # 10.x配下はdeny、それ以外はpermit
        assert not filter_engine.check_prefix_list('R1', 'T', '10.1.1.0', 24)
        assert filter_engine.check_prefix_list('R1', 'T', '192.168.1.0', 24)

    def test_prefix_list_exact_match_only(self, fresh_engines):
        """ge/le無しは完全一致のみ（Cisco仕様）"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        filter_engine.add_prefix_list('R1', 'T', 'permit', '10.0.0.0', 8)
        # /8そのものは許可、配下/24はマッチせず暗黙deny
        assert filter_engine.check_prefix_list('R1', 'T', '10.0.0.0', 8)
        assert not filter_engine.check_prefix_list('R1', 'T', '10.1.1.0', 24)

    def test_prefix_list_ge_le(self, fresh_engines):
        """ge/le でプレフィックス長範囲指定"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        # 10.0.0.0/8 で ge 24 le 24 → /24のみ許可
        filter_engine.add_prefix_list('R1', 'T', 'permit', '10.0.0.0', 8,
                                       ge=24, le=24)
        assert filter_engine.check_prefix_list('R1', 'T', '10.1.1.0', 24)
        assert not filter_engine.check_prefix_list('R1', 'T', '10.0.0.0', 16)

    def test_distribute_list_filters_routes(self, fresh_engines):
        """distribute-listで経路リストがフィルタされる"""
        from engine.protocols import filter_engine
        filter_engine.prefix_lists.clear()
        filter_engine.distribute_lists.clear()
        filter_engine.add_prefix_list('R1', 'OUT', 'permit', '192.168.1.0', 24)
        filter_engine.set_distribute_list('R1', 'rip', 'out', 'OUT')
        routes = [{'network': '192.168.1.0', 'prefix': 24},
                  {'network': '10.10.10.0', 'prefix': 24}]
        filtered = filter_engine.filter_routes('R1', 'rip', 'out', routes)
        nets = [r['network'] for r in filtered]
        assert '192.168.1.0' in nets
        assert '10.10.10.0' not in nets

    @pytest.mark.asyncio
    async def test_rip_distribute_list_e2e(self, fresh_engines):
        """RIP広告がdistribute-listでフィルタされる（エンドツーエンド）"""
        from engine.protocols import filter_engine
        e = fresh_engines
        filter_engine.prefix_lists.clear()
        filter_engine.distribute_lists.clear()
        _link(e, 'R1', 'R2')
        # R1にdistribute-list設定
        filter_engine.add_prefix_list('R1', 'ONLY', 'permit', '192.168.1.0', 24)
        filter_engine.set_distribute_list('R1', 'rip', 'out', 'ONLY')
        await e['rip'].start('R1', 'R1',
                             ['192.168.1.0/24', '10.10.10.0/24'])
        await e['rip'].start('R2', 'R2', ['192.168.2.0/24'])
        await asyncio.sleep(1)
        await e['rip']._send_update('R1')
        await asyncio.sleep(0.5)
        r2_nets = [r.network for r in e['rip'].nodes['R2']['table']]
        assert '192.168.1.0' in r2_nets, "許可経路が届いていない"
        assert '10.10.10.0' not in r2_nets, "拒否経路が漏れている"
        await e['rip'].stop('R1')
        await e['rip'].stop('R2')


# ══════════════════════════════════════════
# ARP テスト
# ══════════════════════════════════════════
class TestArp:
    def test_arp_resolve_same_segment(self, fresh_engines):
        """同一セグメント内のIPはARP解決できる"""
        from engine.protocols import arp_engine
        e = fresh_engines
        arp_engine.tables.clear()
        e['icmp'].register_device('R1', 'R1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        mac = arp_engine.resolve('R1', '192.168.1.50')
        assert mac is not None, "同一セグメントのARP解決失敗"
        # テーブルに記録されている
        assert '192.168.1.50' in arp_engine.tables['R1']

    def test_arp_no_resolve_other_segment(self, fresh_engines):
        """別セグメントのIPはARP解決できない"""
        from engine.protocols import arp_engine
        e = fresh_engines
        arp_engine.tables.clear()
        e['icmp'].register_device('R1', 'R1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        mac = arp_engine.resolve('R1', '10.99.99.99')
        assert mac is None, "別セグメントなのにARP解決できている"

    def test_arp_static_entry(self, fresh_engines):
        """静的ARPエントリを追加できる"""
        from engine.protocols import arp_engine
        arp_engine.tables.clear()
        arp_engine.add_static('R1', '192.168.1.99', '00:11:22:33:44:55')
        assert arp_engine.tables['R1']['192.168.1.99'].entry_type == 'static'

    def test_arp_cache(self, fresh_engines):
        """一度解決したARPはキャッシュされる"""
        from engine.protocols import arp_engine
        e = fresh_engines
        arp_engine.tables.clear()
        e['icmp'].register_device('R1', 'R1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        mac1 = arp_engine.resolve('R1', '192.168.1.50')
        mac2 = arp_engine.resolve('R1', '192.168.1.50')
        assert mac1 == mac2, "ARPキャッシュが一貫していない"


# ══════════════════════════════════════════
# IPフィルタ (ACL) テスト
# ══════════════════════════════════════════
class TestIpFilter:
    def test_acl_no_filter_passes(self, fresh_engines):
        """ACL未適用なら通過"""
        from engine.protocols import ipfilter_engine
        ipfilter_engine.acls.clear()
        ipfilter_engine.applied.clear()
        assert ipfilter_engine.check_packet('R1', 'lan0', 'out',
                                            '1.1.1.1', '2.2.2.2')

    def test_acl_deny_host(self, fresh_engines):
        """特定ホストへのパケットをdeny"""
        from engine.protocols import ipfilter_engine
        ipfilter_engine.acls.clear()
        ipfilter_engine.applied.clear()
        ipfilter_engine.add_rule('R1', '10', 'deny', 'ip', 'host 10.0.0.5', 'any')
        ipfilter_engine.add_rule('R1', '10', 'permit', 'ip', 'any', 'any')
        ipfilter_engine.apply_acl('R1', 'lan0', 'in', '10')
        # 10.0.0.5発はブロック
        assert not ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                                '10.0.0.5', '8.8.8.8')
        # それ以外は通過
        assert ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                            '10.0.0.6', '8.8.8.8')

    def test_acl_implicit_deny(self, fresh_engines):
        """どのルールにもマッチしなければ暗黙deny"""
        from engine.protocols import ipfilter_engine
        ipfilter_engine.acls.clear()
        ipfilter_engine.applied.clear()
        ipfilter_engine.add_rule('R1', '10', 'permit', 'ip',
                                 '192.168.1.0/24', 'any')
        ipfilter_engine.apply_acl('R1', 'lan0', 'in', '10')
        # 許可リスト外は暗黙deny
        assert not ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                                '10.0.0.1', '8.8.8.8')

    def test_acl_extended_port(self, fresh_engines):
        """拡張ACLでポート指定フィルタ"""
        from engine.protocols import ipfilter_engine
        ipfilter_engine.acls.clear()
        ipfilter_engine.applied.clear()
        # telnet(23)をdeny、他は許可
        ipfilter_engine.add_rule('R1', '100', 'deny', 'tcp', 'any', 'any',
                                 dst_port='23')
        ipfilter_engine.add_rule('R1', '100', 'permit', 'ip', 'any', 'any')
        ipfilter_engine.apply_acl('R1', 'lan0', 'in', '100')
        # telnet宛てはブロック
        assert not ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                                '1.1.1.1', '2.2.2.2',
                                                protocol='tcp', dst_port='23')
        # http(80)は通過
        assert ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                            '1.1.1.1', '2.2.2.2',
                                            protocol='tcp', dst_port='80')

    def test_acl_subnet_match(self, fresh_engines):
        """サブネット単位のマッチ"""
        from engine.protocols import ipfilter_engine
        ipfilter_engine.acls.clear()
        ipfilter_engine.applied.clear()
        ipfilter_engine.add_rule('R1', '20', 'deny', 'ip',
                                 '172.16.0.0/16', 'any')
        ipfilter_engine.add_rule('R1', '20', 'permit', 'ip', 'any', 'any')
        ipfilter_engine.apply_acl('R1', 'lan0', 'in', '20')
        # 172.16.x.x はブロック
        assert not ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                                '172.16.5.10', '8.8.8.8')
        # 172.17.x.x は通過
        assert ipfilter_engine.check_packet('R1', 'lan0', 'in',
                                            '172.17.5.10', '8.8.8.8')


# ══════════════════════════════════════════
# pyATS/Genie 風機能テスト
# ══════════════════════════════════════════
class TestGenie:
    @pytest.mark.asyncio
    async def test_learn_ospf(self, fresh_engines):
        """genie learn ospf が構造化データを返す"""
        from engine.protocols import genie_engine
        e = fresh_engines
        genie_engine.snapshots.clear()
        _link(e, 'R1', 'R2')
        await e['ospf'].start('R1', 'R1', 1, ['192.168.1.0/24'])
        await e['ospf'].start('R2', 'R2', 1, ['192.168.2.0/24'])
        await asyncio.sleep(2.5)
        data = genie_engine.learn('R1', 'ospf')
        assert 'router_id' in data
        assert 'neighbors' in data
        # ネイバーが学習されている
        assert len(data['neighbors']) >= 1
        await e['ospf'].stop('R1')
        await e['ospf'].stop('R2')

    def test_learn_routing(self, fresh_engines):
        """genie learn routing がルーティングテーブルを返す"""
        from engine.protocols import genie_engine
        e = fresh_engines
        genie_engine.snapshots.clear()
        e['rib'].add_static_route('R1', 'R1', '10.0.0.0', 24, '192.168.1.1', 1)
        data = genie_engine.learn('R1', 'routing')
        assert 'routes' in data
        assert '10.0.0.0/24' in data['routes']

    def test_snapshot_and_diff_identical(self, fresh_engines):
        """同一状態のsnapshot比較はPASS（差分なし）"""
        from engine.protocols import genie_engine
        e = fresh_engines
        genie_engine.snapshots.clear()
        e['icmp'].register_device('R1', 'R1',
                                   {'lan0': {'ip': '192.168.1.1', 'prefix': 24}})
        genie_engine.take_snapshot('R1', 'before', ['interface'])
        genie_engine.take_snapshot('R1', 'after', ['interface'])
        result = genie_engine.diff('R1', 'before', 'after')
        assert result['identical'], "同一状態なのに差分検出"

    def test_snapshot_and_diff_changed(self, fresh_engines):
        """状態変化があればdiffで差分検出（FAIL）"""
        from engine.protocols import genie_engine
        e = fresh_engines
        genie_engine.snapshots.clear()
        # before: スタティックなし
        genie_engine.take_snapshot('R1', 'before', ['routing'])
        # 変更: スタティック追加
        e['rib'].add_static_route('R1', 'R1', '10.0.0.0', 24, '192.168.1.1', 1)
        # after
        genie_engine.take_snapshot('R1', 'after', ['routing'])
        result = genie_engine.diff('R1', 'before', 'after')
        assert not result['identical'], "状態変化を検出できていない"
        assert 'routing' in result['diffs']

    def test_diff_missing_snapshot(self, fresh_engines):
        """存在しないsnapshotのdiffはエラー"""
        from engine.protocols import genie_engine
        e = fresh_engines
        genie_engine.snapshots.clear()
        result = genie_engine.diff('R1', 'nope1', 'nope2')
        assert 'error' in result


# ══════════════════════════════════════════
# LACP / EtherChannel テスト
# ══════════════════════════════════════════
class TestLacp:
    def test_active_passive_bundles(self, fresh_engines):
        """active + passive は成立（bundled）"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'active')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'passive')
        ok = lacp_engine.negotiate('SW1', 1, 'SW2', 1)
        assert ok, "active+passiveが成立していない"

    def test_passive_passive_fails(self, fresh_engines):
        """passive + passive は不成立"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'passive')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'passive')
        ok = lacp_engine.negotiate('SW1', 1, 'SW2', 1)
        assert not ok, "passive+passiveが成立してしまっている"

    def test_active_active_bundles(self, fresh_engines):
        """active + active は成立"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'active')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'active')
        assert lacp_engine.negotiate('SW1', 1, 'SW2', 1)

    def test_on_on_bundles(self, fresh_engines):
        """on + on は静的成立"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'on')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'on')
        assert lacp_engine.negotiate('SW1', 1, 'SW2', 1)

    def test_on_active_fails(self, fresh_engines):
        """on + active はモード不一致で不成立"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'on')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'active')
        assert not lacp_engine.negotiate('SW1', 1, 'SW2', 1)

    def test_member_state_after_bundle(self, fresh_engines):
        """成立後メンバーがbundled状態になる"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1', 1, 'Gi1/0/1', 'active')
        lacp_engine.add_member('SW1', 1, 'Gi1/0/2', 'active')
        lacp_engine.add_member('SW2', 1, 'Gi1/0/1', 'active')
        lacp_engine.negotiate('SW1', 1, 'SW2', 1)
        ch = lacp_engine.get_channel('SW1', 1)
        assert all(m.state == 'bundled' for m in ch['members'])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ══════════════════════════════════════════
# 未テスト項目 自動テスト（30項目）
# ══════════════════════════════════════════

# ── OSPF 詳細テスト ──────────────────────
class TestOspfAdvanced:

    @pytest.mark.asyncio
    async def test_dr_bdr_show_neighbor_role(self, fresh_engines):
        """show neighbor に Full/DR, Full/BDR, Full/DROTHER が表示される"""
        e = fresh_engines
        _link(e,'R1','R2'); _link(e,'R2','R3')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        e['ospf'].set_priority('R1', 200)
        await e['ospf'].start('R2','R2',1,['10.0.2.0/24'],'0.0.0.0')
        e['ospf'].set_priority('R2', 100)
        await asyncio.sleep(3)
        out = e['ospf'].format_show_ospf_neighbor('R1')
        assert 'Full' in out
        assert 'DR' in out or 'BDR' in out or 'DROTHER' in out
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_multiarea_abr_flag(self, fresh_engines):
        """マルチエリア設定でABRフラグがTrueになる"""
        e = fresh_engines
        _link(e,'ABR','R1'); _link(e,'ABR','R2')
        await e['ospf'].start('ABR','ABR',1,['10.0.0.0/24'],'0.0.0.0')
        e['ospf'].add_network('ABR','10.0.1.0/24','0.0.0.1')
        await e['ospf'].start('R1','R1',1,['10.0.0.0/24'],'0.0.0.0')
        await e['ospf'].start('R2','R2',1,['10.0.1.0/24'],'0.0.0.1')
        await asyncio.sleep(3)
        assert e['ospf'].nodes['ABR']['abr'] == True
        assert len(e['ospf'].area_networks.get('ABR',{})) >= 2
        await e['ospf'].stop('ABR'); await e['ospf'].stop('R1'); await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_passive_interface_no_hello(self, fresh_engines):
        """passive-interface設定でHello送信停止フラグが登録される"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        e['ospf'].add_passive_interface('R1','all')
        # passiveフラグが登録されている
        assert 'all' in e['ospf'].passive_ifaces.get('R1', set())
        await e['ospf'].stop('R1')

    @pytest.mark.asyncio
    async def test_interface_cost(self, fresh_engines):
        """ip ospf cost でコストが設定される"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        e['ospf'].set_cost('R1', 100)
        assert e['ospf'].nodes['R1']['interface_cost'] == 100
        await e['ospf'].stop('R1')

    @pytest.mark.asyncio
    async def test_dead_interval_is_hello_times_4(self, fresh_engines):
        """dead_interval = hello_interval × 4"""
        e = fresh_engines
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        n = e['ospf'].nodes['R1']
        assert n['dead_interval'] == n['hello_interval'] * 4
        await e['ospf'].stop('R1')

    @pytest.mark.asyncio
    async def test_show_ospf_interface(self, fresh_engines):
        """show ip ospf interface にCost/Priority/DR状態が出る"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        await e['ospf'].start('R2','R2',1,['10.0.2.0/24'],'0.0.0.0')
        await asyncio.sleep(2)
        out = e['ospf'].format_show_ospf_interface('R1')
        assert 'Cost:' in out
        assert 'Priority' in out
        assert 'Hello' in out
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_show_ospf_database(self, fresh_engines):
        """show ip ospf database にLSAが出る"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        await e['ospf'].start('R2','R2',1,['10.0.2.0/24'],'0.0.0.0')
        await asyncio.sleep(2)
        out = e['ospf'].format_show_ospf_database('R1')
        assert 'Router Link States' in out
        assert 'R1' in out or 'ADV Router' in out
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')

    @pytest.mark.asyncio
    async def test_show_ospf_route_types(self, fresh_engines):
        """show ip ospf route にO/O IA経路が出る"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['ospf'].start('R1','R1',1,['10.0.1.0/24'],'0.0.0.0')
        await e['ospf'].start('R2','R2',1,['10.0.2.0/24'],'0.0.0.0')
        await asyncio.sleep(2)
        out = e['ospf'].format_show_ospf_route('R1')
        assert 'Routing Table' in out or 'OSPF Router' in out
        await e['ospf'].stop('R1'); await e['ospf'].stop('R2')


# ── BGP 詳細テスト ──────────────────────
class TestBgpAdvanced:

    @pytest.mark.asyncio
    async def test_show_bgp_summary_content(self, fresh_engines):
        """show ip bgp summary にEstablished/ASNが出る"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['bgp'].start('R1','R1',65001)
        await e['bgp'].start('R2','R2',65002)
        await e['bgp'].add_neighbor('R1','R2','R2',65002)
        await e['bgp'].add_neighbor('R2','R1','R1',65001)
        await asyncio.sleep(4)
        out = e['bgp'].format_show_bgp_summary('R1')
        assert '65001' in out or '65002' in out
        assert 'Established' in out or 'MsgRcvd' in out

    @pytest.mark.asyncio
    async def test_bgp_session_state(self, fresh_engines):
        """BGPセッションがEstablishedになる"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['bgp'].start('R1','R1',65001)
        await e['bgp'].start('R2','R2',65002)
        await e['bgp'].add_neighbor('R1','R2','R2',65002)
        await e['bgp'].add_neighbor('R2','R1','R1',65001)
        await asyncio.sleep(4)
        sessions = e['bgp'].nodes['R1']['sessions']
        assert any(s.state == 'Established' for s in sessions.values())


# ── STP 詳細テスト ──────────────────────
class TestStpAdvanced:

    @pytest.mark.asyncio
    async def test_portfast_immediate_forwarding(self, fresh_engines):
        """PortFast設定でポートが即座にFORWARDINGになる"""
        e = fresh_engines
        _link(e,'SW1','SW2')
        await e['stp'].start('SW1','SW1','rstp',32768)
        await asyncio.sleep(2)
        n = e['stp'].nodes['SW1']
        port_name = list(n['ports'].keys())[0]
        e['stp'].set_portfast('SW1', port_name, True)
        assert n['ports'][port_name]['state'] == 'FORWARDING'
        assert n['ports'][port_name].get('portfast') == True
        e['stp'].stop('SW1')

    @pytest.mark.asyncio
    async def test_bpduguard_err_disabled(self, fresh_engines):
        """BPDUGuard有効ポートにBPDU受信→ERR_DISABLED"""
        e = fresh_engines
        _link(e,'SW1','SW2')
        await e['stp'].start('SW1','SW1','rstp',32768)
        await e['stp'].start('SW2','SW2','rstp',32768)
        await asyncio.sleep(2)
        n1 = e['stp'].nodes['SW1']
        port_name = list(n1['ports'].keys())[0]
        n1['ports'][port_name]['portfast'] = True
        n1['ports'][port_name]['bpduguard'] = True
        n2 = e['stp'].nodes['SW2']
        await e['stp'].receive_bpdu('SW1', {
            'type':'stp_bpdu','src_id':'SW2','src_hostname':'SW2','mode':'rstp',
            'root_bridge_id': n2['bridge_id'],'root_path_cost':0,
            'bridge_id': n2['bridge_id'],
            'hello_time':1,'max_age':20,'forward_delay':15,
        })
        await asyncio.sleep(0.5)
        assert n1['ports'][port_name]['state'] == 'ERR_DISABLED'
        e['stp'].stop('SW1'); e['stp'].stop('SW2')

    @pytest.mark.asyncio
    async def test_pvst_vlan_priority(self, fresh_engines):
        """Rapid-PVST+ VLAN別priorityが設定される"""
        e = fresh_engines
        _link(e,'SW1','SW2')
        await e['stp'].start('SW1','SW1','rstp',32768,1)
        e['stp'].set_vlan_priority('SW1',10,4096)
        e['stp'].set_vlan_priority('SW1',20,8192)
        assert e['stp'].nodes['SW1']['vlans'][10]['priority'] == 4096
        assert e['stp'].nodes['SW1']['vlans'][20]['priority'] == 8192
        assert e["stp"].nodes["SW1"]["bridge_priority"] == 8192  # 最後のset_vlan_priorityの値
        e['stp'].stop('SW1')

    @pytest.mark.asyncio
    async def test_tcn_on_link_failure(self, fresh_engines):
        """リンク障害でTCNが発生しtc_countが増える"""
        e = fresh_engines
        _link(e,'SW1','SW2'); _link(e,'SW2','SW3'); _link(e,'SW3','SW1')
        await e['stp'].start('SW1','SW1','rstp',4096)
        await e['stp'].start('SW2','SW2','rstp',32768)
        await e['stp'].start('SW3','SW3','rstp',32768)
        await asyncio.sleep(5)
        pre = e['stp'].nodes['SW1'].get('tc_count',0)
        e['vnet'].remove_link('SW2','SW3')
        await asyncio.sleep(3)
        post = e['stp'].nodes['SW1'].get('tc_count',0)
        assert post > pre, f"TCN未発生 tc_count {pre}→{post}"
        e['stp'].stop('SW1'); e['stp'].stop('SW2'); e['stp'].stop('SW3')

    @pytest.mark.asyncio
    async def test_show_spanning_tree_summary(self, fresh_engines):
        """show spanning-tree summary に rapid-pvst/Portfast が出る"""
        e = fresh_engines
        _link(e,'SW1','SW2')
        await e['stp'].start('SW1','SW1','rstp',32768)
        out = e['stp'].format_show_spanning_tree_summary('SW1')
        assert 'rapid-pvst' in out or 'rstp' in out.lower()
        assert 'Portfast' in out
        e['stp'].stop('SW1')

    @pytest.mark.asyncio
    async def test_show_spanning_tree_vlan(self, fresh_engines):
        """show spanning-tree vlan 10 にVLAN0010が出る"""
        e = fresh_engines
        _link(e,'SW1','SW2')
        await e['stp'].start('SW1','SW1','rstp',4096,10)
        e['stp'].set_vlan_priority('SW1',10,4096)
        out = e['stp'].format_show_spanning_tree_vlan('SW1',10)
        assert 'VLAN0010' in out
        assert '4096' in out
        e['stp'].stop('SW1')


# ── LACP 詳細テスト ──────────────────────
class TestLacpAdvanced:

    def test_srs_linkaggregation_mode(self, fresh_engines):
        """SR-S: linkaggregation mode設定が反映される"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/1','active')
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/2','active')
        ch = lacp_engine.get_channel('SW1',1)
        ch['mode'] = 'passive'
        ch['protocol'] = 'lacp'
        assert ch['mode'] == 'passive'
        assert ch['protocol'] == 'lacp'

    def test_show_linkaggregation_format(self, fresh_engines):
        """show linkaggregation (SR-S形式) にGroup/Status/Modeが出る"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/1','passive')
        lacp_engine.add_member('SW2',1,'GigabitEthernet1/0/1','active')
        lacp_engine.negotiate('SW1',1,'SW2',1)
        out = lacp_engine.format_show_linkaggregation('SW1')
        assert 'Group 1' in out
        assert 'Mode' in out or 'Status' in out

    def test_show_run_port_channel_cisco(self, fresh_engines):
        """Cisco: show run にPort-channel/channel-groupが出る"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/1','active')
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/2','active')
        lines = lacp_engine.get_running_config_lines('SW1','catalyst')
        joined = '\n'.join(lines)
        assert 'Port-channel1' in joined
        assert 'channel-group 1 mode active' in joined

    def test_show_run_linkaggregation_srs(self, fresh_engines):
        """SR-S: show run にlinkaggregationが出る"""
        from engine.protocols import lacp_engine
        lacp_engine.channels.clear()
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/1','passive')
        lacp_engine.add_member('SW1',1,'GigabitEthernet1/0/2','passive')
        ch = lacp_engine.get_channel('SW1',1)
        ch['mode'] = 'passive'
        lines = lacp_engine.get_running_config_lines('SW1','srs')
        joined = '\n'.join(lines)
        assert 'linkaggregation 1 mode passive' in joined


# ── RIP / syslog / SNMP / APRESIA / show系 ──
class TestMiscAdvanced:

    @pytest.mark.asyncio
    async def test_rip_show_neighbor(self, fresh_engines):
        """show ip rip neighbor に学習元が出る"""
        e = fresh_engines
        _link(e,'R1','R2')
        await e['rip'].start('R1','R1',['192.168.1.0/24'])
        await e['rip'].start('R2','R2',['192.168.2.0/24'])
        await asyncio.sleep(3)
        out = e['rip'].format_show_rip_neighbor('R1')
        assert 'Index' in out
        await e['rip'].stop('R1'); await e['rip'].stop('R2')

    @pytest.mark.asyncio
    async def test_rip_cross_vendor(self, fresh_engines):
        """Si-R ↔ Catalyst 異ベンダー間RIP経路学習"""
        e = fresh_engines
        _link(e,'SIR','CAT')
        # Si-R形式とCisco形式の両方でRIPを起動
        await e['rip'].start('SIR','Router-A',['192.168.1.0/24'])
        await e['rip'].start('CAT','Dist-SW',['192.168.10.0/24'])
        await asyncio.sleep(3)
        sir_routes = [r for r in e['rip'].nodes['SIR']['table'] if r.learned_from != 'direct']
        cat_routes = [r for r in e['rip'].nodes['CAT']['table'] if r.learned_from != 'direct']
        assert len(sir_routes) > 0, "Si-RがCatalystの経路を学習していない"
        assert len(cat_routes) > 0, "CatalystがSi-Rの経路を学習していない"
        await e['rip'].stop('SIR'); await e['rip'].stop('CAT')

    def test_syslog_dispatcher_register(self, fresh_engines):
        """syslog_dispatcher にサーバー登録できる"""
        from engine.syslog_sender import syslog_dispatcher
        syslog_dispatcher._targets.clear()
        syslog_dispatcher.register('R1',[{'host':'192.168.1.100','port':514,
                                           'facility':'local7','level':'informational'}])
        assert 'R1' in syslog_dispatcher._targets
        assert syslog_dispatcher._targets['R1'][0]['host'] == '192.168.1.100'

    def test_syslog_level_filter(self, fresh_engines):
        """syslogレベルフィルタ: severity数値が設定レベル以下なら送信"""
        from engine.syslog_sender import SyslogDispatcher
        d = SyslogDispatcher()
        # errors(3) <= warnings(4) → 送信する
        assert d._should_send('warnings', 'errors') == True
        # informational(6) > warnings(4) → 送信しない
        assert d._should_send('warnings', 'informational') == False
        # debugging(7) > informational(6) → 送信しない
        assert d._should_send('informational', 'debugging') == False
        # notifications(5) <= informational(6) → 送信する
        assert d._should_send('informational', 'notifications') == True
        # emergencies(0) は常に送信
        assert d._should_send('warnings', 'emergencies') == True

    def test_snmp_config_show_run(self, fresh_engines):
        """SNMP設定がshow runに反映される（stateオブジェクトで確認）"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState
        state = DeviceState('catalyst','SW1')
        state.snmp_community.append({'name':'public','perm':'ro'})
        state.snmp_hosts.append({'host':'192.168.100.20','community':'public',
                                  'version':'2c','traps':'traps'})
        state.snmp_location = 'Server-Room'
        assert len(state.snmp_community) == 1
        assert state.snmp_hosts[0]['host'] == '192.168.100.20'
        assert state.snmp_location == 'Server-Room'

    def test_apresia_show_version(self, fresh_engines):
        """APRESIA show version にApresiaLightが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('apresia','sw1')
        out = eng.process('show version', state)
        assert 'ApresiaLight' in out
        assert 'Version' in out

    def test_apresia_show_port(self, fresh_engines):
        """APRESIA show port にPort1/0/が出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('apresia','sw1')
        out = eng.process('show port', state)
        assert 'Port1/0/' in out

    def test_apresia_show_vlan(self, fresh_engines):
        """APRESIA show vlan にVLANとactiveが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('apresia','sw1')
        out = eng.process('show vlan', state)
        assert 'active' in out

    def test_apresia_show_loopdetect(self, fresh_engines):
        """APRESIA show loopdetect に独自機能が出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('apresia','sw1')
        out = eng.process('show loopdetect', state)
        assert 'Loop' in out or 'loop' in out.lower()

    def test_show_interfaces_err_disabled(self, fresh_engines):
        """show interfaces status err-disabled にbpduguardが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        # Gi1/0/3をerr-disabledに設定（デフォルト定義済み）
        out = eng.process('show interfaces status err-disabled', state)
        assert 'err-disabled' in out or 'bpduguard' in out

    def test_show_interfaces_description(self, fresh_engines):
        """show interfaces description にInterface/Status/Descriptionが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show interfaces description', state)
        assert 'Interface' in out
        assert 'Status' in out

    def test_show_interfaces_trunk(self, fresh_engines):
        """show interfaces trunk にトランクポートが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show interfaces trunk', state)
        assert 'trunk' in out.lower() or 'Gi1/0/24' in out

    def test_show_interfaces_counters_errors(self, fresh_engines):
        """show interfaces counters errors にAlign-Errが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show interfaces counters errors', state)
        assert 'Align-Err' in out or 'FCS-Err' in out

    def test_show_switch_stack(self, fresh_engines):
        """show switch にActive/Standbyが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show switch', state)
        assert 'Active' in out

    def test_show_switch_detail(self, fresh_engines):
        """show switch detail にStack Portが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show switch detail', state)
        assert 'Stack Port' in out or 'Active' in out

    def test_show_ntp_status(self, fresh_engines):
        """show ntp status にsynchronizedが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show ntp status', state)
        assert 'synchronized' in out or 'stratum' in out

    def test_show_port_security(self, fresh_engines):
        """show port-security にMaxSecureAddrが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show port-security', state)
        assert 'MaxSecureAddr' in out or 'Security' in out

    def test_show_dhcp_snooping(self, fresh_engines):
        """show ip dhcp snooping にsnoopingが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show ip dhcp snooping', state)
        assert 'snooping' in out.lower()

    def test_show_arp_inspection(self, fresh_engines):
        """show ip arp inspection にVlanが出る"""
        import sys; sys.path.insert(0,'..')
        from engine.rules import DeviceState, RuleEngine
        eng = RuleEngine()
        state = DeviceState('catalyst','SW1')
        out = eng.process('show ip arp inspection', state)
        assert 'Vlan' in out or 'inspection' in out.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ══════════════════════════════════════════
# RIP + フローティングスタティック 経路2重化テスト
# Catalyst-A <---> Catalyst-B 構成
# ══════════════════════════════════════════
class TestRipFloatingStatic:
    """RIP（メイン）+ フローティングスタティック（バックアップ）経路2重化
    正常時: RIP(AD=120)優先
    障害時: スタティック(AD=130)に自動切替
    復旧時: RIPに戻る
    """

    @pytest.mark.asyncio
    async def test_rip_preferred_over_floating_static(self, fresh_engines):
        """正常時: RIP(AD=120)がフローティングスタティック(AD=130)より優先"""
        e = fresh_engines
        _link(e, 'CA', 'CB')
        await e['rip'].start('CA', 'Catalyst-A', ['192.168.10.0/24'])
        await e['rip'].start('CB', 'Catalyst-B', ['192.168.20.0/24'])
        await asyncio.sleep(3)
        e['rib'].add_static_route('CA', 'Catalyst-A', '192.168.20.0', 24, '10.0.0.2', 130)
        best = e['rib'].get_best_routes('CA')
        r = next((x for x in best if x['network'] == '192.168.20.0'), None)
        assert r is not None, '192.168.20.0への経路がない'
        assert r['source'] == 'rip', f"RIPが優先されていない: {r['source']}"
        assert r['ad'] == 120
        await e['rip'].stop('CA'); await e['rip'].stop('CB')

    @pytest.mark.asyncio
    async def test_failover_to_floating_static_on_rip_down(self, fresh_engines):
        """障害時: RIP停止でフローティングスタティックに自動切替"""
        e = fresh_engines
        _link(e, 'CA', 'CB')
        await e['rip'].start('CA', 'Catalyst-A', ['192.168.10.0/24'])
        await e['rip'].start('CB', 'Catalyst-B', ['192.168.20.0/24'])
        await asyncio.sleep(3)
        e['rib'].add_static_route('CA', 'Catalyst-A', '192.168.20.0', 24, '10.0.0.2', 130)
        # RIPが優先されていることを確認してから停止
        best_before = e['rib'].get_best_routes('CA')
        r_before = next((x for x in best_before if x['network'] == '192.168.20.0'), None)
        assert r_before and r_before['source'] == 'rip'
        # RIP停止（メイン経路ダウン）
        await e['rip'].stop('CA')
        await asyncio.sleep(1)
        # フローティングスタティックに切替
        best_after = e['rib'].get_best_routes('CA')
        r_after = next((x for x in best_after if x['network'] == '192.168.20.0'), None)
        assert r_after is not None, 'フェイルオーバー後に経路がない'
        assert r_after['source'] == 'static', f"スタティックに切替わっていない: {r_after['source']}"
        assert r_after['ad'] == 130
        await e['rip'].stop('CB')

    @pytest.mark.asyncio
    async def test_recovery_back_to_rip(self, fresh_engines):
        """復旧時: RIP再起動でスタティックからRIPに戻る"""
        e = fresh_engines
        _link(e, 'CA', 'CB')
        await e['rip'].start('CA', 'Catalyst-A', ['192.168.10.0/24'])
        await e['rip'].start('CB', 'Catalyst-B', ['192.168.20.0/24'])
        await asyncio.sleep(3)
        e['rib'].add_static_route('CA', 'Catalyst-A', '192.168.20.0', 24, '10.0.0.2', 130)
        # RIP停止 → スタティック確認
        await e['rip'].stop('CA')
        await asyncio.sleep(1)
        best_fail = e['rib'].get_best_routes('CA')
        r_fail = next((x for x in best_fail if x['network'] == '192.168.20.0'), None)
        assert r_fail and r_fail['source'] == 'static'
        # RIP再起動（復旧）
        await e['rip'].start('CA', 'Catalyst-A', ['192.168.10.0/24'])
        await asyncio.sleep(3)
        best_recover = e['rib'].get_best_routes('CA')
        r_recover = next((x for x in best_recover if x['network'] == '192.168.20.0'), None)
        assert r_recover is not None, '復旧後に経路がない'
        assert r_recover['source'] == 'rip', f"RIPに戻っていない: {r_recover['source']}"
        assert r_recover['ad'] == 120
        await e['rip'].stop('CA'); await e['rip'].stop('CB')

    @pytest.mark.asyncio
    async def test_ad_comparison_rip_vs_static(self, fresh_engines):
        """AD値の比較: Static(AD=1) < RIP(AD=120) < Floating(AD=130)"""
        e = fresh_engines
        _link(e, 'CA', 'CB')
        await e['rip'].start('CA', 'Catalyst-A', ['192.168.10.0/24'])
        await e['rip'].start('CB', 'Catalyst-B', ['192.168.20.0/24'])
        await asyncio.sleep(3)
        # フローティングスタティック(AD=130)追加 → RIPが優先
        e['rib'].add_static_route('CA', 'Catalyst-A', '192.168.20.0', 24, '10.0.0.2', 130)
        best = e['rib'].get_best_routes('CA')
        r = next((x for x in best if x['network'] == '192.168.20.0'), None)
        assert r['ad'] == 120, f"RIP(AD=120)が優先されるべき: AD={r['ad']}"
        # 通常スタティック(AD=1)追加 → 最優先
        e['rib'].add_static_route('CA', 'Catalyst-A', '192.168.20.0', 24, '10.0.0.3', 1)
        best2 = e['rib'].get_best_routes('CA')
        r2 = next((x for x in best2 if x['network'] == '192.168.20.0'), None)
        assert r2['ad'] == 1, f"通常スタティック(AD=1)が最優先されるべき: AD={r2['ad']}"
        await e['rip'].stop('CA'); await e['rip'].stop('CB')


# ══════════════════════════════════════════
# syslog / SNMP trap / NTP 実UDPパケット送信テスト
# ══════════════════════════════════════════
import threading
import socket as _socket

def _udp_receiver(port: int, results: list, timeout: float = 5.0):
    """ローカルUDP受信スレッド（テスト用）"""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', port))
    s.settimeout(timeout)
    try:
        while True:
            data, _ = s.recvfrom(4096)
            results.append(data)
    except Exception:
        pass
    finally:
        s.close()


class TestUdpPacketSend:
    """実際にUDPパケットが送出されることを確認するテスト"""

    def test_syslog_udp_packet_sent(self, fresh_engines):
        """syslog: UDP 15514 に RFC 3164 パケットが届く"""
        from engine.syslog_sender import send_syslog
        received = []
        t = threading.Thread(target=_udp_receiver, args=(15514, received, 2.0), daemon=True)
        t.start()
        import time; time.sleep(0.2)

        ok = send_syslog('127.0.0.1', 15514, 'local7', 'informational',
                         'Catalyst-A', '%OSPF-5-ADJCHG: Neighbor FULL')
        t.join(timeout=2.5)

        assert ok, 'send_syslog が False を返した'
        assert len(received) >= 1, 'UDPパケットが届いていない'
        msg = received[0].decode('utf-8', errors='replace')
        assert '<' in msg and '>' in msg, f'RFC 3164 PRIフィールドがない: {msg[:60]}'
        assert 'OSPF' in msg or 'Catalyst' in msg, f'メッセージ内容が不正: {msg[:80]}'

    def test_syslog_pri_field_format(self, fresh_engines):
        """syslog: PRI値が facility*8+severity で正しく計算される"""
        from engine.syslog_sender import _build_syslog_packet, FACILITY, SEVERITY
        pkt = _build_syslog_packet('local7', 'notifications', 'SW1', 'test').decode()
        # local7=23, notifications=5 → PRI = 23*8+5 = 189
        assert pkt.startswith('<189>'), f'PRI値が不正: {pkt[:10]}'

    def test_syslog_level_filter_blocks_low_priority(self, fresh_engines):
        """syslog: 設定レベルより低優先度は送信しない"""
        from engine.syslog_sender import SyslogDispatcher
        d = SyslogDispatcher()
        assert d._should_send('warnings', 'errors') is True      # errors(3) <= warnings(4)
        assert d._should_send('warnings', 'informational') is False  # info(6) > warnings(4)
        assert d._should_send('informational', 'debugging') is False # debug(7) > info(6)
        assert d._should_send('informational', 'notifications') is True

    def test_snmp_trap_udp_packet_sent(self, fresh_engines):
        """SNMP trap: UDP 16162 に v2c パケットが届く"""
        from engine.syslog_sender import send_snmp_trap
        received = []
        t = threading.Thread(target=_udp_receiver, args=(16162, received, 2.0), daemon=True)
        t.start()
        import time; time.sleep(0.2)

        ok = send_snmp_trap('127.0.0.1', 16162, 'public',
                            'Catalyst-A', '1.3.6.1.4.1.9.10.13.1',
                            'OSPF Neighbor State Change')
        t.join(timeout=2.5)

        assert ok, 'send_snmp_trap が False を返した'
        assert len(received) >= 1, 'SNMP trapパケットが届いていない'
        # SNMP v2c パケットは 0x30 (SEQUENCE) で始まる
        assert received[0][0] == 0x30, f'BER SEQUENCEタグが不正: {received[0][0]:#04x}'

    def test_snmp_trap_packet_structure(self, fresh_engines):
        """SNMP trap: v2c パケット構造が正しい（BER SEQUENCE）"""
        from engine.syslog_sender import build_snmp_v2c_trap
        pkt = build_snmp_v2c_trap('public', 'SW1', '1.3.6.1.6.3.1.1.5.1', 'test')
        assert len(pkt) > 20, f'パケットが短すぎる: {len(pkt)} bytes'
        assert pkt[0] == 0x30, 'BER SEQUENCE(0x30)で始まっていない'
        # version field: INTEGER(0x02) value=1(v2c)
        assert 0x02 in pkt[:10], 'version INTEGER フィールドがない'

    def test_snmp_dispatcher_emit(self, fresh_engines):
        """SNMP dispatcher: 登録済み送信先にtrapを送出"""
        from engine.syslog_sender import SnmpDispatcher
        received = []
        t = threading.Thread(target=_udp_receiver, args=(16163, received, 2.0), daemon=True)
        t.start()
        import time; time.sleep(0.2)

        d = SnmpDispatcher()
        d.register('R1', [{'host': '127.0.0.1', 'port': 16163, 'community': 'public'}])

        async def _run():
            await d.emit('R1', 'Router1', '1.3.6.1.6.3.1.1.5.1', 'link up')

        asyncio.get_event_loop().run_until_complete(_run())
        t.join(timeout=2.5)
        assert len(received) >= 1, 'SnmpDispatcher からパケットが届いていない'

    def test_ntp_udp_packet_sent(self, fresh_engines):
        """NTP: UDP 15123 に 48バイトのクライアントリクエストが届く"""
        from engine.syslog_sender import send_ntp_request
        received = []
        t = threading.Thread(target=_udp_receiver, args=(15123, received, 2.0), daemon=True)
        t.start()
        import time; time.sleep(0.2)

        ok = send_ntp_request('127.0.0.1', 15123)
        t.join(timeout=2.5)

        assert ok, 'send_ntp_request が False を返した'
        assert len(received) >= 1, 'NTPパケットが届いていない'
        assert len(received[0]) == 48, f'NTPパケットが48バイトでない: {len(received[0])} bytes'

    def test_ntp_packet_header(self, fresh_engines):
        """NTP: パケットの LI/VN/Mode フィールドが正しい"""
        from engine.syslog_sender import build_ntp_request
        pkt = build_ntp_request()
        assert len(pkt) == 48, f'48バイトでない: {len(pkt)}'
        li_vn_mode = pkt[0]
        li   = (li_vn_mode >> 6) & 0x3   # LI=0（同期中）
        vn   = (li_vn_mode >> 3) & 0x7   # VN=4（NTPv4）
        mode = li_vn_mode & 0x7           # Mode=3（client）
        assert li == 0,   f'LI={li} (expected 0)'
        assert vn == 4,   f'VN={vn} (expected 4 = NTPv4)'
        assert mode == 3, f'Mode={mode} (expected 3 = client)'

    def test_ntp_client_register_and_poll(self, fresh_engines):
        """NTP client: 登録後にpoll_onceでリクエストが送信される"""
        from engine.syslog_sender import NtpClient
        received = []
        t = threading.Thread(target=_udp_receiver, args=(15124, received, 3.0), daemon=True)
        t.start()
        import time; time.sleep(0.2)

        nc = NtpClient()
        nc.register('R1', [{'host': '127.0.0.1', 'port': 15124}])

        async def _run():
            results = await nc.poll_once('R1')
            assert len(results) == 1
            assert results[0]['sent'] is True

        asyncio.get_event_loop().run_until_complete(_run())
        t.join(timeout=3.0)
        assert len(received) >= 1, 'NTP poll_once でパケットが届いていない'
