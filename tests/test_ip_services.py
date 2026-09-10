"""
IPアドレッシングサービス系のテスト

Catalyst 9300 IP Addressing Services Configuration Guide に載っている
機能のうち、このエミュレータで実装したものを検証する。

- 拡張オブジェクトトラッキング（track <n> ...）
- TCP MSS調整（ip tcp adjust-mss）
- IPv6 基本（ipv6 unicast-routing / ipv6 address）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import rib_engine, track_engine   # noqa: E402
from engine.rules import DeviceState, RuleEngine        # noqa: E402


def _sw(name='SW-TRACK'):
    e = RuleEngine()
    s = DeviceState('catalyst', name)
    s._device_id = name
    track_engine.objects.pop(name, None)
    rib_engine.nodes.pop(name, None)
    for c in ['configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', 'ip address 10.2.2.1 255.255.255.0',
              'no shutdown', 'exit', 'exit']:
        e.process(c, s)
    return e, s


def _cfg(e, s, *cmds):
    e.process('configure terminal', s)
    for c in cmds:
        e.process(c, s)
    e.process('exit', s)


# ── 拡張オブジェクトトラッキング ───────────────────────
def test_track_interface_follows_shutdown():
    """インタフェースをshutdownするとトラックオブジェクトがDownになる"""
    e, s = _sw('SW-T1')
    _cfg(e, s, 'track 10 interface GigabitEthernet1/0/1 line-protocol', 'exit')
    assert 'Line-protocol is Up' in e.process('show track 10', s)

    _cfg(e, s, 'interface GigabitEthernet1/0/1', 'shutdown', 'exit')
    assert 'Line-protocol is Down' in e.process('show track 10', s)


def test_track_ip_route_reachability():
    """経路がRIBに載るとreachabilityがUpになる"""
    e, s = _sw('SW-T2')
    _cfg(e, s, 'track 40 ip route 192.168.99.0/24 reachability', 'exit')
    assert 'Reachability is Down' in e.process('show track 40', s)

    rib_engine.add_static_route('SW-T2', 'SW-T2', '192.168.99.0', 24,
                                '10.2.2.9', 1)
    assert 'Reachability is Up' in e.process('show track 40', s)


def test_track_list_boolean_and():
    """boolean and は全メンバーがUpのときだけUp"""
    e, s = _sw('SW-T3')
    _cfg(e, s,
         'track 1 interface GigabitEthernet1/0/1 line-protocol', 'exit',
         'track 2 interface GigabitEthernet1/0/2 line-protocol', 'exit',
         'track 3 list boolean and', 'object 1', 'object 2', 'exit')
    assert 'Boolean and is Up' in e.process('show track 3', s)

    _cfg(e, s, 'interface GigabitEthernet1/0/1', 'shutdown', 'exit')
    assert 'Boolean and is Down' in e.process('show track 3', s)


def test_track_list_boolean_or():
    """boolean or はどれか1つUpならUp"""
    e, s = _sw('SW-T4')
    _cfg(e, s,
         'track 1 interface GigabitEthernet1/0/1 line-protocol', 'exit',
         'track 2 interface GigabitEthernet1/0/2 line-protocol', 'exit',
         'track 3 list boolean or', 'object 1', 'object 2', 'exit',
         'interface GigabitEthernet1/0/1', 'shutdown', 'exit')
    assert 'Boolean or is Up' in e.process('show track 3', s)


def test_no_track_removes_object():
    e, s = _sw('SW-T5')
    _cfg(e, s, 'track 10 interface GigabitEthernet1/0/1 line-protocol', 'exit')
    _cfg(e, s, 'no track 10')
    assert 'No tracking process' in e.process('show track', s)


def test_show_track_without_objects():
    e, s = _sw('SW-T6')
    assert '%No tracking process' in e.process('show track', s)


# ── TCP MSS調整 ────────────────────────────────────────
def test_ip_tcp_adjust_mss_is_stored_and_shown():
    e, s = _sw('SW-M1')
    _cfg(e, s, 'interface GigabitEthernet1/0/1', 'ip tcp adjust-mss 1360', 'exit')
    assert s.interfaces['GigabitEthernet1/0/1']['tcp_mss'] == 1360
    assert 'ip tcp adjust-mss 1360' in e.process('show running-config', s)


def test_ip_tcp_adjust_mss_range_is_validated():
    """実機同様 500-1460 の範囲外は拒否する"""
    e, s = _sw('SW-M2')
    e.process('configure terminal', s)
    e.process('interface GigabitEthernet1/0/1', s)
    out = e.process('ip tcp adjust-mss 99', s)
    assert '500' in out and '1460' in out
    assert 'tcp_mss' not in s.interfaces['GigabitEthernet1/0/1']


# ── IPv6 基本 ──────────────────────────────────────────
def test_ipv6_address_and_unicast_routing():
    e, s = _sw('SW-V6')
    _cfg(e, s, 'ipv6 unicast-routing',
         'interface GigabitEthernet1/0/1', 'ipv6 address 2001:db8::1/64', 'exit')
    assert getattr(s, 'ipv6_unicast_routing', False) is True
    info = s.interfaces['GigabitEthernet1/0/1']
    assert info['ipv6'] == '2001:db8::1'
    assert info['ipv6_prefix'] == 64
    assert 'ipv6 address 2001:db8::1/64' in e.process('show running-config', s)
