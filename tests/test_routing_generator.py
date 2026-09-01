"""
tools/routing_generator.py テスト
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.routing_generator import _generate_networks


def test_generates_requested_count():
    nets = list(_generate_networks('10.50.0.0', 24, 10))
    assert len(nets) == 10


def test_networks_are_sequential_and_non_overlapping():
    nets = list(_generate_networks('10.50.0.0', 24, 5))
    addrs = [n for n, mask in nets]
    assert addrs == ['10.50.0.0', '10.50.1.0', '10.50.2.0', '10.50.3.0', '10.50.4.0']


def test_mask_matches_prefix():
    nets = list(_generate_networks('192.168.0.0', 24, 1))
    assert nets[0][1] == '255.255.255.0'


def test_prefix_30_step():
    nets = list(_generate_networks('172.16.0.0', 30, 4))
    addrs = [n for n, mask in nets]
    assert addrs == ['172.16.0.0', '172.16.0.4', '172.16.0.8', '172.16.0.12']
    assert nets[0][1] == '255.255.255.252'


def test_zero_count_yields_empty():
    assert list(_generate_networks('10.0.0.0', 24, 0)) == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
