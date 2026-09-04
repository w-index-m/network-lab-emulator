"""
tools/demo_monitoring_pipeline.py テスト
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.demo_monitoring_pipeline import _prefix_to_mask


@pytest.mark.parametrize('prefix,expected', [
    (24, '255.255.255.0'),
    (30, '255.255.255.252'),
    (16, '255.255.0.0'),
    (8, '255.0.0.0'),
    (32, '255.255.255.255'),
])
def test_prefix_to_mask(prefix, expected):
    assert _prefix_to_mask(prefix) == expected


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
