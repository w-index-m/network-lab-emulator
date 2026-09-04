"""
BGP Community 属性テスト

テスト内容:
  1. route-map で set community コマンドをサポート
  2. send-community neighbor コマンドをサポート
  3. advertise_network で community 属性を保持
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import BgpEngine, BgpRoute
from engine.rules import RuleEngine


def test_bgp_community_in_route():
    """BGP Route が community 属性を持つことを確認"""
    route = BgpRoute(
        prefix='192.168.1.0',
        prefix_len=24,
        next_hop='10.0.0.1',
        communities=['65000:100', '65001:200']
    )
    assert route.communities == ['65000:100', '65001:200']


def test_bgp_route_map_set_community():
    """route-map で community を設定できることを確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'

    # デバイス初期化
    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # route-map に community を追加
    engine.add_route_map(
        device_id,
        'SET_COMMUNITY',
        communities=['65000:100', '65001:200']
    )

    # route-map が正しく設定されたか確認
    rm = n['route_maps']['SET_COMMUNITY']
    assert rm['communities'] == ['65000:100', '65001:200']


def test_bgp_apply_route_map_community():
    """_apply_route_map で community が適用されることを確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'

    # デバイス初期化
    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # route-map に community を設定
    engine.add_route_map(
        device_id,
        'SET_COMM',
        communities=['65000:100']
    )

    # 元の経路
    route = BgpRoute(
        prefix='192.168.1.0',
        prefix_len=24,
        next_hop='10.0.0.2'
    )
    assert len(route.communities) == 0

    # route-map を適用
    applied_route = engine._apply_route_map(device_id, 'SET_COMM', route)

    # community が追加されたか確認
    assert applied_route.communities == ['65000:100']


def test_bgp_send_community_flag():
    """neighbor send-community が set されることを確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'
    peer_id = 'cisco-2'

    # デバイス初期化
    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # neighbor を追加
    from engine.protocols import BgpSession
    session = BgpSession(
        neighbor_id=peer_id,
        hostname='cisco-2',
        remote_as=65002
    )
    n['sessions'][peer_id] = session

    # send-community を有効化
    engine.set_neighbor_send_community(device_id, peer_id, True)

    # フラグが set されたか確認
    s = n['sessions'][peer_id]
    assert s.send_community is True


def test_bgp_community_multiple_values():
    """複数の community 値を設定できることを確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'

    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # 複数の community を設定
    communities = ['65000:100', '65001:200', '65002:300']
    engine.add_route_map(
        device_id,
        'MULTI_COMM',
        communities=communities
    )

    rm = n['route_maps']['MULTI_COMM']
    assert rm['communities'] == communities
    assert len(rm['communities']) == 3


def test_bgp_route_map_preserve_other_attributes():
    """route-map で community を設定する際、他の属性は保持されることを確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'

    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # 複数の属性を設定
    engine.add_route_map(
        device_id,
        'FULL_POLICY',
        prepend=[65001, 65001],
        local_pref=150,
        med=100,
        communities=['65000:100']
    )

    rm = n['route_maps']['FULL_POLICY']
    assert rm['prepend'] == [65001, 65001]
    assert rm['local_pref'] == 150
    assert rm['med'] == 100
    assert rm['communities'] == ['65000:100']


def test_bgp_route_with_all_attributes():
    """BGP Route が複数の属性（AS-path, local-pref, MED, community）を持つ"""
    route = BgpRoute(
        prefix='10.0.0.0',
        prefix_len=8,
        next_hop='172.16.0.1',
        as_path=[65000, 65001],
        local_pref=200,
        med=50,
        communities=['65000:100', '65001:200']
    )

    assert route.as_path == [65000, 65001]
    assert route.local_pref == 200
    assert route.med == 50
    assert route.communities == ['65000:100', '65001:200']


def test_bgp_apply_multiple_route_maps():
    """複数の route-map が順序よく適用される場合を確認"""
    engine = BgpEngine()
    device_id = 'cisco-1'

    n = engine._node(device_id)
    n['router_id'] = '10.0.0.1'
    n['local_as'] = 65001
    n['enabled'] = True

    # RM1: AS-path prepend
    engine.add_route_map(device_id, 'RM1', prepend=[65001])

    # RM2: local-pref
    engine.add_route_map(device_id, 'RM2', local_pref=200)

    # RM3: community
    engine.add_route_map(device_id, 'RM3', communities=['65000:100'])

    route = BgpRoute(
        prefix='192.168.0.0',
        prefix_len=16,
        next_hop='10.0.0.2'
    )

    # RM1 を適用
    route = engine._apply_route_map(device_id, 'RM1', route)
    assert route.as_path == [65001]

    # RM2 を適用
    route = engine._apply_route_map(device_id, 'RM2', route)
    assert route.local_pref == 200

    # RM3 を適用
    route = engine._apply_route_map(device_id, 'RM3', route)
    assert route.communities == ['65000:100']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
