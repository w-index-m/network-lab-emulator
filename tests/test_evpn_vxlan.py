"""
Cisco Nexus (NX-OS) の VXLAN EVPN（BGP EVPNをコントロールプレーンに
使うVXLAN）設定。

実機の代表的な構成:
    feature nv overlay
    feature vn-segment-vlan-based
    feature bgp

    vlan 10
     vn-segment 10010

    evpn
     vni 10010 l2
      rd auto
      route-target both auto

    interface nve1
     source-interface loopback0
     member vni 10010
      ingress-replication protocol bgp

    router bgp 65001
     address-family l2vpn evpn
      neighbor 10.0.0.2 activate
      advertise-all-vni

これらのコマンドが受理され、show running-config / show nve peers /
show nve vni / show bgp l2vpn evpn に反映されることを確認する。

なお、このエミュレータには実際のBGP EVPN NLRI交換エンジンは無く、
今回追加したのは設定の受理・保持・表示レベル（PPPoEサーバ実装と
同じ深さ）である点に注意。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='nexus'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


EVPN_SETUP = [
    'configure terminal',
    'feature nv overlay',
    'feature vn-segment-vlan-based',
    'feature bgp',
    'vlan 10',
    'vn-segment 10010',
    'exit',
    'evpn',
    'vni 10010 l2',
    'rd auto',
    'route-target both auto',
    'exit',
    'exit',
    'interface nve1',
    'source-interface loopback0',
    'member vni 10010',
    'ingress-replication protocol bgp',
    'exit',
    'exit',
    'router bgp 65001',
    'address-family l2vpn evpn',
    'neighbor 10.0.0.2 activate',
    'advertise-all-vni',
    'end',
]


class TestEvpnVniMode:
    def test_evpn_vni_l2_stores_rd_and_route_target(self):
        _dev('nx-evpn-1')
        _run('nx-evpn-1', EVPN_SETUP)
        out = _cli('nx-evpn-1', 'show running-config')
        assert 'evpn' in out
        assert 'vni 10010 l2' in out
        assert 'rd auto' in out
        assert 'route-target both auto' in out

    def test_vlan_vn_segment_reflected_in_running_config(self):
        _dev('nx-evpn-2')
        _run('nx-evpn-2', EVPN_SETUP)
        out = _cli('nx-evpn-2', 'show running-config')
        assert 'vn-segment 10010' in out


class TestNveInterface:
    def test_nve_interface_auto_created(self):
        _dev('nx-evpn-3')
        _run('nx-evpn-3', EVPN_SETUP)
        out = _cli('nx-evpn-3', 'show ip interface brief')
        assert 'nve1' in out

    def test_show_nve_vni_lists_member(self):
        _dev('nx-evpn-4')
        _run('nx-evpn-4', EVPN_SETUP)
        out = _cli('nx-evpn-4', 'show nve vni')
        assert 'nve1' in out
        assert '10010' in out

    def test_show_nve_peers_lists_activated_bgp_neighbor(self):
        _dev('nx-evpn-5')
        _run('nx-evpn-5', EVPN_SETUP)
        out = _cli('nx-evpn-5', 'show nve peers')
        assert '10.0.0.2' in out


class TestBgpL2vpnEvpnAddressFamily:
    def test_show_bgp_l2vpn_evpn_lists_activated_neighbor(self):
        _dev('nx-evpn-6')
        _run('nx-evpn-6', EVPN_SETUP)
        out = _cli('nx-evpn-6', 'show bgp l2vpn evpn summary')
        assert '10.0.0.2' in out
        assert 'advertise-all-vni: enabled' in out

    def test_running_config_reflects_address_family(self):
        _dev('nx-evpn-7')
        _run('nx-evpn-7', EVPN_SETUP)
        out = _cli('nx-evpn-7', 'show running-config')
        assert 'address-family l2vpn evpn' in out
        assert 'neighbor 10.0.0.2 activate' in out
        assert 'advertise-all-vni' in out


class TestNotConfigured:
    def test_show_bgp_l2vpn_evpn_without_config(self):
        _dev('nx-evpn-8')
        _run('nx-evpn-8', ['configure terminal', 'router bgp 65001', 'end'])
        out = _cli('nx-evpn-8', 'show bgp l2vpn evpn summary')
        assert 'not configured' in out

    def test_show_nve_peers_without_config(self):
        _dev('nx-evpn-9')
        out = _cli('nx-evpn-9', 'show nve peers')
        assert 'NVEピアなし' in out


class TestNexusDashboardApi:
    """Nexus Dashboard風ファブリックビュー（/api/nexus/dashboard）"""

    def test_vtep_and_vxlan_ready_reflect_full_config(self):
        _dev('nx-evpn-10')
        _run('nx-evpn-10', EVPN_SETUP)
        r = client.get('/api/nexus/dashboard')
        assert r.status_code == 200
        data = r.json()
        sw = next(s for s in data['switches'] if s['device_id'] == 'nx-evpn-10')
        assert sw['role'] == 'VTEP'
        assert sw['overlay_enabled'] is True
        assert sw['vxlan_ready'] is True
        assert sw['evpn_address_family'] is True
        assert sw['advertise_all_vni'] is True
        assert {'vlan': 10, 'vni': 10010} in sw['vlan_vni_map']
        assert any(p['peer_ip'] == '10.0.0.2' for p in sw['nve_peers'])
        vni_entry = next(v for v in data['vnis'] if v['vni'] == 10010)
        assert 'nx-evpn-10' in vni_entry['switches']

    def test_non_nexus_device_excluded(self):
        _dev('evpn-cisco-1', type_='cisco')
        r = client.get('/api/nexus/dashboard')
        ids = [s['device_id'] for s in r.json()['switches']]
        assert 'evpn-cisco-1' not in ids

    def test_partial_config_is_not_vxlan_ready(self):
        _dev('nx-evpn-11')
        _run('nx-evpn-11', ['configure terminal', 'feature nv overlay', 'end'])
        r = client.get('/api/nexus/dashboard')
        sw = next(s for s in r.json()['switches'] if s['device_id'] == 'nx-evpn-11')
        assert sw['overlay_enabled'] is True
        assert sw['vxlan_ready'] is False


# ══════════════════════════════════════════════════════════
# Catalyst 9000 (IOS-XE) のスタンドアロンVXLAN EVPN
#
# NX-OSとは文法が違う:
#   - "feature nv overlay"/"evpn"ではなく "l2vpn evpn" が起点
#   - VLAN⇔VNIは "vlan <n>" 配下の "vn-segment" ではなく
#     "vlan configuration <n>" 配下の "member evpn-instance <n> vni <n>"
#   - "ingress-replication" に "protocol bgp" が付かない
#     （"host-reachability protocol bgp" の方で既に決まっているため）
# 装置の内部状態(state.nve/evpn_vnis/vlan_vn_segment)はNX-OSと共通の
# データモデルに正規化して持たせているので、show running-config /
# /api/nexus/dashboard はNexusと同じ形で確認できる。
# ══════════════════════════════════════════════════════════
C9K_EVPN_SETUP = [
    'configure terminal',
    'interface loopback0',
    'ip address 10.0.0.1 255.255.255.255',
    'exit',
    'l2vpn evpn',
    'replication-type ingress',
    'router-id loopback0',
    'instance 10010 vlan-based',
    'encapsulation vxlan',
    'exit',
    'exit',
    'vlan configuration 10',
    'member evpn-instance 10010 vni 10010',
    'exit',
    'interface nve1',
    'no shutdown',
    'source-interface loopback0',
    'host-reachability protocol bgp',
    'member vni 10010',
    'ingress-replication',
    'exit',
    'exit',
    'router bgp 65001',
    'address-family l2vpn evpn',
    'neighbor 10.0.0.2 activate',
    'end',
]


class TestCatalyst9000Evpn:
    def test_l2vpn_evpn_instance_stored_and_shown(self):
        _dev('c9k-evpn-1', type_='catalyst')
        _run('c9k-evpn-1', C9K_EVPN_SETUP)
        out = _cli('c9k-evpn-1', 'show running-config')
        assert 'l2vpn evpn' in out
        assert 'replication-type ingress' in out
        assert 'instance 10010 vlan-based' in out
        assert 'encapsulation vxlan' in out

    def test_vlan_configuration_maps_vlan_to_vni(self):
        _dev('c9k-evpn-2', type_='catalyst')
        _run('c9k-evpn-2', C9K_EVPN_SETUP)
        out = _cli('c9k-evpn-2', 'show running-config')
        assert 'vlan configuration 10' in out
        assert 'member evpn-instance 10010 vni 10010' in out

    def test_nve_interface_uses_ios_xe_ingress_replication_syntax(self):
        """IOS-XEは"ingress-replication"だけで"protocol bgp"を付けない。"""
        _dev('c9k-evpn-3', type_='catalyst')
        _run('c9k-evpn-3', C9K_EVPN_SETUP)
        out = _cli('c9k-evpn-3', 'show running-config')
        assert 'host-reachability protocol bgp' in out
        assert 'member vni 10010' in out
        assert '  ingress-replication' in out
        assert 'ingress-replication protocol bgp' not in out

    def test_show_nve_vni_and_peers_work_same_as_nexus(self):
        _dev('c9k-evpn-4', type_='catalyst')
        _run('c9k-evpn-4', C9K_EVPN_SETUP)
        assert '10010' in _cli('c9k-evpn-4', 'show nve vni')
        assert '10.0.0.2' in _cli('c9k-evpn-4', 'show nve peers')

    def test_address_family_reflected_under_router_bgp(self):
        _dev('c9k-evpn-5', type_='catalyst')
        _run('c9k-evpn-5', C9K_EVPN_SETUP)
        out = _cli('c9k-evpn-5', 'show running-config')
        assert 'address-family l2vpn evpn' in out
        assert 'neighbor 10.0.0.2 activate' in out

    def test_dashboard_api_reports_catalyst_as_vtep(self):
        _dev('c9k-evpn-6', type_='catalyst')
        _run('c9k-evpn-6', C9K_EVPN_SETUP)
        r = client.get('/api/nexus/dashboard')
        sw = next(s for s in r.json()['switches'] if s['device_id'] == 'c9k-evpn-6')
        assert sw['role'] == 'VTEP'
        assert sw['overlay_enabled'] is True
        assert sw['vxlan_ready'] is True
        assert sw['features'] == ['l2vpn evpn']
        assert {'vlan': 10, 'vni': 10010} in sw['vlan_vni_map']
        assert any(p['peer_ip'] == '10.0.0.2' for p in sw['nve_peers'])

    def test_evpn_commands_are_rejected_on_generic_cisco(self):
        """l2vpn evpn / vlan configuration はNexusとCatalystだけの構文。
        汎用'cisco'デバイスタイプでは受理されず、configモードのまま
        （config-l2vpn-evpn等の専用サブモードに入らない）。"""
        _dev('generic-cisco-evpn', type_='cisco')
        _cli('generic-cisco-evpn', 'configure terminal')
        r = client.post('/api/cli', json={'device_id': 'generic-cisco-evpn',
                                          'command': 'l2vpn evpn'})
        assert r.json()['mode'] != 'config-l2vpn-evpn'

    def test_nexus_and_catalyst_evpn_syntax_do_not_cross_contaminate(self):
        """NexusのCLIコマンド('evpn'グローバルモード)はCatalystでは
        使えず、逆にCatalystの'l2vpn evpn'はNexusでは使えないこと
        （専用サブモードに入らないことで確認する）。"""
        _dev('nx-strict', type_='nexus')
        _cli('nx-strict', 'configure terminal')
        r = client.post('/api/cli', json={'device_id': 'nx-strict',
                                          'command': 'l2vpn evpn'})
        assert r.json()['mode'] != 'config-l2vpn-evpn'

        _dev('c9k-strict', type_='catalyst')
        _cli('c9k-strict', 'configure terminal')
        r = client.post('/api/cli', json={'device_id': 'c9k-strict',
                                          'command': 'evpn'})
        assert r.json()['mode'] != 'config-evpn'
