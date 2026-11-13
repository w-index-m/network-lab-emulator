"""
RADIUS(NetAttest EPS風サーバ)・RADIUSによる機器ログイン認証・
有線802.1X(Catalyst)・無線LAN(C9800 WLC)の回帰テスト。

ユーザー依頼「ネットワーク運用計画radiusなど運用系を伸ばしたい、もしくは
NetAttestとWi-Fi認証のようなツールを実装して欲しい」(範囲は段階1〜3を
選択)への対応。RADIUSは本物のUDPパケットで、テストもソケット越しに
実際に問い合わせて確認する(パケット形式そのものの互換性はFreeRADIUSの
radtest/radclientでライブ検証済み — docs/radius-netattest-8021x-wifi.md)。

各クラスは専用のIP帯(10.251.<n>.0/24)を使い、実ソケットのbindが他の
テストと衝突しないようにしている。
"""

import json
import os
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from fastapi.testclient import TestClient

import app as app_module
from engine import radius_server as rs

client = TestClient(app_module.app)


def _dev(id_, type_, hostname=None):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': hostname or id_})


def _del(id_):
    client.delete(f'/api/device/{id_}')


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    for c in cmds:
        out = _cli(id_, c)
        assert not (out or '').startswith('%'), f'{c!r} -> {out}'


def _netattest(dev_id, ip, clients, users):
    _dev(dev_id, 'netattest')
    cmds = ['configure', f'ip port1 address {ip}/24']
    cmds += [f'radius-client {c} secret {s}' for c, s in clients]
    cmds += [f'user {u}' for u in users]
    cmds += ['service radius', 'end']
    _run(dev_id, cmds)


def _nas_radius(dev_id, server_ip, secret, group='NAC'):
    return ['aaa new-model', 'radius server NA1',
            f'address ipv4 {server_ip} auth-port 1812 acct-port 1813',
            f'key {secret}', 'exit', f'aaa group server radius {group}',
            'server name NA1', 'exit']


# ── RADIUSパケット/サーバ単体 ──────────────────────────────────

class TestRadiusCodec:
    def test_user_password_hiding_roundtrip(self):
        auth = os.urandom(16)
        for pw in (b'a', b'exactly16bytes!!', b'a-password-longer-than-16-bytes'):
            hidden = rs.encrypt_password(pw, b'secret', auth)
            assert len(hidden) % 16 == 0 and hidden != pw
            assert rs.decrypt_password(hidden, b'secret', auth) == pw

    def test_long_eap_message_is_split_and_rejoined(self):
        payload = os.urandom(600)
        pkt = rs.build_packet(rs.ACCESS_REQUEST, 7, b'\x00' * 16, [(rs.EAP_MESSAGE, payload)])
        _code, ident, _auth, attrs = rs.parse_packet(pkt)
        assert ident == 7
        assert len([a for a in attrs if a[0] == rs.EAP_MESSAGE]) == 3
        assert rs.attr_join(attrs, rs.EAP_MESSAGE) == payload


class _FakeState:
    def __init__(self, ip, clients, users):
        self.interfaces = {'port1': {'ip': ip, 'prefix': 24, 'status': 'up'}}
        self.netattest = {'service_enabled': True, 'clients': clients, 'users': users}


@pytest.fixture
def radius_server():
    st = _FakeState('10.251.1.10', {'10.251.1.1': {'secret': 's3cret'}},
                    {'alice': {'password': 'pass1', 'vlan': '30', 'privilege': 15},
                     'off': {'password': 'x', 'disabled': True}})
    srv = rs.ensure_radius_server('t-radius-unit', st)
    assert srv is not None
    yield st
    rs.stop_radius_server('t-radius-unit')


class TestRadiusServer:
    @pytest.mark.parametrize('method', ['pap', 'chap', 'eap-md5'])
    def test_accept_carries_vlan_and_privilege(self, radius_server, method):
        r = rs.radius_authenticate('10.251.1.10', 's3cret', 'alice', 'pass1', method=method,
                                   source_ip='10.251.1.1')
        assert r.status == 'accept'
        assert r.vlan == '30'
        assert r.privilege == 15

    @pytest.mark.parametrize('method', ['pap', 'chap', 'eap-md5'])
    def test_wrong_password_is_rejected(self, radius_server, method):
        r = rs.radius_authenticate('10.251.1.10', 's3cret', 'alice', 'bad', method=method,
                                   source_ip='10.251.1.1')
        assert r.status == 'reject'

    def test_disabled_and_unknown_users_are_rejected_and_logged(self, radius_server):
        assert rs.radius_authenticate('10.251.1.10', 's3cret', 'off', 'x',
                                      source_ip='10.251.1.1').status == 'reject'
        assert rs.radius_authenticate('10.251.1.10', 's3cret', 'nobody', 'x',
                                      source_ip='10.251.1.1').status == 'reject'
        reasons = [e['reason'] for e in radius_server.netattest['auth_log']]
        assert 'user disabled' in reasons and 'user not found' in reasons

    def test_unknown_client_is_silently_dropped(self, radius_server):
        # 実機同様、未登録の送信元(NAS)からの要求には応答しない
        r = rs.radius_authenticate('10.251.1.10', 's3cret', 'alice', 'pass1',
                                   source_ip='10.251.1.99', timeout=0.5, retries=0)
        assert r.status == 'timeout'

    def test_wrong_shared_secret_is_detected_by_the_client(self, radius_server):
        r = rs.radius_authenticate('10.251.1.10', 'wrong', 'alice', 'pass1',
                                   source_ip='10.251.1.1')
        assert r.status == 'error' and 'authenticator' in r.detail

    def test_accounting_start_stop_is_recorded(self, radius_server):
        assert rs.radius_accounting('10.251.1.10', 's3cret', rs.ACCT_START, 'alice', 'sid1',
                                    source_ip='10.251.1.1') == 'ok'
        assert rs.radius_accounting('10.251.1.10', 's3cret', rs.ACCT_STOP, 'alice', 'sid1',
                                    source_ip='10.251.1.1', session_time=42) == 'ok'
        recs = radius_server.netattest['acct_log']
        assert [r['status'] for r in recs] == ['Start', 'Stop']
        assert recs[-1]['session_time'] == 42


# ── NetAttest装置のCLI ─────────────────────────────────────────

class TestNetAttestCli:
    def test_config_show_and_service_lifecycle(self):
        dev = 't-na-cli'
        _dev(dev, 'netattest', 'eps-cli')
        try:
            _run(dev, ['configure', 'ip port1 address 10.251.2.10/24',
                       'radius-client 10.251.2.1 secret topsecret name sw',
                       'user alice password pass1 vlan 30 group staff privilege 15',
                       'user bob password bobpw', 'user bob disable', 'end'])
            assert 'not running' in _cli(dev, 'show radius status')
            users = _cli(dev, 'show users')
            assert 'alice' in users and 'staff' in users and 'pass1' not in users
            assert 'disabled' in users
            assert 'topsecret' not in _cli(dev, 'show radius clients')
            _run(dev, ['configure', 'service radius', 'end'])
            st = _cli(dev, 'show radius status')
            assert 'enabled' in st and '10.251.2.10:1812' in st
            rc = _cli(dev, 'show running-config')
            for line in ('radius-client 10.251.2.1 secret topsecret name sw',
                         'user alice password pass1 vlan 30 group staff privilege 15',
                         'user bob disable', 'service radius'):
                assert line in rc
            _run(dev, ['configure', 'no service radius', 'end'])
            assert rs.get_radius_server(dev) is None
        finally:
            _del(dev)

    def test_invalid_vlan_is_rejected(self):
        dev = 't-na-badvlan'
        _dev(dev, 'netattest')
        try:
            _cli(dev, 'configure')
            assert 'Invalid VLAN' in _cli(dev, 'user eve password x vlan 5000')
            assert 'eve' not in _cli(dev, 'show users')
        finally:
            _del(dev)


# ── 段階1: RADIUSによるSSHログイン認証 ───────────────────────────

class TestRadiusLogin:
    @pytest.fixture
    def lab(self):
        paramiko = pytest.importorskip('paramiko')
        _netattest('t-na-login', '10.251.3.10', [('10.251.3.21', 'k1')],
                   ['alice password pass1 privilege 15', 'bob password bobpw'])
        _dev('t-sw-login', 'catalyst', 'SW-LOGIN')
        _run('t-sw-login', ['configure terminal', 'interface Vlan10',
                            'ip address 10.251.3.21 255.255.255.0', 'exit']
             + _nas_radius('t-sw-login', '10.251.3.10', 'k1')
             + ['aaa authentication login default group NAC local',
                'username admin privilege 15 secret localpw',
                'crypto key generate rsa modulus 2048', 'end'])
        time.sleep(0.3)
        yield paramiko
        _del('t-sw-login')
        _del('t-na-login')

    @staticmethod
    def _ssh(paramiko, user, pw):
        cl = paramiko.SSHClient()
        cl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cl.connect('10.251.3.21', 22, username=user, password=pw, timeout=8,
                       look_for_keys=False, allow_agent=False)
            ch = cl.invoke_shell()
            time.sleep(0.6)
            return ch.recv(4000).decode(errors='replace').strip().splitlines()[-1]
        except paramiko.AuthenticationException:
            return None
        finally:
            cl.close()

    def test_privilege_comes_from_radius(self, lab):
        assert self._ssh(lab, 'alice', 'pass1').endswith('#')
        assert self._ssh(lab, 'bob', 'bobpw').endswith('>')

    def test_reject_does_not_fall_back_to_local(self, lab):
        # IOSのメソッドリスト仕様: サーバのRejectで確定し、localには進まない
        assert self._ssh(lab, 'alice', 'wrong') is None
        assert self._ssh(lab, 'admin', 'localpw') is None

    def test_unreachable_server_falls_back_to_local(self, lab):
        _run('t-na-login', ['configure', 'no service radius', 'end'])
        assert self._ssh(lab, 'admin', 'localpw').endswith('#')
        assert self._ssh(lab, 'alice', 'pass1') is None

    def test_test_aaa_and_show_aaa_servers(self, lab):
        ok = _cli('t-sw-login', 'test aaa group NAC alice pass1 legacy')
        assert 'User was successfully authenticated.' in ok
        ng = _cli('t-sw-login', 'test aaa group NAC alice nope legacy')
        assert 'rejected by server' in ng
        stats = _cli('t-sw-login', 'show aaa servers')
        assert 'host 10.251.3.10' in stats and 'accept 1, reject 1' in stats
        rc = _cli('t-sw-login', 'show running-config')
        for line in ('radius server NA1', ' address ipv4 10.251.3.10 auth-port 1812 acct-port 1813',
                     ' key k1', 'aaa group server radius NAC', ' server name NA1',
                     'aaa authentication login default group NAC local'):
            assert line in rc


class TestNexusRadiusConfig:
    def test_legacy_radius_server_host_and_test_aaa(self):
        _netattest('t-na-nx', '10.251.4.10', [('10.251.4.5', 'nxkey')], ['alice password pass1'])
        _dev('t-nx', 'nexus')
        try:
            _run('t-nx', ['configure terminal', 'interface mgmt0',
                          'ip address 10.251.4.5 255.255.255.0', 'exit',
                          'radius-server host 10.251.4.10 key nxkey',
                          'aaa group server radius RAD', 'server 10.251.4.10', 'exit', 'end'])
            assert _cli('t-nx', 'test aaa group RAD alice pass1') == 'user has been authenticated'
            assert _cli('t-nx', 'test aaa group RAD alice bad') == 'user has failed authentication'
            rc = _cli('t-nx', 'show running-config')
            assert 'radius-server host 10.251.4.10 key 7 "nxkey"' in rc
            assert 'aaa group server radius RAD' in rc
        finally:
            _del('t-nx')
            _del('t-na-nx')


# ── 段階2: 有線802.1X ──────────────────────────────────────────

@pytest.fixture
def dot1x_lab():
    _netattest('t-na-dot1x', '10.251.5.10', [('10.251.5.21', 'dk')],
               ['alice password pass1 vlan 30', 'bob password bobpw'])
    _dev('t-sw-dot1x', 'catalyst')
    _dev('t-pc-dot1x', 'pc')
    client.post('/api/link', json={'a': 't-pc-dot1x', 'b': 't-sw-dot1x', 'iface_a': 'eth0',
                                   'iface_b': 'GigabitEthernet1/0/5'})
    _run('t-sw-dot1x', ['configure terminal', 'interface Vlan10',
                        'ip address 10.251.5.21 255.255.255.0', 'exit']
         + _nas_radius('t-sw-dot1x', '10.251.5.10', 'dk')
         + ['aaa authentication dot1x default group NAC', 'aaa authorization network default group NAC',
            'aaa accounting dot1x default start-stop group NAC', 'dot1x system-auth-control',
            'interface GigabitEthernet1/0/5', 'switchport mode access', 'switchport access vlan 10',
            'authentication port-control auto', 'dot1x pae authenticator', 'no shutdown', 'end'])
    _cli('t-pc-dot1x', 'ip addr add 10.251.5.50/24 dev eth0')
    yield
    for d in ('t-pc-dot1x', 't-sw-dot1x', 't-na-dot1x'):
        _del(d)


def _eth_profile(user, pw, eap='md5'):
    _cli('t-pc-dot1x', 'nmcli connection delete corp')
    return _cli('t-pc-dot1x', f'nmcli connection add type ethernet con-name corp ifname eth0 '
                              f'802-1x.eap {eap} 802-1x.identity {user} 802-1x.password {pw}')


class TestWired8021x:
    def test_unauthorized_port_blocks_traffic(self, dot1x_lab):
        assert '100% packet loss' in _cli('t-pc-dot1x', 'ping -c 1 10.251.5.21')

    def test_wrong_password_leaves_port_unauthorized(self, dot1x_lab):
        _eth_profile('bob', 'wrong')
        assert '802.1X supplicant failed' in _cli('t-pc-dot1x', 'nmcli connection up corp')
        assert 'Unauth' in _cli('t-sw-dot1x', 'show access-session')
        assert '100% packet loss' in _cli('t-pc-dot1x', 'ping -c 1 10.251.5.21')

    def test_success_assigns_vlan_opens_port_and_accounts(self, dot1x_lab):
        assert 'successfully added' in _eth_profile('alice', 'pass1', eap='peap')
        assert 'successfully activated' in _cli('t-pc-dot1x', 'nmcli connection up corp')
        detail = _cli('t-sw-dot1x', 'show access-session interface Gi1/0/5 details')
        assert 'User-Name:  alice' in detail and 'Status:  Authorized' in detail
        assert 'Vlan: 30' in detail
        assert 'emulated: no TLS tunnel' in detail       # PEAPの実装範囲を正直に表示
        assert ' 30 ' in [l for l in _cli('t-sw-dot1x', 'show interfaces status').splitlines()
                          if l.startswith('Gi1/0/5')][0]
        assert '0% packet loss' in _cli('t-pc-dot1x', 'ping -c 1 10.251.5.21')

        _cli('t-pc-dot1x', 'nmcli connection down corp')
        assert 'No sessions' in _cli('t-sw-dot1x', 'show access-session')
        assert ' 10 ' in [l for l in _cli('t-sw-dot1x', 'show interfaces status').splitlines()
                          if l.startswith('Gi1/0/5')][0]
        assert '100% packet loss' in _cli('t-pc-dot1x', 'ping -c 1 10.251.5.21')
        acct = _cli('t-na-dot1x', 'show radius accounting')
        assert 'Start' in acct and 'Stop' in acct and 'GigabitEthernet1/0/5' in acct

    def test_vlan_not_applied_without_network_authorization(self, dot1x_lab):
        _run('t-sw-dot1x', ['configure terminal', 'no aaa authorization network default group NAC', 'end'])
        _eth_profile('alice', 'pass1')
        assert 'successfully activated' in _cli('t-pc-dot1x', 'nmcli connection up corp')
        assert 'Vlan:' not in _cli('t-sw-dot1x', 'show access-session interface Gi1/0/5 details')

    def test_port_without_8021x_times_out_the_supplicant(self, dot1x_lab):
        _run('t-sw-dot1x', ['configure terminal', 'interface GigabitEthernet1/0/5',
                            'no authentication port-control auto', 'end'])
        _eth_profile('alice', 'pass1')
        assert 'took too long' in _cli('t-pc-dot1x', 'nmcli connection up corp')

    def test_running_config(self, dot1x_lab):
        rc = _cli('t-sw-dot1x', 'show running-config')
        for line in ('aaa authentication dot1x default group NAC',
                     'aaa authorization network default group NAC',
                     'aaa accounting dot1x default start-stop group NAC',
                     'dot1x system-auth-control', ' authentication port-control auto',
                     ' dot1x pae authenticator'):
            assert line in rc


# ── 段階3: 無線LAN(C9800 WLC) ─────────────────────────────────

@pytest.fixture
def wifi_lab():
    _netattest('t-na-wifi', '10.251.6.10', [('10.251.6.30', 'wk')], ['alice password pass1 vlan 30'])
    _dev('t-wlc', 'wlc', 'WLC-T')
    _dev('t-pc-wifi', 'pc')
    client.post('/api/link', json={'a': 't-pc-wifi', 'b': 't-wlc', 'iface_a': 'wlan0',
                                   'iface_b': 'GigabitEthernet2'})
    _run('t-wlc', ['configure terminal', 'interface GigabitEthernet1',
                   'ip address 10.251.6.30 255.255.255.0', 'exit']
         + _nas_radius('t-wlc', '10.251.6.10', 'wk')
         + ['aaa authentication dot1x CORP-AUTH group NAC',
            'aaa accounting identity default start-stop group NAC',
            'wireless profile policy CORP-POL', 'aaa-override', 'vlan 100', 'no shutdown', 'exit',
            'wireless tag policy default-policy-tag', 'wlan CORP policy CORP-POL', 'exit',
            'wlan CORP 1 CORP-SSID', 'security dot1x authentication-list CORP-AUTH', 'no shutdown', 'exit',
            'wlan GUEST 2 GUEST-SSID', 'no security wpa akm dot1x', 'security wpa akm psk',
            'security wpa psk set-key ascii 0 guestpass123', 'no shutdown', 'exit', 'end'])
    yield
    for d in ('t-pc-wifi', 't-wlc', 't-na-wifi'):
        _del(d)


def _corp_profile(pw):
    _cli('t-pc-wifi', 'nmcli connection delete corp')
    _cli('t-pc-wifi', 'nmcli connection add type wifi con-name corp ifname wlan0 ssid CORP-SSID '
                      'wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 '
                      f'802-1x.identity alice 802-1x.password {pw}')


class TestWifi:
    def test_wlan_summary_and_scan(self, wifi_lab):
        summ = _cli('t-wlc', 'show wlan summary')
        assert 'CORP-SSID' in summ and '[WPA2][802.1x][AES]' in summ
        assert 'GUEST-SSID' in summ and '[WPA2][PSK][AES]' in summ
        scan = _cli('t-pc-wifi', 'nmcli device wifi list')
        assert 'CORP-SSID' in scan and 'WPA2 802.1X' in scan and 'GUEST-SSID' in scan

    def test_shutdown_wlan_is_not_visible(self, wifi_lab):
        _run('t-wlc', ['configure terminal', 'wlan GUEST', 'shutdown', 'end'])
        assert 'GUEST-SSID' not in _cli('t-pc-wifi', 'nmcli device wifi list')

    def test_enterprise_success_applies_aaa_override_vlan(self, wifi_lab):
        _corp_profile('pass1')
        assert 'successfully activated' in _cli('t-pc-wifi', 'nmcli connection up corp')
        summ = _cli('t-wlc', 'show wireless client summary')
        assert 'Run' in summ and 'Dot1x' in summ
        mac = summ.splitlines()[-1].split()[0]
        detail = _cli('t-wlc', f'show wireless client mac-address {mac} detail')
        assert 'Client Username : alice' in detail
        assert 'VLAN : 30' in detail and 'AAA Override Passed : Yes' in detail
        assert 'Start' in _cli('t-na-wifi', 'show radius accounting')

    def test_enterprise_wrong_password(self, wifi_lab):
        _corp_profile('nope')
        assert '802.1X supplicant failed' in _cli('t-pc-wifi', 'nmcli connection up corp')
        assert 'Number of Clients: 0' in _cli('t-wlc', 'show wireless client summary')

    def test_psk(self, wifi_lab):
        assert 'Secrets were required' in _cli('t-pc-wifi', 'nmcli device wifi connect GUEST-SSID password bad')
        assert 'successfully activated' in _cli('t-pc-wifi',
                                                'nmcli device wifi connect GUEST-SSID password guestpass123')
        assert 'PSK' in _cli('t-wlc', 'show wireless client summary')

    def test_unknown_ssid(self, wifi_lab):
        assert "No network with SSID 'NOPE' found." in _cli('t-pc-wifi',
                                                             'nmcli device wifi connect NOPE password x')
        assert 'NOPE' not in _cli('t-pc-wifi', 'nmcli connection show')

    def test_running_config(self, wifi_lab):
        rc = _cli('t-wlc', 'show running-config')
        for line in ('wlan CORP 1 CORP-SSID', ' security dot1x authentication-list CORP-AUTH',
                     'wlan GUEST 2 GUEST-SSID', ' security wpa psk set-key ascii 0 guestpass123',
                     'wireless profile policy CORP-POL', ' aaa-override', ' vlan 100',
                     'wireless tag policy default-policy-tag', ' wlan CORP policy CORP-POL',
                     'aaa authentication dot1x CORP-AUTH group NAC',
                     'aaa accounting identity default start-stop group NAC'):
            assert line in rc


class TestPcPingIsTopologyAware:
    def test_unknown_destination_is_unreachable(self):
        """PCのpingは以前、宛先に関係なく常に0% lossを返していた(実バグ)。"""
        _dev('t-pc-ping', 'pc')
        try:
            out = _cli('t-pc-ping', 'ping -c 2 10.251.99.99')
            assert 'Destination Host Unreachable' in out and '100% packet loss' in out
        finally:
            _del('t-pc-ping')


# ── 永続化 ────────────────────────────────────────────────────

class TestPersistence:
    def test_radius_dot1x_wifi_config_survive_restart(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})
        try:
            _netattest('t-na-p', '10.251.7.10', [('10.251.7.21', 'pk')], ['alice password pass1 vlan 30'])
            _dev('t-sw-p', 'catalyst')
            _run('t-sw-p', ['configure terminal', 'interface Vlan10',
                            'ip address 10.251.7.21 255.255.255.0', 'exit']
                 + _nas_radius('t-sw-p', '10.251.7.10', 'pk')
                 + ['aaa authentication login default group NAC local',
                    'aaa authentication dot1x default group NAC', 'dot1x system-auth-control',
                    'interface GigabitEthernet1/0/7', 'authentication port-control auto',
                    'dot1x pae authenticator', 'end'])
            _dev('t-wlc-p', 'wlc')
            _run('t-wlc-p', ['configure terminal', 'wlan CORP 1 CORP-SSID', 'no shutdown', 'end'])
            _dev('t-pc-p', 'pc')
            _cli('t-pc-p', 'nmcli connection add type wifi con-name corp ifname wlan0 ssid CORP-SSID '
                           'wifi-sec.key-mgmt wpa-eap 802-1x.eap md5 802-1x.identity alice 802-1x.password pass1')

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))['devices']
            assert 'auth_log' not in saved['t-na-p']['netattest']
            assert 'sessions' not in saved['t-sw-p']['dot1x']

            rs.stop_radius_server('t-na-p')
            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            # RADIUSサーバは実ソケットごと再起動している
            assert rs.get_radius_server('t-na-p') is not None
            sw = app_module.device_sessions['t-sw-p']
            assert sw.aaa_new_model and 'NA1' in sw.radius_servers
            assert sw.dot1x['ports']['GigabitEthernet1/0/7']['port_control'] == 'auto'
            from engine.protocols import vnet
            assert 'GigabitEthernet1/0/7' in vnet.down_interfaces.get('t-sw-p', set())
            res = __import__('engine.aaa_radius', fromlist=['x']).authenticate(sw, 'NAC', 'alice', 'pass1')
            assert res.accepted
            assert 'CORP' in app_module.device_sessions['t-wlc-p'].wireless['wlans']
            pc = app_module.device_sessions['t-pc-p']
            assert 'corp' in pc.nm['connections'] and 'wlan0' in pc.interfaces
        finally:
            rs.stop_radius_server('t-na-p')
            from engine.protocols import vnet
            vnet.down_interfaces.pop('t-sw-p', None)
