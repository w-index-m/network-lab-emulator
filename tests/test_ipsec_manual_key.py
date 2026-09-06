"""
Si-R の手動鍵設定 IPsec（remote ap ipsec type manual）。

IKEでは negotiate_ipsec() がPhase1/Phase2の一致確認を行うが、手動鍵設定は
ネゴシエーションを介さず、両側の send/receive（SPI・プロトコル・暗号鍵・
認証鍵）が噛み合った時点でSAが張られる。

「暗号化しないトンネル」は実機ではこちら側の機能で組む:
  - protocol=ah, auth=hmac-sha256 等（認証のみ、暗号化なし）
  - protocol=esp, encrypt=null（ESP-NULL。フレーミングはESPだが機密性なし）

コマンドリファレンス 10.2.27〜10.2.36 で確認した仕様:
  - SA作成可否は auth/encrypt の定義有無とプロトコルの組み合わせで決まる
    （10.2.29の表）: protocol=ah は auth必須、protocol=esp は encrypt必須
  - encrypt/authアルゴリズムが none/null の場合は鍵を指定できない
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.ike_engine import manual_sa_creatable

client = TestClient(app_module.app)


def _dev(id_, type_='sir'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


def _tunnel_status_line(dev_id):
    return _cli(dev_id, 'show ipsec tunnel').splitlines()[-1]


def _setup_manual_pair(a, b, ip_a, ip_b, cmds_a, cmds_b):
    _dev(a)
    _dev(b)
    _run(a, [
        'configure', f'lan 1 ip address {ip_a}/30 3',
        f'remote 1 ap 0 tunnel local {ip_a}',
        f'remote 1 ap 0 tunnel remote {ip_b}',
        'remote 1 ap 0 ipsec type manual',
        *cmds_a,
    ])
    _run(b, [
        'configure', f'lan 1 ip address {ip_b}/30 3',
        f'remote 1 ap 0 tunnel local {ip_b}',
        f'remote 1 ap 0 tunnel remote {ip_a}',
        'remote 1 ap 0 ipsec type manual',
        *cmds_b,
    ])


class TestManualSaCreatableTable:
    """コマンドリファレンス 10.2.29 の表そのもの。"""

    def test_ah_requires_auth(self):
        assert manual_sa_creatable(
            {'protocol': 'ah', 'auth': {'algo': 'hmac-sha256'}}) is True
        assert manual_sa_creatable(
            {'protocol': 'ah', 'auth': {'algo': 'none'}}) is False
        assert manual_sa_creatable({'protocol': 'ah'}) is False

    def test_ah_ignores_encrypt(self):
        # authさえあればencryptの有無は無関係にSA作成可
        assert manual_sa_creatable({
            'protocol': 'ah', 'auth': {'algo': 'hmac-sha256'},
            'encrypt': {'algo': 'aes-cbc-256'},
        }) is True

    def test_esp_requires_encrypt(self):
        assert manual_sa_creatable(
            {'protocol': 'esp', 'encrypt': {'algo': 'aes-cbc-256'}}) is True
        assert manual_sa_creatable(
            {'protocol': 'esp', 'encrypt': {'algo': 'none'}}) is False
        assert manual_sa_creatable({'protocol': 'esp'}) is False

    def test_esp_null_still_creatable(self):
        # nullは「暗号化なし」だが定義されたアルゴリズムなのでSA作成可
        assert manual_sa_creatable(
            {'protocol': 'esp', 'encrypt': {'algo': 'null'}}) is True

    def test_esp_ignores_auth_alone(self):
        # encryptが無ければauthだけではesp SAは作れない
        assert manual_sa_creatable(
            {'protocol': 'esp', 'auth': {'algo': 'hmac-sha256'}}) is False

    def test_no_protocol_never_creatable(self):
        assert manual_sa_creatable({
            'auth': {'algo': 'hmac-sha256'}, 'encrypt': {'algo': 'aes-cbc-256'},
        }) is False


class TestManualKeyConfigParsing:
    def test_spi_hex_range(self):
        _dev('mk-p1')
        _run('mk-p1', ['configure', 'remote 1 ap 0 ipsec type manual'])
        assert 'format error' in _run(
            'mk-p1', ['configure', 'remote 1 ap 0 ipsec send spi 50'])  # < 0x100
        assert _run('mk-p1', ['configure', 'remote 1 ap 0 ipsec send spi abcdef']) == ''

    def test_protocol_must_be_none_esp_or_ah(self):
        # 未知の値は本コマンドの正規表現にマッチしないため、
        # send/receiveのprotocolとして保存されない
        # （このリポジトリの他のSi-Rコマンドと同じく、認識できない入力は
        #  黙って無視される。エラーメッセージは出ない）。
        _dev('mk-p2')
        _run('mk-p2', ['configure', 'remote 1 ap 0 ipsec send protocol tcp'])
        state = app_module.device_sessions['mk-p2']
        assert state.ipsec_tunnels.get(1, {}).get('manual_send', {}).get('protocol') is None

        assert _run('mk-p2', ['configure', 'remote 1 ap 0 ipsec send protocol ah']) == ''
        state = app_module.device_sessions['mk-p2']
        assert state.ipsec_tunnels[1]['manual_send']['protocol'] == 'ah'

    def test_encrypt_rejects_unknown_algorithm(self):
        _dev('mk-p3')
        out = _run('mk-p3', ['configure', 'remote 1 ap 0 ipsec send encrypt rc4'])
        assert 'format error' in out

    def test_encrypt_null_and_none_reject_key(self):
        _dev('mk-p4')
        assert 'format error' in _run(
            'mk-p4', ['configure', 'remote 1 ap 0 ipsec send encrypt null text key1'])
        assert 'format error' in _run(
            'mk-p4', ['configure', 'remote 1 ap 0 ipsec send encrypt none text key1'])
        assert _run('mk-p4', ['configure', 'remote 1 ap 0 ipsec send encrypt null']) == ''

    def test_auth_none_rejects_key(self):
        _dev('mk-p5')
        assert 'format error' in _run(
            'mk-p5', ['configure', 'remote 1 ap 0 ipsec send auth none text key1'])

    def test_auth_rejects_unknown_algorithm(self):
        _dev('mk-p6')
        out = _run('mk-p6', ['configure', 'remote 1 ap 0 ipsec send auth hmac-sha3'])
        assert 'format error' in out

    def test_range_any4_accepted(self):
        _dev('mk-p7')
        assert _run('mk-p7', [
            'configure', 'remote 1 ap 0 ipsec send range any4 any4']) == ''

    def test_range_with_prefix_accepted(self):
        _dev('mk-p8')
        assert _run('mk-p8', [
            'configure',
            'remote 1 ap 0 ipsec send range 192.168.1.0/24 192.168.2.0/24']) == ''


class TestManualKeyEstablishment:
    def test_ah_only_tunnel_establishes_without_encryption(self):
        """認証のみ(AH)＝暗号化しないトンネル。"""
        _setup_manual_pair(
            'mk-e1', 'mk-e2', '192.0.2.121', '192.0.2.122',
            cmds_a=['remote 1 ap 0 ipsec send spi 1000',
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text mykey12345678',
                   'remote 1 ap 0 ipsec receive spi 2000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text peerkey1234567'],
            cmds_b=['remote 1 ap 0 ipsec send spi 2000',
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text peerkey1234567',
                   'remote 1 ap 0 ipsec receive spi 1000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text mykey12345678'],
        )
        line = _tunnel_status_line('mk-e1')
        assert 'Established' in line
        assert 'none' in line  # Encrypt列が none（暗号化していない証拠）

        sa = _cli('mk-e1', 'show ipsec sa')
        assert 'AH' in sa
        assert '0x00001000' in sa or '0x00002000' in sa

    def test_esp_null_tunnel_establishes_without_confidentiality(self):
        """ESP-NULL＝ESPのフレーミングはあるが機密性なし。"""
        _setup_manual_pair(
            'mk-e3', 'mk-e4', '192.0.2.125', '192.0.2.126',
            cmds_a=['remote 1 ap 0 ipsec send spi 3000',
                   'remote 1 ap 0 ipsec send protocol esp',
                   'remote 1 ap 0 ipsec send encrypt null',
                   'remote 1 ap 0 ipsec receive spi 4000',
                   'remote 1 ap 0 ipsec receive protocol esp',
                   'remote 1 ap 0 ipsec receive encrypt null'],
            cmds_b=['remote 1 ap 0 ipsec send spi 4000',
                   'remote 1 ap 0 ipsec send protocol esp',
                   'remote 1 ap 0 ipsec send encrypt null',
                   'remote 1 ap 0 ipsec receive spi 3000',
                   'remote 1 ap 0 ipsec receive protocol esp',
                   'remote 1 ap 0 ipsec receive encrypt null'],
        )
        line = _tunnel_status_line('mk-e3')
        assert 'Established' in line
        assert 'null' in line

    def test_encrypted_manual_tunnel_still_works(self):
        """比較対象: 通常どおり暗号化ありでも成立すること。"""
        _setup_manual_pair(
            'mk-e5', 'mk-e6', '192.0.2.129', '192.0.2.130',
            cmds_a=['remote 1 ap 0 ipsec send spi 5000',
                   'remote 1 ap 0 ipsec send protocol esp',
                   'remote 1 ap 0 ipsec send encrypt aes-cbc-256 text 0123456789abcdef01234567',
                   'remote 1 ap 0 ipsec receive spi 6000',
                   'remote 1 ap 0 ipsec receive protocol esp',
                   'remote 1 ap 0 ipsec receive encrypt aes-cbc-256 text fedcba9876543210fedcba98'],
            cmds_b=['remote 1 ap 0 ipsec send spi 6000',
                   'remote 1 ap 0 ipsec send protocol esp',
                   'remote 1 ap 0 ipsec send encrypt aes-cbc-256 text fedcba9876543210fedcba98',
                   'remote 1 ap 0 ipsec receive spi 5000',
                   'remote 1 ap 0 ipsec receive protocol esp',
                   'remote 1 ap 0 ipsec receive encrypt aes-cbc-256 text 0123456789abcdef01234567'],
        )
        line = _tunnel_status_line('mk-e5')
        assert 'Established' in line
        assert 'aes-cbc-256' in line


class TestManualKeyMismatch:
    def test_spi_mismatch_stays_waiting(self):
        _setup_manual_pair(
            'mk-m1', 'mk-m2', '192.0.2.133', '192.0.2.134',
            cmds_a=['remote 1 ap 0 ipsec send spi 1000',
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text key1',
                   'remote 1 ap 0 ipsec receive spi 2000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text key2'],
            cmds_b=['remote 1 ap 0 ipsec send spi 9999',  # わざと不一致
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text key2',
                   'remote 1 ap 0 ipsec receive spi 1000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text key1'],
        )
        assert 'Waiting' in _tunnel_status_line('mk-m1')

    def test_key_mismatch_stays_waiting(self):
        _setup_manual_pair(
            'mk-m3', 'mk-m4', '192.0.2.137', '192.0.2.138',
            cmds_a=['remote 1 ap 0 ipsec send spi 1000',
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text key1',
                   'remote 1 ap 0 ipsec receive spi 2000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text key2'],
            cmds_b=['remote 1 ap 0 ipsec send spi 2000',
                   'remote 1 ap 0 ipsec send protocol ah',
                   'remote 1 ap 0 ipsec send auth hmac-sha256 text WRONGKEY',
                   'remote 1 ap 0 ipsec receive spi 1000',
                   'remote 1 ap 0 ipsec receive protocol ah',
                   'remote 1 ap 0 ipsec receive auth hmac-sha256 text key1'],
        )
        assert 'Waiting' in _tunnel_status_line('mk-m3')

    def test_incomplete_config_stays_waiting(self):
        """protocol=espなのにencryptが未定義 → SA作成不可（マニュアル10.2.29の表）"""
        _dev('mk-m5')
        _run('mk-m5', [
            'configure', 'lan 1 ip address 192.0.2.141/30 3',
            'remote 1 ap 0 tunnel local 192.0.2.141',
            'remote 1 ap 0 tunnel remote 192.0.2.142',
            'remote 1 ap 0 ipsec type manual',
            'remote 1 ap 0 ipsec send spi 1000',
            'remote 1 ap 0 ipsec send protocol esp',  # encryptを設定しない
        ])
        assert 'Waiting' in _tunnel_status_line('mk-m5')

    def test_ike_and_manual_do_not_cross_talk(self):
        """IKE用の(ipsec type manualを打っていない)トンネルは、
        manual_send/manual_receiveが無いので手動鍵用の判定に一切かからず、
        従来どおりIKEのnegotiate_ipsec()だけで処理されること。"""
        _dev('mk-x1', 'sir')
        _dev('mk-x2', 'sir')
        _run('mk-x1', [
            'configure', 'lan 1 ip address 192.0.2.145/30 3',
            'remote 1 ap 0 tunnel local 192.0.2.145',
            'remote 1 ap 0 tunnel remote 192.0.2.146',
            'remote 1 ap 0 ipsec ike preshared-key testkey123',
        ])
        _run('mk-x2', [
            'configure', 'lan 1 ip address 192.0.2.146/30 3',
            'remote 1 ap 0 tunnel local 192.0.2.146',
            'remote 1 ap 0 tunnel remote 192.0.2.145',
            'remote 1 ap 0 ipsec ike preshared-key testkey123',
            'ike use on', 'ipsec use on',
        ])
        _run('mk-x1', ['ike use on', 'ipsec use on'])
        assert 'Established' in _tunnel_status_line('mk-x1')
        state = app_module.device_sessions['mk-x1']
        assert 'manual_send' not in state.ipsec_tunnels[1]
        assert state.ipsec_tunnels[1].get('ipsec_type') is None
