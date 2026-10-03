"""
Cisco ASA 5505実機のPPPoEクライアント機能を追加する(ユーザー依頼
「PPPoE ASAやfortigateもいけると思うのでお願いします」— FortiGateは
既にPPPoE WAN(`set mode pppoe`)対応済みだったため、本ファイルは残る
ASA側のみを対象にする)。

実機ASA 5505のPPPoEクライアント構文:
    interface Vlan2
     ip address pppoe setroute
    vpdn group PPPOE request dialout pppoe
    vpdn group PPPOE localname <user>
    vpdn group PPPOE ppp authentication chap
    vpdn username <user> password <pass>

Yamahaの"pp enable"/FortiGateの"set mode pppoe"と同じ
`engine.protocols.PppoeEngine`をそのまま使い、新しいネゴシエーション
ロジックは追加していない。コマンド順序に依存せず
(`vpdn group`/`vpdn username`がどの順で来ても)、localname+username+
passwordが揃った時点でpendingな"ip address pppoe"インタフェースへの
接続を試みる(`app.py`の`cli_command()`、rule_engine.process()実行後)。

"ip address pppoe setroute"は、PPPoE接続で得たゲートウェイ経由の
デフォルトルートを実際に`rib_engine`へ登録する(Yamahaの
"ip route default gateway pp N"と同じ役割)。

**この機能を実装中に見つけた既存の別の潜在的スコープ**(ASA固有ではなく
pre-existing、今回のPPPoE追加が原因ではない): ASAの"show route"
(`_asa_show_route`)は元々`state.routes`(ASAの"route"コマンドでのみ
追加される独自リスト)しか見ておらず、`rib_engine`に登録された
スタティックルート(本機能のPPPoE setrouteや、他の仕組みで
rib_engine経由で入った経路)を表示できていなかった。他vendorの
"show ip route"同様、rib_engineの経路も合わせて表示するよう
`_asa_show_route`を拡張した(dedupeはnetwork/prefixで実施)。

また、ASA/Cisco間のIPsecで"show crypto ipsec sa"
(`app.py`の`handle_protocol_show`)は`icmp_engine.ipsec_tunnels`
というDPD(Dead Peer Detection)専用の別トラッキング構造を見ており、
これは"crypto isakmp keepalive"のDPD機能テスト専用に
`icmp_engine.register_ipsec()`経由でのみ登録される——DPDを設定して
いない通常のIPsecネゴシエーション成立時には何も登録されないため、
"show crypto ipsec sa"は常に"There are no ipsec sas."を返す
(ASA固有ではなくCisco IOS/ASA共通の既存スコープ、今回のPPPoE追加が
露見させたが原因ではない)。本テストでは`show crypto isakmp sa`
(Phase1、DPDに依存せずstate.ipsec_peersを直接見る)と
`state.ipsec_peers`の直接確認でネゴシエーション成立を確認する。

Live-verified: PPPoE経由でASAが共有IPv4を取得(`show interface ip
brief`に反映)、"setroute"による実デフォルトルート登録
(`show route`に反映)、そのIPを使ってCisco IOSと実際にIKE Phase1/
Phase2が確立すること(`state.ipsec_peers`で`status: established`/
`phase1: MATURE`/`phase2: MATURE`)を確認。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='asa'):
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


def _setup_pppoe(suffix, pool_start, pool_end, user, password):
    """ASA -- bas(擬似FLET'S) の2台を作り、ASA側にPPPoEクライアント設定
    (まだ接続はしない状態にするコマンド分割は呼び出し側で行う)。"""
    asa_id, bas_id = f'asa-pppoe-{suffix}', f'bas-pppoe-{suffix}'
    _dev(asa_id, 'asa')
    _dev(bas_id, 'bas')
    _link(asa_id, bas_id, 'GigabitEthernet0/0', 'wan1')

    _run(bas_id, [
        'configure terminal',
        f'ip pool {pool_start} {pool_end}/16',
        f'pppoe-user {user} {password}',
    ])
    return asa_id, bas_id


class TestAsaPppoeWanBasic:
    def test_ip_address_pppoe_assigns_real_pool_ip(self):
        asa_id, _bas_id = _setup_pppoe('ok1', '100.95.10.10', '100.95.10.20', 'au1', 'ap1')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'nameif outside',
            'security-level 0',
            'no shutdown',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname au1',
            'vpdn group PPPOE ppp authentication chap',
            'vpdn username au1 password ap1',
        ])
        state = app_module.device_sessions[asa_id]
        assert state.interfaces['GigabitEthernet0/0']['ip'].startswith('100.95.10.')
        assert state.asa_pppoe['GigabitEthernet0/0']['connected'] is True

    def test_command_order_independent_vpdn_username_before_group(self):
        """"vpdn username"が"vpdn group"より先に来ても(実機では通常後だが
        エミュレータの要件はコマンド順序非依存)接続できることを確認する。"""
        asa_id, _bas_id = _setup_pppoe('ok2', '100.96.10.10', '100.96.10.20', 'au2', 'ap2')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'exit',
            'vpdn username au2 password ap2',
            'vpdn group PPPOE localname au2',
            'vpdn group PPPOE request dialout pppoe',
        ])
        state = app_module.device_sessions[asa_id]
        assert state.asa_pppoe['GigabitEthernet0/0']['connected'] is True

    def test_wrong_password_does_not_connect(self):
        asa_id, _bas_id = _setup_pppoe('bad1', '100.97.10.10', '100.97.10.20', 'au3', 'rightpass')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname au3',
            'vpdn username au3 password WRONG',
        ])
        state = app_module.device_sessions[asa_id]
        assert not state.asa_pppoe.get('GigabitEthernet0/0', {}).get('connected')
        assert not state.interfaces['GigabitEthernet0/0'].get('ip', '').startswith('100.97.10.')

    def test_no_bas_linked_does_not_connect(self):
        asa_id = 'asa-pppoe-nolink'
        _dev(asa_id, 'asa')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname nouser',
            'vpdn username nouser password nopass',
        ])
        state = app_module.device_sessions[asa_id]
        assert not state.asa_pppoe.get('GigabitEthernet0/0', {}).get('connected')


class TestAsaPppoeSetroute:
    def test_setroute_registers_real_default_route(self):
        asa_id, bas_id = _setup_pppoe('setroute1', '100.98.10.10', '100.98.10.20', 'su1', 'sp1')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe setroute',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname su1',
            'vpdn username su1 password sp1',
        ])
        route_out = _out(asa_id, 'show route')
        assert 'S*       0.0.0.0/0' in route_out
        assert 'Gateway of last resort is 100.64.0.254' in route_out

    def test_without_setroute_no_default_route(self):
        asa_id, _bas_id = _setup_pppoe('noroute1', '100.99.10.10', '100.99.10.20', 'nu1', 'np1')
        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname nu1',
            'vpdn username nu1 password np1',
        ])
        route_out = _out(asa_id, 'show route')
        assert 'Gateway of last resort is not set' in route_out
        assert '0.0.0.0/0' not in route_out


class TestAsaPppoeIpsecToCisco:
    def test_ipsec_establishes_between_pppoe_assigned_asa_and_cisco(self):
        asa_id, _bas_id = _setup_pppoe('ipsec1', '100.80.20.10', '100.80.20.20', 'iu1', 'ip1')
        cisco_id = 'cisco-pppoe-ipsec1'
        _dev(cisco_id, 'cisco')
        _link(cisco_id, asa_id, 'GigabitEthernet0/0', 'GigabitEthernet0/1')

        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'nameif outside',
            'security-level 0',
            'no shutdown',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname iu1',
            'vpdn username iu1 password ip1',
        ])
        asa_ip = app_module.device_sessions[asa_id].interfaces['GigabitEthernet0/0']['ip']
        assert asa_ip.startswith('100.80.20.')

        _run(cisco_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address 198.51.250.30 255.255.255.0',
            'no shutdown',
            'exit',
            f'crypto isakmp key pppoe-ipsec-psk address {asa_ip}',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp',
            f'crypto map CMAP 10 set peer {asa_ip}',
            'crypto map CMAP 10 set transform-set TS',
            'crypto map CMAP 10 match address 101',
            'interface GigabitEthernet0/0',
            'crypto map CMAP',
            'exit',
            'crypto isakmp enable',
        ])

        _run(asa_id, [
            'configure terminal',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp',
            'crypto map CMAP 10 set peer 198.51.250.30',
            'crypto map CMAP 10 set transform-set TS',
            'crypto map CMAP 10 match address 101',
            'crypto map CMAP interface outside',
            'tunnel-group 198.51.250.30 type ipsec-l2l',
            'tunnel-group 198.51.250.30 ipsec-attributes',
            'ikev1 pre-shared-key pppoe-ipsec-psk',
            'exit',
            'crypto isakmp enable outside',
        ])

        state = app_module.device_sessions[asa_id]
        peer = state.ipsec_peers.get('198.51.250.30', {})
        assert peer.get('status') == 'established'
        assert peer.get('phase1') == 'MATURE'
        assert peer.get('phase2') == 'MATURE'

        isakmp_sa = _out(asa_id, 'show crypto isakmp sa')
        assert 'IKE Peer: 198.51.250.30' in isakmp_sa
        assert 'MM_ACTIVE' in isakmp_sa

    def test_wrong_preshared_key_does_not_establish(self):
        asa_id, _bas_id = _setup_pppoe('ipsecbad1', '100.81.20.10', '100.81.20.20', 'bu1', 'bp1')
        cisco_id = 'cisco-pppoe-ipsecbad1'
        _dev(cisco_id, 'cisco')
        _link(cisco_id, asa_id, 'GigabitEthernet0/0', 'GigabitEthernet0/1')

        _run(asa_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address pppoe',
            'nameif outside',
            'security-level 0',
            'no shutdown',
            'exit',
            'vpdn group PPPOE request dialout pppoe',
            'vpdn group PPPOE localname bu1',
            'vpdn username bu1 password bp1',
        ])
        asa_ip = app_module.device_sessions[asa_id].interfaces['GigabitEthernet0/0']['ip']

        _run(cisco_id, [
            'configure terminal',
            'interface GigabitEthernet0/0',
            'ip address 198.51.251.30 255.255.255.0',
            'no shutdown',
            'exit',
            f'crypto isakmp key right-psk address {asa_ip}',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp',
            f'crypto map CMAP 10 set peer {asa_ip}',
            'crypto map CMAP 10 set transform-set TS',
            'crypto map CMAP 10 match address 101',
            'interface GigabitEthernet0/0',
            'crypto map CMAP',
            'exit',
            'crypto isakmp enable',
        ])

        _run(asa_id, [
            'configure terminal',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp',
            'crypto map CMAP 10 set peer 198.51.251.30',
            'crypto map CMAP 10 set transform-set TS',
            'crypto map CMAP 10 match address 101',
            'crypto map CMAP interface outside',
            'tunnel-group 198.51.251.30 type ipsec-l2l',
            'tunnel-group 198.51.251.30 ipsec-attributes',
            'ikev1 pre-shared-key WRONG-psk',
            'exit',
            'crypto isakmp enable outside',
        ])

        state = app_module.device_sessions[asa_id]
        peer = state.ipsec_peers.get('198.51.251.30', {})
        assert peer.get('status') != 'established'
