"""
OSPF マルチエリアテスト: Area 0 (バックボーン) + Area 1, 2 での
ABR 経由のエリア間経路学習を検証

【実装アーキテクチャ上の注意】
本エミュレータの隣接判定はノード単位の単一エリアID比較で行われる
（実機のような「インターフェース単位のエリア」ではない）。そのため:
- ABR は `add_network()` で複数エリアのネットワークを自分の
  Router LSA に stub リンクとして保持し、それを自エリア(プライマリ
  area_id)内の隣接ルータへ広告する。
- 別エリアを明示的に設定したルータ（プライマリ area_id が ABR と異なる）
  は ABR と Hello エリア不一致になり、隣接そのものが成立しない。

このテストは上記の実装挙動を正として検証する。

実行: pytest tests/test_ospf_multiarea.py -v
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
    proto.ospf_engine.nodes.clear()
    proto.ospf_engine.area_networks.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'ospf': proto.ospf_engine, 'noop': noop,
    }


def _link(e, a, b):
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


class TestAbrMultiAreaRegistration:
    """ABR: 複数エリアのネットワーク登録・ABRフラグ"""

    @pytest.mark.asyncio
    async def test_abr_flag_set_with_two_areas(self, fresh_engines):
        e = fresh_engines
        _link(e, 'ABR', 'R0')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        await e['ospf'].start('R0', 'R0', 1, ['10.0.0.0/24'], '0.0.0.0')

        await asyncio.sleep(3)

        assert e['ospf'].nodes['ABR']['abr'] is True, "ABRフラグが立っていない"
        assert len(e['ospf'].area_networks['ABR']) == 2, \
            "ABRが2エリアを認識していない"
        assert '0.0.0.0' in e['ospf'].area_networks['ABR']
        assert '0.0.0.1' in e['ospf'].area_networks['ABR']

        for rid in ['ABR', 'R0']:
            await e['ospf'].stop(rid)

    @pytest.mark.asyncio
    async def test_abr_flag_set_with_three_areas(self, fresh_engines):
        e = fresh_engines
        _link(e, 'ABR', 'BB')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        e['ospf'].add_network('ABR', '10.2.0.0/24', '0.0.0.2')
        await e['ospf'].start('BB', 'BB', 1, ['10.0.1.0/24'], '0.0.0.0')

        await asyncio.sleep(3)

        assert e['ospf'].nodes['ABR']['abr'] is True
        assert len(e['ospf'].area_networks['ABR']) == 3, \
            f"ABRが3エリアを認識していない: {e['ospf'].area_networks['ABR']}"

        for rid in ['ABR', 'BB']:
            await e['ospf'].stop(rid)


class TestBackboneLearnsOtherAreas:
    """バックボーン(Area 0)のルータが、ABR経由で他エリアのネットワークを学習"""

    @pytest.mark.asyncio
    async def test_backbone_router_learns_area1_network_via_abr(self, fresh_engines):
        """Area0のR0が、ABRが保有するArea1のネットワーク(10.1.0.0/24)を
        到達可能経路として学習する"""
        e = fresh_engines
        _link(e, 'ABR', 'R0')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        await e['ospf'].start('R0', 'R0', 1, ['10.0.0.0/24'], '0.0.0.0')

        await asyncio.sleep(5)

        r0_learned = {r['network'] for r in e['ospf'].nodes['R0']['routes']}
        assert '10.1.0.0' in r0_learned, \
            f"R0がABR経由でArea1のネットワークを学習していない: {r0_learned}"

        for rid in ['ABR', 'R0']:
            await e['ospf'].stop(rid)

    @pytest.mark.asyncio
    async def test_backbone_router_learns_multiple_other_areas(self, fresh_engines):
        """Area0のBBが、ABRが保有するArea1・Area2両方のネットワークを学習"""
        e = fresh_engines
        _link(e, 'ABR', 'BB')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        e['ospf'].add_network('ABR', '10.2.0.0/24', '0.0.0.2')
        await e['ospf'].start('BB', 'BB', 1, ['10.0.1.0/24'], '0.0.0.0')

        await asyncio.sleep(5)

        bb_learned = {r['network'] for r in e['ospf'].nodes['BB']['routes']}
        assert '10.1.0.0' in bb_learned, \
            f"BBがArea1のネットワークを学習していない: {bb_learned}"
        assert '10.2.0.0' in bb_learned, \
            f"BBがArea2のネットワークを学習していない: {bb_learned}"

        for rid in ['ABR', 'BB']:
            await e['ospf'].stop(rid)

    @pytest.mark.asyncio
    async def test_multiple_backbone_routers_all_learn_other_areas(self, fresh_engines):
        """複数のバックボーンルータ全員がABR経由で他エリアを学習"""
        e = fresh_engines
        _link(e, 'ABR', 'BB1')
        _link(e, 'ABR', 'BB2')
        _link(e, 'ABR', 'BB3')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')

        for bb in ['BB1', 'BB2', 'BB3']:
            await e['ospf'].start(bb, bb, 1, [f'10.0.{ord(bb[-1])}.0/24'], '0.0.0.0')

        await asyncio.sleep(5)

        for bb in ['BB1', 'BB2', 'BB3']:
            learned = {r['network'] for r in e['ospf'].nodes[bb]['routes']}
            assert '10.1.0.0' in learned, \
                f"{bb}がArea1のネットワークを学習していない: {learned}"

        for rid in ['ABR', 'BB1', 'BB2', 'BB3']:
            await e['ospf'].stop(rid)


class TestDifferentAreaRouterNoDirectAdjacency:
    """既知のアーキテクチャ制約: プライマリエリアが異なるルータはABRと隣接しない

    本エミュレータはノード単位でエリアIDを1つだけ保持するため、
    「インターフェース単位のエリア分離」を持つ実機とは異なり、
    ABRと異なるプライマリエリアを持つルータは Hello のエリア不一致で
    隣接が確立しない。これは実装上の既知の制約であり、回帰検知のために
    明示的にテストする。
    """

    @pytest.mark.asyncio
    async def test_area1_primary_router_does_not_adjacency_with_area0_abr(self, fresh_engines):
        e = fresh_engines
        _link(e, 'ABR', 'R1')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        # R1はプライマリエリアとして 0.0.0.1 を明示的に設定
        await e['ospf'].start('R1', 'R1', 1, ['10.1.0.0/24'], '0.0.0.1')

        await asyncio.sleep(4)

        abr_neighbors = e['ospf'].nodes['ABR']['neighbors']
        r1_neighbors = e['ospf'].nodes['R1']['neighbors']
        assert 'R1' not in abr_neighbors, \
            "既知の制約に反してABR-R1が隣接している（アーキテクチャ変更の可能性）"
        assert len(r1_neighbors) == 0, \
            f"R1が誰とも隣接しないはずが隣接している: {r1_neighbors}"

        for rid in ['ABR', 'R1']:
            await e['ospf'].stop(rid)


class TestAreaMismatchSameArea:
    """同一エリア内では通常通り隣接が成立する（回帰確認）"""

    @pytest.mark.asyncio
    async def test_same_area_adjacency_still_works_in_multiarea_context(self, fresh_engines):
        e = fresh_engines
        _link(e, 'ABR', 'R0')

        await e['ospf'].start('ABR', 'ABR', 1, ['10.0.0.0/24'], '0.0.0.0')
        e['ospf'].add_network('ABR', '10.1.0.0/24', '0.0.0.1')
        await e['ospf'].start('R0', 'R0', 1, ['10.0.0.0/24'], '0.0.0.0')

        await asyncio.sleep(4)

        abr_neighbors = e['ospf'].nodes['ABR']['neighbors']
        assert 'R0' in abr_neighbors, "同一エリアの隣接が張れていない"
        assert abr_neighbors['R0'].state == 'Full', \
            f"R0がFullでない: {abr_neighbors['R0'].state}"

        for rid in ['ABR', 'R0']:
            await e['ospf'].stop(rid)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
