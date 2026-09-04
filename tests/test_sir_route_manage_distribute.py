"""
Si-R distribute-list相当テスト: "ip rip use route-manage <name> in|out" /
"ip ospf use route-manage <name> in|out"

これまで Si-R の route-manage は redistribute のフィルタにしか使えなかった。
Cisco の distribute-list 同様、RIP/OSPF が学習・広告する経路自体を
prefix-list で絞れることを確認する。
"""

import re
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import filter_engine

# app.py 内の正規表現と同一パターン（CLIディスパッチャ全体を起動せずに検証）
SIR_USE_RM_PATTERN = r'^ip\s+(rip|ospf)\s+use\s+route-manage\s+(\S+)\s+(in|out)'


def test_pattern_matches_rip_in():
    m = re.match(SIR_USE_RM_PATTERN, 'ip rip use route-manage RIPFILTER in', re.I)
    assert m
    assert m.group(1) == 'rip'
    assert m.group(2) == 'RIPFILTER'
    assert m.group(3) == 'in'


def test_pattern_matches_ospf_out():
    m = re.match(SIR_USE_RM_PATTERN, 'ip ospf use route-manage OSPFFILTER out', re.I)
    assert m
    assert m.group(1) == 'ospf'
    assert m.group(2) == 'OSPFFILTER'
    assert m.group(3) == 'out'


def test_pattern_does_not_match_plain_rip_use_on():
    """既存の 'ip rip use on'（RIP有効化）とは衝突しない"""
    m = re.match(SIR_USE_RM_PATTERN, 'ip rip use on', re.I)
    assert m is None


def test_rip_distribute_via_route_manage_filters_routes():
    """ip rip use route-manage <name> in が filter_engine 経由で
    RIP の distribute-list と同じ仕組みで機能することを確認"""
    device_id = 'sir-dl-1'
    filter_engine.add_prefix_list(device_id, 'RIPFILTER', 'permit', '10.0.0.0', 8, ge=8, le=32)
    filter_engine.set_distribute_list(device_id, 'rip', 'in', 'RIPFILTER')

    routes = [
        {'network': '10.1.1.0', 'prefix': '24'},
        {'network': '192.168.5.0', 'prefix': '24'},
    ]
    filtered = filter_engine.filter_routes(device_id, 'rip', 'in', routes)
    networks = {r['network'] for r in filtered}
    assert networks == {'10.1.1.0'}


def test_ospf_distribute_via_route_manage_filters_routes():
    """ip ospf use route-manage <name> out も同じ機構を共有することを確認"""
    device_id = 'sir-dl-2'
    filter_engine.add_prefix_list(device_id, 'OSPFFILTER', 'deny', '172.16.0.0', 12, ge=12, le=32)
    filter_engine.add_prefix_list(device_id, 'OSPFFILTER', 'permit', '0.0.0.0', 0, ge=0, le=32)
    filter_engine.set_distribute_list(device_id, 'ospf', 'out', 'OSPFFILTER')

    routes = [
        {'network': '172.16.5.0', 'prefix': '24'},
        {'network': '203.0.113.0', 'prefix': '24'},
    ]
    filtered = filter_engine.filter_routes(device_id, 'ospf', 'out', routes)
    networks = {r['network'] for r in filtered}
    assert networks == {'203.0.113.0'}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
