"""
「設定生成」タブ拡張: LLM→Ansible Playbook生成(`POST /api/generate_playbook`)。

ユーザー依頼「LLMを使っているのでLLMとansibleのうまい組み合わせ
ありますか？」→「これも試験に反映お願いします」と明示された4パターン:
  - 構成投入系(push): arista.eos.eos_config / cisco.ios.ios_config /
    junipernetworks.junos.junos_configのlinesで設定変更を投入
  - 事実取得・差分検知系(facts): *_facts + ansible.builtin.assertで
    現在の状態と期待値の差分を検知
  - 検証系(verify): *_commandでshowコマンドを実行しregister/debug
  - ロールバック系(rollback): backup: yesで変更を適用するplayと、
    バックアップから復元するplayの2play構成

このセッションで実際にnetmiko/ansibleの実SSH/NETCONF接続を確認済みの
device_type(arista/juniper/cisco/catalyst)のみ対応する。

実`ansible-playbook`の実行はこのエンドポイントの責務外——
netmiko/ansibleの既存テスト(tests/test_netmiko_ansible_compat.py等)と
同じ方針で、生成されたPlaybook YAMLの構造(正しいモジュール名/
接続方式/妥当なYAML)を検証するに留め、実行自体は手動/オプションの
ライブ検証とする。LLM呼び出しはtests/test_generate_config_endpoint.py
と同じパターンでmonkeypatchしてモックする(実際のGroq/Mistral/Ollama
APIへの疎通はテスト対象外)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_, hostname=None):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': hostname or id_})


def _generate(device_id, pattern, instruction=''):
    body = {'device_id': device_id, 'pattern': pattern, 'instruction': instruction}
    return client.post('/api/generate_playbook', json=body).json()


@pytest.fixture
def mock_llm(monkeypatch):
    """次に呼ばれるquery_llmの戻り値をセットするためのヘルパーを返す。"""
    holder = {'response': None}

    async def fake_query_llm(prompt, system):
        return holder['response']

    monkeypatch.setattr(app_module, 'query_llm', fake_query_llm)
    old_backend = app_module.LLM_BACKEND
    app_module.LLM_BACKEND = 'groq'
    yield holder
    app_module.LLM_BACKEND = old_backend


_PUSH_YAML = """
- hosts: all
  gather_facts: no
  vars:
    ansible_connection: network_cli
    ansible_network_os: eos
  tasks:
    - name: push config
      arista.eos.eos_config:
        lines:
          - interface Ethernet1
          - description test
"""

_FACTS_YAML = """
- hosts: all
  gather_facts: no
  vars:
    ansible_connection: network_cli
    ansible_network_os: eos
  tasks:
    - name: gather facts
      arista.eos.eos_facts:
      register: facts_result
    - name: detect diff
      ansible.builtin.assert:
        that:
          - facts_result.ansible_facts.ansible_net_hostname == "expected-host"
"""

_VERIFY_YAML = """
- hosts: all
  gather_facts: no
  vars:
    ansible_connection: network_cli
    ansible_network_os: eos
  tasks:
    - name: run show command
      arista.eos.eos_command:
        commands:
          - show interfaces Ethernet1
      register: cmd_result
    - name: show result
      ansible.builtin.debug:
        var: cmd_result.stdout
"""

_ROLLBACK_YAML = """
- hosts: all
  gather_facts: no
  vars:
    ansible_connection: network_cli
    ansible_network_os: eos
  tasks:
    - name: apply config with backup
      arista.eos.eos_config:
        lines:
          - interface Ethernet1
          - description changed
        backup: yes
      register: backup_result
- hosts: all
  gather_facts: no
  vars:
    ansible_connection: network_cli
    ansible_network_os: eos
  tasks:
    - name: rollback from backup
      arista.eos.eos_config:
        src: "{{ backup_result.backup_path }}"
        replace: config
"""


class TestGeneratePlaybookPatterns:
    def test_push_pattern_generates_config_module_playbook(self, mock_llm):
        _dev('pb-push-1', 'arista')
        mock_llm['response'] = _PUSH_YAML
        d = _generate('pb-push-1', 'push', 'Ethernet1にdescriptionを設定して')
        assert d['ok'] is True
        assert d['pattern'] == 'push'
        assert d['ansible_collection'] == 'arista.eos'
        assert 'eos_config' in d['playbook_yaml']

    def test_facts_pattern_generates_facts_and_assert(self, mock_llm):
        _dev('pb-facts-1', 'arista')
        mock_llm['response'] = _FACTS_YAML
        d = _generate('pb-facts-1', 'facts', 'ホスト名がexpected-hostであることを確認して')
        assert d['ok'] is True
        assert 'eos_facts' in d['playbook_yaml']
        assert 'assert' in d['playbook_yaml']

    def test_verify_pattern_generates_command_and_debug(self, mock_llm):
        _dev('pb-verify-1', 'arista')
        mock_llm['response'] = _VERIFY_YAML
        d = _generate('pb-verify-1', 'verify', 'Ethernet1の状態を確認して')
        assert d['ok'] is True
        assert 'eos_command' in d['playbook_yaml']
        assert 'debug' in d['playbook_yaml']

    def test_rollback_pattern_generates_backup_and_restore_plays(self, mock_llm):
        _dev('pb-rollback-1', 'arista')
        mock_llm['response'] = _ROLLBACK_YAML
        d = _generate('pb-rollback-1', 'rollback', 'Ethernet1の設定変更とロールバック手順')
        assert d['ok'] is True
        parsed_plays = d['playbook_yaml'].count('- hosts:')
        assert parsed_plays == 2, '適用play + ロールバックplayの2つ'
        assert 'backup' in d['playbook_yaml']


class TestGeneratePlaybookDeviceTypes:
    def test_juniper_uses_netconf_connection_and_junos_collection(self, mock_llm):
        _dev('pb-juniper-1', 'juniper')
        mock_llm['response'] = (
            "- hosts: all\n  vars:\n    ansible_connection: netconf\n"
            "    ansible_network_os: junos\n  tasks:\n"
            "    - name: push\n      junipernetworks.junos.junos_config:\n"
            "        lines:\n          - set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24\n"
        )
        d = _generate('pb-juniper-1', 'push', 'ge-0/0/0にIPを設定')
        assert d['ok'] is True
        assert d['ansible_collection'] == 'junipernetworks.junos'
        assert d['ansible_connection'] == 'netconf'

    def test_cisco_and_catalyst_use_ios_collection(self, mock_llm):
        _dev('pb-cisco-1', 'cisco')
        _dev('pb-catalyst-1', 'catalyst')
        mock_llm['response'] = (
            "- hosts: all\n  tasks:\n    - name: x\n      cisco.ios.ios_config:\n"
            "        lines:\n          - hostname test\n"
        )
        d1 = _generate('pb-cisco-1', 'push', 'hostnameを設定')
        d2 = _generate('pb-catalyst-1', 'push', 'hostnameを設定')
        assert d1['ansible_collection'] == 'cisco.ios'
        assert d2['ansible_collection'] == 'cisco.ios'

    def test_unsupported_device_type_reports_error(self, mock_llm):
        _dev('pb-sir-1', 'sir')
        mock_llm['response'] = _PUSH_YAML
        d = _generate('pb-sir-1', 'push', '何か設定して')
        assert d['ok'] is False
        assert 'sir' in d['error']


class TestGeneratePlaybookValidation:
    def test_invalid_pattern_rejected(self):
        _dev('pb-invalid-1', 'arista')
        d = _generate('pb-invalid-1', 'not-a-real-pattern', '何か')
        assert d['ok'] is False

    def test_malformed_yaml_from_llm_is_reported_not_silently_accepted(self, mock_llm):
        """フェイクの成功を作らない: LLMが壊れたYAMLを返したら
        ok: Falseで明示的にエラーを返す。"""
        _dev('pb-malformed-1', 'arista')
        mock_llm['response'] = "- hosts: all\n  tasks:\n    - name: x\n   bad indent here:\n"
        d = _generate('pb-malformed-1', 'push', '何か')
        assert d['ok'] is False
        assert 'YAML' in d['error']
        assert 'raw' in d

    def test_non_list_yaml_from_llm_is_rejected(self, mock_llm):
        _dev('pb-nonlist-1', 'arista')
        mock_llm['response'] = "just_a_string: true\n"
        d = _generate('pb-nonlist-1', 'push', '何か')
        assert d['ok'] is False

    def test_markdown_fence_is_stripped(self, mock_llm):
        _dev('pb-fence-1', 'arista')
        mock_llm['response'] = f"```yaml\n{_PUSH_YAML}\n```"
        d = _generate('pb-fence-1', 'push', '何か')
        assert d['ok'] is True
        assert '```' not in d['playbook_yaml']

    def test_no_llm_backend_reports_error_not_fake_success(self):
        _dev('pb-nollm-1', 'arista')
        old_backend = app_module.LLM_BACKEND
        try:
            app_module.LLM_BACKEND = 'rules'
            d = _generate('pb-nollm-1', 'push', '何か設定して')
            assert d['ok'] is False
            assert 'AI' in d['error']
        finally:
            app_module.LLM_BACKEND = old_backend

    def test_empty_llm_response_reports_error(self, mock_llm):
        _dev('pb-empty-1', 'arista')
        mock_llm['response'] = None
        d = _generate('pb-empty-1', 'push', '何か')
        assert d['ok'] is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
