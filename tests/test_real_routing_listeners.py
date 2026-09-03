"""
engine/real_rip_agent.py・engine/real_bgp_agent.py のパケットパース
ロジックのテスト（実ソケットは使わず、純粋な関数の入出力のみ検証）
"""

import os
import socket
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.real_rip_agent import parse_rip_packet
from engine.real_bgp_agent import _parse_update
from tools.route_injector_cli import bgp_build_update as _build_update
from tools import route_injector_cli as injector


def _build_rip_response(entries):
    header = struct.pack('!BBH', 2, 2, 0)  # command=Response, version=2
    body = b''
    for net, prefix, metric in entries:
        mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix else 0
        body += struct.pack('!HH4s4s4sI', 2, 0, socket.inet_aton(net),
                             struct.pack('!I', mask_int), b'\x00\x00\x00\x00', metric)
    return header + body


def test_parse_rip_packet_single_entry():
    pkt = _build_rip_response([('172.16.50.0', 24, 1)])
    parsed = parse_rip_packet(pkt)
    assert parsed['command'] == 2
    assert parsed['version'] == 2
    assert len(parsed['entries']) == 1
    assert parsed['entries'][0] == {'network': '172.16.50.0', 'prefix': 24, 'metric': 1}


def test_parse_rip_packet_multiple_entries():
    pkt = _build_rip_response([
        ('10.0.0.0', 8, 2),
        ('192.168.1.0', 24, 3),
    ])
    parsed = parse_rip_packet(pkt)
    assert len(parsed['entries']) == 2
    assert parsed['entries'][1]['network'] == '192.168.1.0'
    assert parsed['entries'][1]['prefix'] == 24


def test_parse_rip_packet_too_short_returns_none():
    assert parse_rip_packet(b'\x02') is None


@pytest.mark.parametrize('network,expected_prefix', [
    ('10.1.0.0', 8),       # クラスA
    ('172.16.80.0', 16),   # クラスB
    ('192.168.5.0', 24),   # クラスC
])
def test_ripv1_derives_classful_prefix(network, expected_prefix):
    """RIPv1はマスク欄を持たない（常に0）ため、受信側がアドレスクラスから
    プレフィックス長を導出しなければならない（RFC 1058）。
    ここで0のままにすると /0 の経路として学習され、デフォルトルート
    同然の誤った経路が入ってしまう。
    """
    routes = [{'network': network, 'netmask': '255.255.255.0',
               'nexthop': '0.0.0.0', 'metric': 1, 'tag': 0}]
    pkt = injector.rip_build_packet(routes, 1, command=2)  # version=1
    parsed = parse_rip_packet(pkt)
    assert parsed['version'] == 1
    assert parsed['entries'][0]['network'] == network
    assert parsed['entries'][0]['prefix'] == expected_prefix


def test_ripv2_uses_mask_field_not_class():
    """RIPv2はマスク欄を持つので、クラスに関係なくその値を使う"""
    routes = [{'network': '172.16.80.0', 'netmask': '255.255.255.0',
               'nexthop': '0.0.0.0', 'metric': 1, 'tag': 0}]
    parsed = parse_rip_packet(injector.rip_build_packet(routes, 2, command=2))
    # クラスBだが、マスク欄の /24 が優先される
    assert parsed['entries'][0]['prefix'] == 24


def test_bgp_update_roundtrip():
    pkt = _build_update(
        routes=[('172.16.60.0', 24)],
        next_hop='10.9.9.100',
        as_path=[65001, 65002],
        is_ibgp=False,
        med=5,
    )
    # BGPヘッダ(19バイト)を除いたBODYを渡す
    body = pkt[19:]
    parsed = _parse_update(body)
    assert parsed is not None
    assert parsed['prefixes'] == [('172.16.60.0', 24)]
    assert parsed['next_hop'] == '10.9.9.100'
    assert parsed['as_path'] == [65001, 65002]
    assert parsed['med'] == 5


def test_bgp_update_no_routes_returns_empty_prefixes():
    pkt = _build_update(routes=[], next_hop='10.9.9.1', as_path=[], is_ibgp=False)
    body = pkt[19:]
    parsed = _parse_update(body)
    assert parsed is not None
    assert parsed['prefixes'] == []
