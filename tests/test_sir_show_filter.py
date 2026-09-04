"""
Si-R show filter / show ip filter が実設定を反映するかのテスト。

これまで show filter / show ip filter は固定文字列 "no filter configured"
を返すだけで、route-manage で登録した prefix-list を全く反映していなかった
（レビューで発見したバグ）。
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.rules import RuleEngine, DeviceState
from engine.protocols import filter_engine


def _make_state(device_id, hostname='sir-test'):
    state = DeviceState(device_type='sir', hostname=hostname)
    state._device_id = device_id
    return state


def test_show_filter_empty_when_no_prefix_list():
    engine = RuleEngine()
    state = _make_state('sir-sf-empty')
    out = engine._sir_show_filter(state)
    assert 'no filter configured' in out


def test_show_filter_reflects_configured_route_manage():
    engine = RuleEngine()
    device_id = 'sir-sf-1'
    state = _make_state(device_id)

    filter_engine.add_prefix_list(device_id, 'MYFILTER', 'permit', '10.0.0.0', 8)
    filter_engine.set_distribute_list(device_id, 'rip', 'in', 'MYFILTER')

    out = engine._sir_show_filter(state)
    assert 'MYFILTER' in out
    assert '10.0.0.0/8' in out
    assert 'rip use route-manage in' in out


def test_show_filter_marks_unused_list():
    engine = RuleEngine()
    device_id = 'sir-sf-2'
    state = _make_state(device_id)

    filter_engine.add_prefix_list(device_id, 'UNUSED', 'deny', '192.168.0.0', 16)

    out = engine._sir_show_filter(state)
    assert 'UNUSED' in out
    assert 'not applied' in out


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
