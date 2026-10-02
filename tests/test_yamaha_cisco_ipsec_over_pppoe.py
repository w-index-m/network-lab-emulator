"""
擬似FLETS網(PPPoE)経由でYamaha RTX ⇔ Cisco IOS のIPsecを実際に
張れるかの確認。ユーザー依頼: 「擬似ふれっつもうを経由して他のCiscoと
IPsec貼れるか試して欲しい」。

構成: Cisco(固定WAN IP) -- Yamaha RTX(擬似FLETS BAS経由で動的IP取得)
RTXは先に `pp enable` でPPPoE接続して実IPを取得してから、そのIPを
`ipsec ike local-address` に使ってCiscoとIKE/IPsecネゴシエーションする
— 現実のFLETS+RTX+IPsecの典型構成と同じ順序。

実装: engine/ike_engine.py は元々Si-R/SR-S ⇔ Cisco IOS/ASAのIKE/IPsec
ネゴシエーションしか対応していなかった。Yamahaの`tunnel select N`配下の
IPsec設定をSi-Rの`ipsec_tunnels`辞書と全く同じ形式(local_ip/remote_ip/
preshared/encryption/hash/dh_group/ike_mode/ike_lifetime/protocol/
phase1/phase2/status)でstate.ipsec_tunnelsに持たせ、ike_engine.py内の
`dt in ('sir', 'srs')`判定をすべて`('sir', 'srs', 'yamaha')`に拡張
することで、新しいネゴシエーションロジックを一切増やさずに相乗りさせた
(Cisco側からYamahaを対向として見つける_find_peer()のsir/srs分岐も
含む)。
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


def _setup_scenario(suffix, cisco_wan_ip, pool_start, pool_end, pppoe_user,
                     pppoe_pass, psk, cisco_psk=None):
    """Cisco(固定WAN) -- BAS(擬似FLETS) -- Yamaha RTX の3台を作り、
    PPPoE接続まで済ませた状態にする。IPsecトンネルはまだ張らない。"""
    cisco_id, bas_id, rtx_id = f'csc-{suffix}', f'bas-{suffix}', f'rtx-{suffix}'
    _dev(cisco_id, 'cisco')
    _run(cisco_id, [
        'configure terminal',
        'interface GigabitEthernet0/0/0',
        f'ip address {cisco_wan_ip} 255.255.255.0',
        'no shutdown',
        'exit',
        f'crypto isakmp key {cisco_psk or psk} address {pool_start}',
        'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
        'crypto map CMAP 10 ipsec-isakmp',
        f'set peer {pool_start}',
        'set transform-set TS',
        'match address 101',
        'exit',
        'interface GigabitEthernet0/0/0',
        'crypto map CMAP',
        'exit',
        'crypto isakmp enable',
    ])

    _dev(bas_id, 'bas')
    _run(bas_id, [
        'configure terminal',
        f'ip pool {pool_start} {pool_end}/16',
        f'pppoe-user {pppoe_user} {pppoe_pass}',
    ])

    _dev(rtx_id, 'yamaha')
    _link(rtx_id, bas_id, 'lan2', 'wan1')
    _link(cisco_id, rtx_id, 'GigabitEthernet0/0/0', 'lan2')
    _run(rtx_id, ['administrator', 'pp select 1', 'pppoe use lan2',
                  f'pp auth myname {pppoe_user} {pppoe_pass}', 'exit'])
    _out(rtx_id, 'pp enable 1')
    return cisco_id, bas_id, rtx_id


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


class TestYamahaCiscoIpsecOverPppoe:
    def test_pp1_gets_the_dynamic_ip_that_matches_the_cisco_crypto_key_peer(self):
        """IPsecを張る前に、擬似PPPoEで取得したIPがCisco側のcrypto map
        peerとして使う前提のIPと一致することを確認する(テストの土台)。"""
        _cisco_id, _bas_id, rtx_id = _setup_scenario(
            'match1', '198.51.210.1', '100.64.20.10', '100.64.20.20',
            'u1', 'p1', 'psk1')
        rc = _out(rtx_id, 'show config')
        assert 'ip pp1 address 100.64.20.10/16' in rc

    def test_ipsec_establishes_over_the_pppoe_assigned_address(self):
        _cisco_id, _bas_id, rtx_id = _setup_scenario(
            'ok1', '198.51.210.2', '100.64.21.10', '100.64.21.20',
            'u2', 'p2', 'shared-psk-1')
        _configure_rtx_tunnel(rtx_id, '100.64.21.10', '198.51.210.2', 'shared-psk-1')
        out_enable = _out(rtx_id, 'tunnel enable 1')
        assert out_enable == ''  # 実機同様、成功時は無言
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : MATURE' in status
        assert 'IPsec SA        : MATURE' in status
        assert 'status          : established' in status

    def test_cisco_also_sees_the_tunnel_established(self):
        """Cisco側から見ても(対向探索で見つけたYamahaとの)ネゴシエーション
        結果が反映されることを、ike_engineのnegotiate側で直接確認する。"""
        _cisco_id, _bas_id, rtx_id = _setup_scenario(
            'ok2', '198.51.210.3', '100.64.22.10', '100.64.22.20',
            'u3', 'p3', 'shared-psk-2')
        _configure_rtx_tunnel(rtx_id, '100.64.22.10', '198.51.210.3', 'shared-psk-2')
        _out(rtx_id, 'tunnel enable 1')
        rtx_state = app_module.device_sessions[rtx_id]
        tun = rtx_state.ipsec_tunnels[1]
        assert tun['phase1'] == 'MATURE'
        assert tun['phase2'] == 'MATURE'
        assert tun['status'] == 'established'

    def test_wrong_preshared_key_fails_phase1(self):
        _cisco_id, _bas_id, rtx_id = _setup_scenario(
            'badpsk', '198.51.210.4', '100.64.23.10', '100.64.23.20',
            'u4', 'p4', 'right-psk')
        _configure_rtx_tunnel(rtx_id, '100.64.23.10', '198.51.210.4', 'WRONG-PSK')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : DYING' in status
        assert 'status          : wait' in status

    def test_tunnel_disable_tears_down_the_sa(self):
        _cisco_id, _bas_id, rtx_id = _setup_scenario(
            'teardown1', '198.51.210.5', '100.64.24.10', '100.64.24.20',
            'u5', 'p5', 'shared-psk-3')
        _configure_rtx_tunnel(rtx_id, '100.64.24.10', '198.51.210.5', 'shared-psk-3')
        _out(rtx_id, 'tunnel enable 1')
        assert 'status          : established' in _out(rtx_id, 'show status tunnel 1')
        _out(rtx_id, 'tunnel disable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : LARVAL' in status
        assert 'status          : wait' in status

    def test_without_pppoe_connected_there_is_no_valid_local_ip_so_ipsec_cannot_establish(self):
        """PPPoEが繋がっていない(pp enableしていない)状態だと、
        local-addressに指定できる動的WAN IPがそもそも存在しないため、
        ここでは単純にlocal-addressが空のままトンネルを有効化しても
        確立しないことを固定する。"""
        cisco_id, bas_id, rtx_id = 'csc-nopp', 'bas-nopp', 'rtx-nopp'
        _dev(cisco_id, 'cisco')
        _run(cisco_id, [
            'configure terminal', 'interface GigabitEthernet0/0/0',
            'ip address 198.51.210.6 255.255.255.0', 'no shutdown', 'exit',
            'crypto isakmp key nopp-psk address 100.64.25.10',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp', 'set peer 100.64.25.10',
            'set transform-set TS', 'match address 101', 'exit',
            'interface GigabitEthernet0/0/0', 'crypto map CMAP', 'exit',
            'crypto isakmp enable',
        ])
        _dev(rtx_id, 'yamaha')
        # BASは存在するがリンクしていない・pp enableもしていない
        _dev(bas_id, 'bas')
        _link(cisco_id, rtx_id, 'GigabitEthernet0/0/0', 'lan2')
        _configure_rtx_tunnel(rtx_id, '100.64.25.10', '198.51.210.6', 'nopp-psk')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'status          : established' not in status
