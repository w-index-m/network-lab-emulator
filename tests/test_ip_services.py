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


# ── GLBP ───────────────────────────────────────────────
def _glbp_pair(prefix='GL'):
    """リンク済みの2台を作ってGLBPを同一グループで設定する"""
    import app
    from engine.protocols import glbp_engine, vnet
    A, B = f'{prefix}-a', f'{prefix}-b'
    for d in (A, B):
        app.device_sessions[d] = DeviceState('catalyst', d)
        app.device_sessions[d]._device_id = d
        glbp_engine.groups.pop(d, None)
    vnet.add_link(A, B, 'GigabitEthernet1/0/1', 'GigabitEthernet1/0/1')
    e = RuleEngine()
    for d, ip, pri in ((A, '10.63.0.1', 150), (B, '10.63.0.2', 100)):
        s = app.device_sessions[d]
        for c in ['configure terminal', 'interface GigabitEthernet1/0/1',
                  'no switchport', f'ip address {ip} 255.255.255.0',
                  'no shutdown', 'glbp 1 ip 10.63.0.254',
                  f'glbp 1 priority {pri}', 'glbp 1 preempt', 'exit', 'exit']:
            e.process(c, s)
    return e, A, B


def test_glbp_higher_priority_becomes_avg():
    """priorityが高い方がAVG(Active)、低い方がStandbyになる"""
    import app
    e, A, B = _glbp_pair('GL1')
    out_a = e.process('show glbp', app.device_sessions[A])
    out_b = e.process('show glbp', app.device_sessions[B])
    assert 'State is Active' in out_a, out_a
    assert 'State is Standby' in out_b, out_b


def test_glbp_assigns_virtual_forwarders_with_glbp_mac():
    """各メンバーにAVF(仮想フォワーダ)と 0007.b400.xxyy の仮想MACが割り当たる"""
    import app
    e, A, B = _glbp_pair('GL2')
    out = e.process('show glbp', app.device_sessions[A])
    assert '0007.b400.0101' in out, out
    assert '0007.b400.0102' in out, out


def test_glbp_brief_uses_short_interface_names():
    """show glbp brief は列幅の都合で実機同様に短縮表記を使う"""
    import app
    e, A, _B = _glbp_pair('GL3')
    out = e.process('show glbp brief', app.device_sessions[A])
    assert 'Gi1/0/1' in out
    assert 'GigabitEthernet1/0/1' not in out


def test_glbp_priority_range_is_validated():
    e, s = _sw('SW-GLBP')
    e.process('configure terminal', s)
    e.process('interface GigabitEthernet1/0/1', s)
    out = e.process('glbp 1 priority 999', s)
    assert '1' in out and '255' in out


# ── NHRP / WCCP ────────────────────────────────────────
def test_nhrp_map_is_shown():
    e, s = _sw('SW-NHRP')
    _cfg(e, s, 'interface Tunnel0', 'ip nhrp network-id 100',
         'ip nhrp nhs 10.9.9.1', 'ip nhrp map 10.9.9.1 203.0.113.9', 'exit')
    out = e.process('show ip nhrp', s)
    assert '10.9.9.1' in out
    assert '203.0.113.9' in out


def test_wccp_service_is_shown():
    e, s = _sw('SW-WCCP')
    _cfg(e, s, 'ip wccp 61 group-address 239.1.1.1',
         'interface GigabitEthernet1/0/1', 'ip wccp 61 redirect in', 'exit')
    out = e.process('show ip wccp', s)
    assert 'Service Identifier: 61' in out
    assert '239.1.1.1' in out
    assert s.interfaces['GigabitEthernet1/0/1']['wccp_redirect']['in'] == '61'
