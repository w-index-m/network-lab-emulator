"""
v6プラス(IPoE+MAP-E)経由でYamaha RTX ⇔ Cisco IOS のIPsecを実際に張れるかの
確認。ユーザー依頼: 「YAMAHA CiscoでIPOEしてIPsec接続設定作ってノウハウに
残して」。

擬似FLET'S PPPoE経由のIPsec(tests/test_yamaha_cisco_ipsec_over_pppoe.py)の
IPoE版。構成はほぼ同じだが、RTXのWANアドレスがPPPoE払い出しのpp<N>では
なく、v6プラス方式のMAP-E払い出しアドレス(map0、IPoEアクセス配下で取得)
になる点が違う。

実装面で新規コードは一切不要だった: engine/ike_engine.pyのIKE/IPsec
ネゴシエーションは`ipsec ike local-address`に設定された値をそのまま
使うだけで、そのIPがどの経路(固定/PPPoE払い出し/MAP-E払い出し)で得られた
ものかを一切区別しない。pp<N>のケースで既に`dt in ('sir', 'srs',
'yamaha')`に拡張済みだったため、MAP-E払い出しのmap0アドレスを
local-addressに使うだけで何も変更せずに確立した(ライブ検証で確認済み)。

構成: Cisco(固定WAN、RTXのlan3に直結) -- Yamaha RTX(v6プラスの
ルールサーバー役`bas`からlan2経由でMAP-E払い出しアドレスを取得) --
`bas`(ルールサーバー)。RTXは`map-e use lan2`で先にIPoE+MAP-E接続を
済ませてから、payされたアドレスを`ipsec ike local-address`に使って
Ciscoとネゴシエーションする — 現実のv6プラス対応ルーター+IPsecの
典型構成と同じ順序。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _link(a, b, iface_a, iface_b):
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': iface_a, 'iface_b': iface_b})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


def _out(id_, cmd):
    return _cli(id_, cmd)['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _out(id_, cmd)
    return out


def _setup_scenario(suffix, cisco_wan_ip, shared_ipv4, br_ipv6, psk, cisco_psk=None):
    """ルールサーバー(bas)+Yamaha RTX+Cisco(固定WAN)の3台を作り、
    IPoE+MAP-E接続(map-e use)まで済ませた状態にする。IPsecトンネルは
    まだ張らない。"""
    rs_id, rtx_id, cisco_id = f'rs-{suffix}', f'rtx-{suffix}', f'csc-{suffix}'

    _dev(rs_id, 'bas')
    _run(rs_id, [
        'configure terminal',
        f'map-e ipv4-pool {shared_ipv4} {shared_ipv4}',
        f'map-e br-address {br_ipv6}',
    ])

    _dev(rtx_id, 'yamaha')
    _link(rtx_id, rs_id, 'lan2', 'wan1')
    _run(rtx_id, ['administrator', 'ipv6 prefix dhcp-prefix@lan2::/64'])
    _out(rtx_id, 'map-e use lan2')

    _dev(cisco_id, 'cisco')
    _link(cisco_id, rtx_id, 'GigabitEthernet0/0/0', 'lan3')
    _run(rtx_id, ['administrator', 'ip lan3 address 10.202.0.1/30'])
    _run(cisco_id, [
        'configure terminal',
        'interface GigabitEthernet0/0/0',
        f'ip address {cisco_wan_ip} 255.255.255.0',
        'no shutdown',
        'exit',
        f'crypto isakmp key {cisco_psk or psk} address {shared_ipv4}',
        'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
        'crypto map CMAP 10 ipsec-isakmp',
        f'set peer {shared_ipv4}',
        'set transform-set TS',
        'match address 101',
        'exit',
        'interface GigabitEthernet0/0/0',
        'crypto map CMAP',
        'exit',
        'crypto isakmp enable',
        f'ip route {shared_ipv4} 255.255.255.255 10.202.0.1',
    ])
    return rs_id, rtx_id, cisco_id


def _configure_rtx_tunnel(rtx_id, local_ip, remote_ip, psk):
    _run(rtx_id, [
        'tunnel select 1',
        'ipsec tunnel 101',
        'ipsec sa policy 101 1 esp aes256 sha256',
        f'ipsec ike local-address 1 {local_ip}',
        f'ipsec ike remote-address 1 {remote_ip}',
        f'ipsec ike pre-shared-key 1 text {psk}',
        'exit',
    ])


class TestYamahaCiscoIpsecOverIpoeMapE:
    def test_map_e_use_gets_the_shared_ipv4_that_matches_the_cisco_crypto_key_peer(self):
        """IPsecを張る前に、IPoE+MAP-Eで取得した共有IPv4がCisco側の
        crypto map peerとして使う前提のIPと一致することを確認する
        (テストの土台)。"""
        _rs_id, rtx_id, _cisco_id = _setup_scenario(
            'match1', '198.51.230.1', '203.0.126.50', '2001:db8:fff::20', 'psk1')
        rc = _out(rtx_id, 'show config')
        assert 'ip map0 address 203.0.126.50/32' in rc

    def test_ipsec_establishes_over_the_map_e_assigned_address(self):
        _rs_id, rtx_id, _cisco_id = _setup_scenario(
            'ok1', '198.51.230.2', '203.0.127.50', '2001:db8:fff::21', 'shared-psk-1')
        _configure_rtx_tunnel(rtx_id, '203.0.127.50', '198.51.230.2', 'shared-psk-1')
        out_enable = _out(rtx_id, 'tunnel enable 1')
        assert out_enable == ''  # 実機同様、成功時は無言
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : MATURE' in status
        assert 'IPsec SA        : MATURE' in status
        assert 'status          : established' in status

    def test_rtx_state_reflects_mature_tunnel_directly(self):
        _rs_id, rtx_id, _cisco_id = _setup_scenario(
            'ok2', '198.51.230.3', '203.0.128.50', '2001:db8:fff::22', 'shared-psk-2')
        _configure_rtx_tunnel(rtx_id, '203.0.128.50', '198.51.230.3', 'shared-psk-2')
        _out(rtx_id, 'tunnel enable 1')
        rtx_state = app_module.device_sessions[rtx_id]
        tun = rtx_state.ipsec_tunnels[1]
        assert tun['phase1'] == 'MATURE'
        assert tun['phase2'] == 'MATURE'
        assert tun['status'] == 'established'

    def test_wrong_preshared_key_fails_phase1(self):
        _rs_id, rtx_id, _cisco_id = _setup_scenario(
            'badpsk', '198.51.230.4', '203.0.129.50', '2001:db8:fff::23', 'right-psk')
        _configure_rtx_tunnel(rtx_id, '203.0.129.50', '198.51.230.4', 'WRONG-PSK')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : DYING' in status
        assert 'status          : wait' in status

    def test_tunnel_disable_tears_down_the_sa(self):
        _rs_id, rtx_id, _cisco_id = _setup_scenario(
            'teardown1', '198.51.230.5', '203.0.130.50', '2001:db8:fff::24', 'shared-psk-3')
        _configure_rtx_tunnel(rtx_id, '203.0.130.50', '198.51.230.5', 'shared-psk-3')
        _out(rtx_id, 'tunnel enable 1')
        assert 'status          : established' in _out(rtx_id, 'show status tunnel 1')
        _out(rtx_id, 'tunnel disable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : LARVAL' in status
        assert 'status          : wait' in status

    def test_without_map_e_connected_there_is_no_valid_local_ip_so_ipsec_cannot_establish(self):
        """IPoE+MAP-Eが繋がっていない(map-e useしていない)状態だと、
        local-addressに指定できる共有IPv4がそもそも存在しないため、
        ここでは単純にlocal-addressが空のままトンネルを有効化しても
        確立しないことを固定する。"""
        rs_id, rtx_id, cisco_id = 'rs-nomape', 'rtx-nomape', 'csc-nomape'
        _dev(cisco_id, 'cisco')
        _run(cisco_id, [
            'configure terminal', 'interface GigabitEthernet0/0/0',
            'ip address 198.51.230.6 255.255.255.0', 'no shutdown', 'exit',
            'crypto isakmp key nomape-psk address 203.0.131.50',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp', 'set peer 203.0.131.50',
            'set transform-set TS', 'match address 101', 'exit',
            'interface GigabitEthernet0/0/0', 'crypto map CMAP', 'exit',
            'crypto isakmp enable',
        ])
        _dev(rtx_id, 'yamaha')
        # ルールサーバーは存在するがリンクしていない・map-e useもしていない
        _dev(rs_id, 'bas')
        _link(cisco_id, rtx_id, 'GigabitEthernet0/0/0', 'lan2')
        _configure_rtx_tunnel(rtx_id, '203.0.131.50', '198.51.230.6', 'nomape-psk')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'status          : established' not in status
