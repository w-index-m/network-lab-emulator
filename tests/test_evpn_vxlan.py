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
