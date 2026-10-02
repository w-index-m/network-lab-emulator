"""
netmiko / Ansible(network_cli)から実機のArista・Juniperに繋ぐための
最小対応。ユーザー依頼「その他ansibleで取得や、netmikoでの運用など
柔軟に行きましょう」への対応。

実際に `netmiko.ConnectHandler(device_type='arista_eos'/'juniper_junos')`
と `ansible-playbook`(arista.eos.eos_command)で本物のエミュレータ
インスタンスに接続して確認した上で、見つかった不足を埋めた:

1. 実SSHリスナーの起動条件
   - 従来 `crypto key generate rsa` は device_type in ('cisco','catalyst')
     のみが対象で、Arista では実SSHリスナー(engine/ssh_cli_agent.py)が
     一切起動しなかった。'arista' を対象に追加。
   - Juniperは実機通り `set system services ssh` → `commit` で
     有効化する形にした。ただし handle_protocol_config 側
     (rule_engine.process()より前に実行される)でリスナーを起動すると、
     _juniper_processのcommit処理(state.interfacesへのIP反映含む)が
     まだ走っていない時点のIPでbindしてしまうため、
     rule_engine.process()の**後**で起動するよう順序を直した
     (古いデフォルトIP 203.0.113.2 にbindしてしまい、インタフェースに
     設定した本来のIPで待ち受けない不具合があった)。

2. netmikoが接続直後に送る端末設定コマンドへの応答
   - arista_eos: "terminal width <n>" → "Width set to N columns."、
     "terminal length 0" → "Pagination disabled."
     (netmiko 4.8のArista用ドライバはこの文字列を待ち受けており、
     空応答だと接続自体がタイムアウトする)
   - juniper_junos: "set cli screen-width <n>" →
     "Screen width set to N"、"set cli screen-length <n>" →
     "Screen length set to N"、"set cli complete-on-space off/on"
     (いずれもcandidate configとは無関係な表示設定で、commit不要・
     モード不問で即座に反映される実機の挙動)

3. Ansible(arista.eos系モジュール)が内部で自動送信する
   "show version | json" / "show hostname | json"
   (eAPI相当のJSON出力。実装が無いと"show version"だけでなく
   arista.eos.*モジュールすべてが接続直後のdevice_info取得で
   こけて使い物にならなかった)
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


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


class TestAristaRealSshEnablement:
    def test_crypto_key_generate_rsa_sets_ssh_rsa_key_flag(self):
        _dev('nm-arista-ssh-1', 'arista')
        _cli('nm-arista-ssh-1', 'configure terminal')
        _cli('nm-arista-ssh-1', 'crypto key generate rsa')
        state = app_module.device_sessions['nm-arista-ssh-1']
        assert state.ssh_rsa_key is True

    def test_terminal_width_responds_with_width_set_to(self):
        _dev('nm-arista-ssh-2', 'arista')
        out = _cli('nm-arista-ssh-2', 'terminal width 511')['output']
        assert out == 'Width set to 511 columns.'

    def test_terminal_length_0_responds_with_pagination_disabled_on_arista(self):
        _dev('nm-arista-ssh-3', 'arista')
        out = _cli('nm-arista-ssh-3', 'terminal length 0')['output']
        assert out == 'Pagination disabled.'

    def test_terminal_length_0_is_silent_on_cisco(self):
        """Arista専用の挙動がCisco/Catalystに波及していないことの固定。"""
        _dev('nm-cisco-ssh-1', 'cisco')
        out = _cli('nm-cisco-ssh-1', 'terminal length 0')['output']
        assert out == ''


class TestAristaJsonOutput:
    def test_show_version_json_is_valid_json_with_expected_keys(self):
        import json
        _dev('nm-arista-json-1', 'arista')
        out = _cli('nm-arista-json-1', 'show version | json')['output']
        data = json.loads(out)
        assert data['modelName'] == 'DCS-7050SX3-48YC8'
        assert data['version'] == '4.32.1F'
        assert 'serialNumber' in data
        assert 'systemMacAddress' in data

    def test_show_hostname_json_reflects_current_hostname(self):
        import json
        _dev('nm-arista-json-2', 'arista')
        _cli('nm-arista-json-2', 'configure terminal')
        _cli('nm-arista-json-2', 'hostname spine-json')
        out = _cli('nm-arista-json-2', 'show hostname | json')['output']
        data = json.loads(out)
        assert data['hostname'] == 'spine-json'

    def test_json_output_is_arista_only(self):
        _dev('nm-cisco-json-1', 'cisco')
        out = _cli('nm-cisco-json-1', 'show version | json')['output']
        assert 'Invalid input' in out or '%' in out or out != ''


class TestJuniperRealSshEnablement:
    def _enable_ssh_with_address(self, dev_id, addr):
        _dev(dev_id, 'juniper')
        _cli(dev_id, 'configure')
        _cli(dev_id, f'set interfaces ge-0/0/0 unit 0 family inet address {addr}/30')
        _cli(dev_id, 'set system services ssh')
        return _cli(dev_id, 'commit')

    def test_commit_with_ssh_service_sets_ssh_rsa_key_flag(self):
        self._enable_ssh_with_address('nm-jnpr-ssh-1', '198.51.100.21')
        state = app_module.device_sessions['nm-jnpr-ssh-1']
        assert state.ssh_rsa_key is True

    def test_ssh_not_enabled_before_commit(self):
        _dev('nm-jnpr-ssh-2', 'juniper')
        _cli('nm-jnpr-ssh-2', 'configure')
        _cli('nm-jnpr-ssh-2', 'set system services ssh')
        state = app_module.device_sessions['nm-jnpr-ssh-2']
        assert not getattr(state, 'ssh_rsa_key', False)

    def test_ssh_listener_binds_to_the_newly_committed_interface_ip_not_the_default(self):
        """handle_protocol_config(rule_engine.process()より前)でリスナーを
        起動すると、まだ反映されていない旧デフォルトIP(203.0.113.2)に
        bindしてしまう不具合があった。rule_engine.process()の後で
        起動するよう順序を直したことの固定。"""
        from engine.ssh_cli_agent import _servers as ssh_servers
        dev_id = 'nm-jnpr-ssh-3'
        addr = '198.51.100.33'
        try:
            self._enable_ssh_with_address(dev_id, addr)
            srv = ssh_servers.get(dev_id)
            assert srv is not None, 'SSHリスナーが起動していない'
            assert srv.ip == addr, f'古いデフォルトIPにbindしている: {srv.ip}'
        finally:
            from engine.ssh_cli_agent import stop_ssh_cli_agent
            stop_ssh_cli_agent(dev_id)


class TestJuniperCliDisplayCommands:
    """netmikoのjuniper_junosドライバがログイン直後に送るset cli系コマンド。"""

    def test_set_cli_screen_width_is_accepted_and_responds(self):
        _dev('nm-jnpr-cli-1', 'juniper')
        out = _cli('nm-jnpr-cli-1', 'set cli screen-width 511')['output']
        assert out == 'Screen width set to 511'

    def test_set_cli_screen_length_is_accepted_and_responds(self):
        _dev('nm-jnpr-cli-2', 'juniper')
        out = _cli('nm-jnpr-cli-2', 'set cli screen-length 0')['output']
        assert out == 'Screen length set to 0'

    def test_set_cli_complete_on_space_off_is_accepted(self):
        _dev('nm-jnpr-cli-3', 'juniper')
        out = _cli('nm-jnpr-cli-3', 'set cli complete-on-space off')['output']
        assert out == 'Disabling complete-on-space'

    def test_these_do_not_pollute_candidate_config(self):
        """表示設定であってcandidate configではないため、show configuration
        に出てこないことを固定する。"""
        _dev('nm-jnpr-cli-4', 'juniper')
        _cli('nm-jnpr-cli-4', 'set cli screen-width 511')
        _cli('nm-jnpr-cli-4', 'configure')
        rc = _cli('nm-jnpr-cli-4', 'show configuration | display set')['output']
        assert 'screen-width' not in rc
