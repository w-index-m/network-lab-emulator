"""
OSPF distribute-list テスト

テスト内容:
  1. distribute-list <prefix-list> in が OSPF の SPF計算結果（n['routes']）に適用される
  2. prefix-list に一致しない経路は RIB から除外される
  3. distribute-list が設定されていなければフィルタなし（全経路保持）
"""

import asyncio
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import OspfEngine, filter_engine


def _make_lsdb_routes():
    """SPF計算後を模した経路リスト（_recalc_routes の best 相当）"""
    return [
        {'network': '10.0.1.0', 'prefix': '24', 'metric': 10,
         'via': 'r2', 'next_hop': '10.0.1.2', 'type': 'O', 'area': '0.0.0.0'},
        {'network': '10.0.2.0', 'prefix': '24', 'metric': 20,
         'via': 'r3', 'next_hop': '10.0.2.2', 'type': 'O', 'area': '0.0.0.0'},
        {'network': '192.168.1.0', 'prefix': '24', 'metric': 10,
         'via': 'r4', 'next_hop': '192.168.1.2', 'type': 'O', 'area': '0.0.0.0'},
    ]


def test_ospf_distribute_list_in_filters_routes():
    """distribute-list in で prefix-list に不一致な経路が除外される"""
    device_id = 'cisco-1'

    # prefix-list: 10.0.0.0/8 配下のみ許可
    filter_engine.add_prefix_list(device_id, 'OSPF_IN', 'permit', '10.0.0.0', 8, ge=8, le=32)
    filter_engine.set_distribute_list(device_id, 'ospf', 'in', 'OSPF_IN')

    routes = _make_lsdb_routes()
    filtered = filter_engine.filter_routes(device_id, 'ospf', 'in', routes)

    networks = {r['network'] for r in filtered}
    assert '10.0.1.0' in networks
    assert '10.0.2.0' in networks
    assert '192.168.1.0' not in networks


def test_ospf_distribute_list_not_configured_keeps_all_routes():
    """distribute-list が設定されていない場合は全経路が保持される"""
    device_id = 'cisco-2'
    routes = _make_lsdb_routes()
    filtered = filter_engine.filter_routes(device_id, 'ospf', 'in', routes)
    assert len(filtered) == len(routes)


def test_ospf_engine_recalc_routes_applies_distribute_list():
    """OspfEngine._recalc_routes が distribute-list in を適用して n['routes'] を絞り込む"""
    engine = OspfEngine()
    device_id = 'cisco-3'

    n = engine._node(device_id)
    n['enabled'] = True
    n['router_id'] = '1.1.1.1'

    # prefix-list: 172.16.0.0/12 のみ許可
    filter_engine.add_prefix_list(device_id, 'OSPF_FILTER', 'permit', '172.16.0.0', 12, ge=12, le=32)
    filter_engine.set_distribute_list(device_id, 'ospf', 'in', 'OSPF_FILTER')

    from engine.protocols import OspfLsa
    n['lsdb']['1.1.1.1:1'] = OspfLsa(
        ls_type=1, ls_id='1.1.1.1', adv_router='1.1.1.1',
        seq_num=1, checksum=0, age=0,
        links=[
            {'type': 'stub', 'network': '172.16.5.0/24', 'link_id': '172.16.5.0', 'metric': 10},
            {'type': 'stub', 'network': '10.99.0.0/24', 'link_id': '10.99.0.0', 'metric': 10},
        ]
    )

    asyncio.run(_recalc_and_settle(engine, device_id))

    networks = {r['network'] for r in n['routes']}
    assert '172.16.5.0' in networks
    assert '10.99.0.0' not in networks


async def _recalc_and_settle(engine, device_id):
    engine._recalc_routes(device_id)
    await asyncio.sleep(0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
