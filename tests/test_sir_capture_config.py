"""
_capture_sir_config: "?" によるヘルプ照会が running-config に
残らないことのテスト（レビューで発見したバグの修正確認）。
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app import _capture_sir_config
from engine.rules import DeviceState


def _make_state():
    state = DeviceState(device_type='sir', hostname='sir-test')
    state.sir_config = {}
    return state


def test_help_query_not_captured():
    state = _make_state()
    _capture_sir_config(state, 'ip route-manage ?')
    assert state.sir_config == {}


def test_help_query_with_leading_whitespace_not_captured():
    state = _make_state()
    _capture_sir_config(state, '  ip rip use ? ')
    assert state.sir_config == {}


def test_real_config_command_still_captured():
    state = _make_state()
    _capture_sir_config(state, 'ip route-manage RIPFILTER permit 10.0.0.0/8')
    assert len(state.sir_config) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
