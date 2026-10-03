"""
Cisco ASAをPPPoE+IPsecマトリクス(`docs/pppoe-ipsec-mesh-status.md`)の
検証対象に加える(ユーザー依頼「ASAやFortiGateも追加で試験幅を増やして
欲しい」)。

**ASA自体にPPPoEクライアント機能は追加していない**(実機のASAも
PPPoE WANはサポートしない機種が通常のため、Ciscoが既存テストで
担っているのと同じ「固定WAN IPのIPsec対向」役を割り当てる)。
IPsec自体は`engine/ike_engine.py`が元々`('cisco', 'catalyst', 'asa')`
を対向として対応済みなので、**新しいネゴシエーションロジックは
一切追加していない** — 実際に繋がるかをこのテストで初めて確認した。

**この調査で見つけた実バグ(2件、ASA固有ではなく既存の潜在バグ)**:

1. `crypto map <name> <seq> match address <acl>` のハンドラ
   (IOS版・ASA版の両方)が `[seq] = {'acl': acl}` と辞書を丸ごと
   代入していたため、`match address`を`set peer`/`set transform-set`
   より後に打つと、既存のpeer/transform_setが消えてしまっていた
   (`match address`が先に来る設定順なら表面化しない潜在バグ)。
   `setdefault(seq, {})['acl'] = acl`に修正。
2. ASAの`tunnel-group <peer> ipsec-attributes`サブモードは
   `state.mode`を変えずに`_tg_attr_mode`フラグだけで管理している。
   `_asa_process`冒頭の`exit`/`end`/`quit`ハンドラがこのフラグを
   見ておらず、`ipsec-attributes`配下から`exit`すると
   `state.mode=='config'`のまま一般のconfig→exec判定に落ちてしまい、
   1回の`exit`でconfigモードごと抜けてしまっていた(実機は
   ipsec-attributesだけ抜けてconfigに留まる)。`_tg_attr_mode`を
   先にチェックしてクリアするよう修正。

この2つ目のバグのせいで、修正前は`crypto isakmp enable outside`が
`state.mode != 'config'`と誤判定されて無視され、`isakmp_enabled`が
立たず、IKEネゴシエーションが絶対に`established`にならなかった
(ライブ検証で最初に踏んだ)。

Live-verified: Yamaha RTX(PPPoE払い出しIP)⇔ASA(固定WAN)で実際に
IPsecが確立することを確認(`IKE negotiation : MATURE` / `IPsec SA :
MATURE` / `status : established`)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
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
                     pppoe_pass, psk):
    """ASA(固定WAN) -- bas(擬似FLET'S) -- Yamaha RTX の3台を作り、
    PPPoE接続まで済ませた状態にする。IPsecトンネルはまだ張らない。"""
    asa_id, bas_id, rtx_id = f'asa-{suffix}', f'basmesh-{suffix}', f'rtxmesh-{suffix}'
    _dev(asa_id, 'asa')
    _run(asa_id, [
        'configure terminal',
        'interface GigabitEthernet0/0',
        f'ip address {cisco_wan_ip} 255.255.255.0',
        'nameif outside',
        'security-level 0',
        'no shutdown',
        'exit',
    ])

    _dev(bas_id, 'bas')
    _run(bas_id, [
        'configure terminal',
        f'ip pool {pool_start} {pool_end}/16',
        f'pppoe-user {pppoe_user} {pppoe_pass}',
    ])

    _dev(rtx_id, 'yamaha')
    _link(rtx_id, bas_id, 'lan2', 'wan1')
    _link(asa_id, rtx_id, 'GigabitEthernet0/0', 'lan2')
    _run(rtx_id, ['administrator', 'pp select 1', 'pppoe use lan2',
                  f'pp auth myname {pppoe_user} {pppoe_pass}', 'exit'])
    _out(rtx_id, 'pp enable 1')
    return asa_id, bas_id, rtx_id


def _configure_asa_crypto_map(asa_id, peer_ip, transform_set, psk):
    _run(asa_id, [
        'configure terminal',
        f'crypto ipsec transform-set {transform_set} esp-aes esp-sha-hmac',
        'crypto map CMAP 10 ipsec-isakmp',
        f'crypto map CMAP 10 set peer {peer_ip}',
        f'crypto map CMAP 10 set transform-set {transform_set}',
        'crypto map CMAP 10 match address 101',
        'crypto map CMAP interface outside',
        f'tunnel-group {peer_ip} type ipsec-l2l',
        f'tunnel-group {peer_ip} ipsec-attributes',
        f'ikev1 pre-shared-key {psk}',
        'exit',
        'crypto isakmp enable outside',
    ])


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


class TestAsaTunnelGroupExitBug:
    """tunnel-group ipsec-attributesの"exit"が意図せずconfigモードまで
    抜けてしまうバグの固定(regression防止)。"""

    def test_exit_from_ipsec_attributes_stays_in_config_mode(self):
        _dev('asa-exitbug-1', 'asa')
        _run('asa-exitbug-1', ['configure terminal',
                               'tunnel-group 1.2.3.4 type ipsec-l2l',
                               'tunnel-group 1.2.3.4 ipsec-attributes',
                               'ikev1 pre-shared-key abc',
                               'exit'])
        state = app_module.device_sessions['asa-exitbug-1']
        assert state.mode == 'config'
        assert getattr(state, '_tg_attr_mode', False) is False

    def test_crypto_isakmp_enable_after_exit_is_actually_registered(self):
        _dev('asa-exitbug-2', 'asa')
        _run('asa-exitbug-2', ['configure terminal',
                               'tunnel-group 1.2.3.4 type ipsec-l2l',
                               'tunnel-group 1.2.3.4 ipsec-attributes',
                               'ikev1 pre-shared-key abc',
                               'exit',
                               'crypto isakmp enable outside'])
        state = app_module.device_sessions['asa-exitbug-2']
        assert state.ipsec_crypto.get('isakmp_enabled') == 'outside'


class TestCryptoMapMatchAddressOrderingBug:
    """crypto map ... match address が peer/transform-set を消してしまう
    バグの固定(regression防止。IOS/ASA両方のハンドラを直接確認)。"""

    def test_asa_match_address_after_peer_and_transform_set_preserves_them(self):
        _dev('asa-matchbug-1', 'asa')
        _run('asa-matchbug-1', [
            'configure terminal',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp',
            'crypto map CMAP 10 set peer 9.9.9.9',
            'crypto map CMAP 10 set transform-set TS',
            'crypto map CMAP 10 match address 101',
        ])
        cmap = app_module.device_sessions['asa-matchbug-1'].ipsec_crypto['crypto_maps']['cmap'][10]
        assert cmap['peer'] == '9.9.9.9'
        assert cmap['transform_set'] == 'ts'
        assert cmap['acl'] == '101'

    def test_cisco_ios_inline_match_address_after_peer_and_transform_set_preserves_them(self):
        """IOSには"crypto map X N ipsec-isakmp"でconfig-cryptoサブモードに
        入ってから"set peer"/"match address"と打つ形の他に、サブモードへ
        入らず"crypto map X N set peer ..."のように毎回mapname/seqを
        繰り返すinline形式もある(ASAの一行形式と同じ)。このinline形式
        側のハンドラが今回の対象。"""
        _dev('cisco-matchbug-1', 'cisco')
        _run('cisco-matchbug-1', [
            'configure terminal',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map cmap 10 set peer 9.9.9.9',
            'crypto map cmap 10 set transform-set TS',
            'crypto map cmap 10 match address 101',
        ])
        cmap = app_module.device_sessions['cisco-matchbug-1'].ipsec_crypto['crypto_maps']['cmap'][10]
        assert cmap['peer'] == '9.9.9.9'
        assert 'transform_set' in cmap
        assert cmap['acl'] == '101'


class TestAsaIpsecOverYamahaPppoe:
    def test_ipsec_establishes_between_asa_and_pppoe_assigned_yamaha(self):
        asa_id, _bas_id, rtx_id = _setup_scenario(
            'ok1', '198.51.230.10', '100.80.10.10', '100.80.10.20', 'au1', 'ap1', 'asa-shared-psk-1')
        _configure_asa_crypto_map(asa_id, '100.80.10.10', 'TS', 'asa-shared-psk-1')
        _configure_rtx_tunnel(rtx_id, '100.80.10.10', '198.51.230.10', 'asa-shared-psk-1')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'IKE negotiation : MATURE' in status
        assert 'IPsec SA        : MATURE' in status
        assert 'status          : established' in status

    def test_wrong_preshared_key_does_not_establish(self):
        asa_id, _bas_id, rtx_id = _setup_scenario(
            'badpsk', '198.51.230.11', '100.80.11.10', '100.80.11.20', 'au2', 'ap2', 'right-psk')
        _configure_asa_crypto_map(asa_id, '100.80.11.10', 'TS', 'right-psk')
        _configure_rtx_tunnel(rtx_id, '100.80.11.10', '198.51.230.11', 'WRONG-PSK')
        _out(rtx_id, 'tunnel enable 1')
        status = _out(rtx_id, 'show status tunnel 1')
        assert 'status          : established' not in status
