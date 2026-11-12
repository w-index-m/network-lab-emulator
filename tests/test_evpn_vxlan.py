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
import re
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


class TestShowFeature:
    """`show feature`がnv overlay/vn-segment-vlan-basedを一切表示しない
    実バグの回帰テスト。この2つはstate.nx_featuresに正しく保持され、
    show running-configや/api/nexus/dashboardでは既に使われていたが、
    show feature自身の固定テーブルには含まれていなかった(実機の
    show featureは当然この2つを列挙する)。
    """

    def test_nv_overlay_and_vn_segment_shown_as_enabled(self):
        _dev('nx-feat-1')
        _run('nx-feat-1', [
            'configure terminal', 'feature nv overlay',
            'feature vn-segment-vlan-based', 'end',
        ])
        out = _cli('nx-feat-1', 'show feature')
        assert 'nv overlay' in out
        assert 'vn-segment-vlan-based' in out
        for line in out.splitlines():
            if line.startswith('nv overlay') or line.startswith('vn-segment-vlan-based'):
                # 列がくっついていない(regression防止: 列幅がfeature名の
                # 長さとちょうど同じだと、Instance列の"1"と直接くっついて
                # 出力されてしまう——vn-segment-vlan-basedがまさにこの
                # 長さでそれを踏んだ)
                assert re.match(r'^\S.*\s+1\s+enabled$', line), line

    def test_disabled_by_default_before_feature_command(self):
        _dev('nx-feat-2')
        out = _cli('nx-feat-2', 'show feature')
        for line in out.splitlines():
            if line.startswith('nv overlay') or line.startswith('vn-segment-vlan-based'):
                assert 'disabled' in line


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

    def test_bgp_router_id_command_is_reflected(self):
        """router bgp配下の"router-id X.X.X.X"がshow bgpに反映されること。

        state.bgpの初期値には表示用のもっともらしいダミー値
        （router_id="10.1.0.1"）が入っており、"router bgp <n>"は
        asnだけを上書きしてrouter_idを更新していなかったため、
        実際に"router-id"を打ってもダミー値のまま変わらなかった
        （EVPN対応でrouter-idを使うようになって顕在化した）。
        """
        _dev('nx-evpn-12')
        _run('nx-evpn-12', ['configure terminal', 'router bgp 65001',
                            'router-id 10.9.9.9', 'end'])
        out = _cli('nx-evpn-12', 'show bgp l2vpn evpn summary')
        # address-family未設定なので本来は"not configured"だが、
        # router_id自体の反映確認のため直接stateを見る形にはせず
        # まずaddress-familyありのケースで確認する
        _run('nx-evpn-12', ['configure terminal', 'router bgp 65001',
                            'address-family l2vpn evpn', 'end'])
        out = _cli('nx-evpn-12', 'show bgp l2vpn evpn summary')
        assert 'BGP router identifier 10.9.9.9' in out
        assert 'BGP router identifier 10.1.0.1' not in out


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

    def test_fabric_vni_inventory_includes_member_vni_only_configs(self):
        """実バグの回帰テスト: ファブリック全体のVNIインベントリ
        (`data['vnis']`/`fabric.vni_count`)が、NX-OSの明示的な
        `evpn`/`vni <n> l2`グローバルサブモード経由のVNIしか見ておらず、
        より一般的な`interface nve1`配下の`member vni <n>` +
        `advertise-all-vni`だけで構成したVNI(明示的な`evpn`サブモードを
        一切使わない、実運用でもよくある書き方)が、スイッチ単位の
        `member_vnis`には出るのにファブリック全体のインベントリには
        一切反映されなかった。"""
        # 他テストが使う10010等と衝突しない専用VNIを使う
        # (device_sessionsはテスト間で共有されるアプリ全体の状態なので、
        # 同じVNI番号を別テストの装置が明示的evpnサブモード経由で
        # 既に使っていると l2vpn_evpn の判定が汚染されるため)
        vni = 20020
        a, b = 'nx-evpn-12', 'nx-evpn-13'
        for dev in (a, b):
            _dev(dev)
            _run(dev, [
                'configure terminal', 'feature nv overlay',
                'feature vn-segment-vlan-based', 'feature bgp',
                'vlan 20', f'vn-segment {vni}', 'exit',
                'interface nve1', 'source-interface loopback0',
                'host-reachability protocol bgp',
                f'member vni {vni}', 'ingress-replication protocol bgp',
                'exit', 'exit',
                'router bgp 65000', 'address-family l2vpn evpn',
                'advertise-all-vni', 'end',
            ])
        r = client.get('/api/nexus/dashboard')
        data = r.json()
        sw_a = next(s for s in data['switches'] if s['device_id'] == a)
        assert {'vni': vni, 'interface': 'nve1', 'type': 'L2',
                'ingress_replication': True, 'mcast_group': None} in sw_a['member_vnis']
        # 修正前はここが見つからず落ちていた
        vni_entry = next((v for v in data['vnis'] if v['vni'] == vni), None)
        assert vni_entry is not None, data['vnis']
        assert a in vni_entry['switches']
        assert b in vni_entry['switches']
        # このVNIは明示的な"evpn"グローバルサブモードを一切使っていないので
        # l2vpn_evpn(明示的なEVPNインスタンス設定の有無)はFalseのまま
        assert vni_entry['l2vpn_evpn'] is False
        assert data['fabric']['vni_count'] >= 1


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
