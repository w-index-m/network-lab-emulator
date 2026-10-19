"""
Juniper / Yamaha / 擬似FLET'S BAS / FortiGate / ASA threat-detection の
設定永続化テスト。

ユーザー依頼「Juniper/Yamaha/BAS/FortiGate/ASA threat-detectionも
永続化対応お願いします」への対応——`tests/test_bigip_alb_config_
persistence.py`で見つけた「`_save_config()`/`_load_config()`に
保存・復元ブロックが無いと再起動のたびに設定が消える」というギャップ
(`docs/automated-verification-patterns.md`のパターン3)を、このセッション
で追加された残りのdevice_typeにも横展開する。

`tests/test_nexus_tacacs_persistence.py`と同じ隔離パターン
(SAVED_CONFIG_PATH/device_sessionsをmonkeypatch)を使い、各device_typeに
ついて(1)辞書状態の保存・復元往復、(2)実ソケットを伴う機能
(Juniper SSH/NETCONF、ASA threat-detectionが依存する実SSHリスナー)は
復元後に実際に接続できることまで確認する。
"""

import json
import os
import socket
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from fastapi.testclient import TestClient
import app as app_module

client = TestClient(app_module.app)


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _wait_connect(addr, timeout=3.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection(addr, timeout=1) as s:
                return True
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    raise AssertionError(f'{addr} への実接続に失敗した: {last_err}')


class TestJuniperConfigAndSshPersistence:
    def test_junos_committed_candidate_and_ssh_survive_restart(self, tmp_path, monkeypatch):
        from engine.ssh_cli_agent import stop_ssh_cli_agent

        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'jnpr-persist-1'
        try:
            app_module.device_sessions[dev_id] = app_module.DeviceState('juniper', dev_id)
            _cli(dev_id, 'configure')
            _cli(dev_id, 'set interfaces ge-0/0/0 unit 0 family inet address 198.51.100.61/30')
            _cli(dev_id, 'set system services ssh')
            _cli(dev_id, 'set system host-name jnpr-persist-1')
            _cli(dev_id, 'commit')

            state_before = app_module.device_sessions[dev_id]
            assert 'system services ssh' in state_before.junos_committed
            assert state_before.ssh_rsa_key is True

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            dev = saved['devices'][dev_id]
            assert 'system services ssh' in dev['junos_committed']
            assert dev['juniper_ssh_rsa_key'] is True

            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            restored = app_module.device_sessions[dev_id]
            assert 'system services ssh' in restored.junos_committed
            assert restored.ssh_rsa_key is True

            # 辞書が戻っただけでは実ソケットは上がらない。_load_config()が
            # 実SSHリスナーを再起動していることを本物のTCP接続で確認する。
            _wait_connect(('198.51.100.61', 22))
        finally:
            stop_ssh_cli_agent(dev_id)

    def test_junos_candidate_survives_restart_uncommitted(self, tmp_path, monkeypatch):
        """実機のJunosもcommit前のcandidate configはセッションを跨いで
        残る(exitで破棄確認が出るのと同じ理由)ので、candidateも復元
        されることを固定する。"""
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'jnpr-persist-2'
        app_module.device_sessions[dev_id] = app_module.DeviceState('juniper', dev_id)
        _cli(dev_id, 'configure')
        _cli(dev_id, 'set interfaces ge-0/0/1 unit 0 family inet address 198.51.100.65/30')

        app_module._save_config()
        monkeypatch.setattr(app_module, 'device_sessions', {})
        app_module._load_config()

        restored = app_module.device_sessions[dev_id]
        assert any('ge-0/0/1' in l for l in restored.junos_candidate)
        # 未commitなのでcommitted/interfacesには反映されていないはず
        assert not any('ge-0/0/1' in l for l in restored.junos_committed)


class TestYamahaConfigPersistence:
    def test_nat_pppoe_ipv6_mape_survive_restart(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'rtx-persist-1'
        state = app_module.DeviceState('yamaha', dev_id)
        state.nat_descriptors = {'1000': {'type': 'masquerade', 'lan': 'lan2'}}
        state.yamaha_pp = {1: {'pppoe_lan': 'lan2', 'auth_user': 'u1',
                                'auth_pass': 'p1', 'enabled': True, 'connected': False}}
        state.yamaha_ipv6_prefix = 'dhcp-prefix@lan2::/64'
        state.map_e_rule = {'ipv4': '203.0.116.50', 'psid': 3,
                             'port_range': '1024-1535', 'br_ipv6': '2001:db8::1'}
        state.ipsec_tunnels = {1: {'local_ip': '203.0.113.9', 'remote_ip': '203.0.113.1',
                                    'preshared': 'psk', 'phase1': 'LARVAL', 'phase2': 'wait',
                                    'status': 'wait'}}
        state.ike_enabled = True
        state.ipsec_enabled = True
        app_module.device_sessions[dev_id] = state

        app_module._save_config()
        saved = json.loads(cfg_path.read_text(encoding='utf-8'))
        dev = saved['devices'][dev_id]
        assert dev['nat_descriptors'] == {'1000': {'type': 'masquerade', 'lan': 'lan2'}}
        assert dev['yamaha_pp']['1']['auth_user'] == 'u1'
        assert dev['yamaha_ipv6_prefix'] == 'dhcp-prefix@lan2::/64'
        assert dev['map_e_rule']['ipv4'] == '203.0.116.50'
        assert dev['ipsec_tunnels']['1']['local_ip'] == '203.0.113.9'
        assert dev['ike_enabled'] is True

        monkeypatch.setattr(app_module, 'device_sessions', {})
        app_module._load_config()

        restored = app_module.device_sessions[dev_id]
        assert restored.nat_descriptors == {'1000': {'type': 'masquerade', 'lan': 'lan2'}}
        # pp_id/tunnel_idはint型キーとして管理されるため、JSON往復後も
        # int型に戻っていることを固定する(str型のままだと、復元後に
        # 装置へ新たにint型キーで打ち込まれた際にsorted()がint/str比較
        # でTypeErrorになる実バグを踏んだ)
        assert restored.yamaha_pp[1]['auth_user'] == 'u1'
        assert '1' not in restored.yamaha_pp
        assert restored.yamaha_ipv6_prefix == 'dhcp-prefix@lan2::/64'
        assert restored.map_e_rule['ipv4'] == '203.0.116.50'
        assert restored.ipsec_tunnels[1]['local_ip'] == '203.0.113.9'
        assert '1' not in restored.ipsec_tunnels
        assert restored.ike_enabled is True
        assert restored.ipsec_enabled is True


class TestFortigateConfigPersistence:
    def test_pppoe_and_ipsec_survive_restart(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'fgt-persist-1'
        state = app_module.DeviceState('fortigate', dev_id)
        state.fortigate_pppoe = {'port1': {'username': 'u2', 'password': 'p2',
                                            'enabled': True, 'connected': False}}
        state.ipsec_tunnels = {'ph1': {'local_ip': '203.0.113.5', 'remote_ip': '203.0.113.6',
                                        'preshared': 'psk2', 'phase1': 'LARVAL',
                                        'phase2': 'wait', 'status': 'wait'}}
        state.ike_enabled = True
        state.ipsec_enabled = True
        app_module.device_sessions[dev_id] = state

        app_module._save_config()
        saved = json.loads(cfg_path.read_text(encoding='utf-8'))
        dev = saved['devices'][dev_id]
        assert dev['fortigate_pppoe']['port1']['username'] == 'u2'
        assert dev['ipsec_tunnels']['ph1']['remote_ip'] == '203.0.113.6'

        monkeypatch.setattr(app_module, 'device_sessions', {})
        app_module._load_config()

        restored = app_module.device_sessions[dev_id]
        assert restored.fortigate_pppoe['port1']['username'] == 'u2'
        assert restored.ipsec_tunnels['ph1']['remote_ip'] == '203.0.113.6'
        assert restored.ike_enabled is True


class TestBasConfigPersistence:
    def test_pppoe_pool_and_mape_server_survive_restart(self, tmp_path, monkeypatch):
        from engine.protocols import pppoe_engine, map_e_engine

        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'bas-persist-1'
        app_module.device_sessions[dev_id] = app_module.DeviceState('bas', dev_id)
        pppoe_engine.nodes.pop(dev_id, None)
        map_e_engine.nodes.pop(dev_id, None)
        try:
            pppoe_engine.set_pool(dev_id, '203.0.116.10', '203.0.116.20', 29)
            pppoe_engine.add_user(dev_id, 'rtxuser', 'rtxpass')
            map_e_engine.set_ipv4_pool(dev_id, '203.0.116.50', '203.0.116.51')
            map_e_engine.set_br_address(dev_id, '2001:db8::1')

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            dev = saved['devices'][dev_id]
            assert dev['pppoe_pool']['pool_start'] == '203.0.116.10'
            assert dev['pppoe_pool']['users'] == {'rtxuser': 'rtxpass'}
            assert dev['mape_server']['br_ipv6'] == '2001:db8::1'
            assert '203.0.116.50' in dev['mape_server']['ipv4_pool']

            # エンジン側(再起動を模してクリア)
            pppoe_engine.nodes.pop(dev_id, None)
            map_e_engine.nodes.pop(dev_id, None)
            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            bas = pppoe_engine.nodes[dev_id]
            assert bas['pool_start'] == '203.0.116.10'
            assert bas['users'] == {'rtxuser': 'rtxpass'}
            srv = map_e_engine.nodes[dev_id]
            assert srv['br_ipv6'] == '2001:db8::1'
            assert '203.0.116.50' in srv['ipv4_pool']
        finally:
            pppoe_engine.nodes.pop(dev_id, None)
            map_e_engine.nodes.pop(dev_id, None)


class TestAsaThreatDetectionPersistence:
    def test_threat_detection_config_and_ssh_listener_survive_restart(self, tmp_path, monkeypatch):
        from engine.ssh_cli_agent import stop_ssh_cli_agent

        cfg_path = tmp_path / 'saved_config.json'
        monkeypatch.setattr(app_module, 'SAVED_CONFIG_PATH', cfg_path)
        monkeypatch.setattr(app_module, 'device_sessions', {})

        dev_id = 'asa-persist-1'
        try:
            app_module.device_sessions[dev_id] = app_module.DeviceState('asa', dev_id)
            _cli(dev_id, 'configure terminal')
            _cli(dev_id, 'interface GigabitEthernet0/0')
            _cli(dev_id, 'ip address 198.51.100.71 255.255.255.252')
            _cli(dev_id, 'exit')
            _cli(dev_id, 'crypto key generate rsa modulus 1024')
            _cli(dev_id, 'threat-detection rate scanning-threat rate-interval 5 '
                         'average-rate 3 burst-rate 3')
            _cli(dev_id, 'threat-detection scanning-threat shun')

            state_before = app_module.device_sessions[dev_id]
            assert state_before.threat_detection['enabled'] is True
            assert state_before.ssh_rsa_key is True

            app_module._save_config()
            saved = json.loads(cfg_path.read_text(encoding='utf-8'))
            dev = saved['devices'][dev_id]
            assert dev['threat_detection']['enabled'] is True
            assert dev['ssh_rsa_key'] is True

            monkeypatch.setattr(app_module, 'device_sessions', {})
            app_module._load_config()

            restored = app_module.device_sessions[dev_id]
            assert restored.threat_detection['enabled'] is True
            assert restored.threat_detection['rate_interval'] == 5

            # 実SSHリスナーが復元後に再起動していること(threat-detectionの
            # shun判定は実SSHリスナーへの接続試行回数を見るため、リスナー
            # が上がっていないと機能全体が意味を持たない)。
            _wait_connect(('198.51.100.71', 22))
        finally:
            stop_ssh_cli_agent(dev_id)
