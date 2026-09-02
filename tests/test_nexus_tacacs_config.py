"""
Nexus (NX-OS) TACACS+/AAA 設定コマンド テスト

app.py の _handle_nexus_tacacs_config() が実装する以下のコマンドを検証:
  feature tacacs+
  tacacs-server host <ip> [key <key>]
  aaa group server tacacs+ <name> ... server <ip> ... exit
  aaa authentication login default group <name> [local]
  aaa authorization commands default group <name> [local]
  aaa accounting commands default group <name>
  show tacacs-server / show aaa groups
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
from engine.rules import DeviceState


def _nexus_state():
    return DeviceState('nexus', 'nexus-test')


def test_non_nexus_device_returns_none():
    state = DeviceState('cisco', 'r1')
    out = app._handle_nexus_tacacs_config('r1', 'feature tacacs+', state)
    assert out is None


def test_feature_tacacs_enables_flag():
    state = _nexus_state()
    out = app._handle_nexus_tacacs_config('nexus', 'feature tacacs+', state)
    assert out == ""
    assert state.tacacs_feature_enabled is True


def test_no_feature_tacacs_disables_flag():
    state = _nexus_state()
    state.tacacs_feature_enabled = True
    app._handle_nexus_tacacs_config('nexus', 'no feature tacacs+', state)
    assert state.tacacs_feature_enabled is False


def test_tacacs_server_host_stores_host_key_port():
    state = _nexus_state()
    app._handle_nexus_tacacs_config('nexus', 'tacacs-server host 127.0.0.1 key demo', state)
    assert state.tacacs_hosts == [{'host': '127.0.0.1', 'key': 'demo', 'port': 49}]


def test_no_tacacs_server_host_removes_it():
    state = _nexus_state()
    app._handle_nexus_tacacs_config('nexus', 'tacacs-server host 127.0.0.1 key demo', state)
    app._handle_nexus_tacacs_config('nexus', 'no tacacs-server host 127.0.0.1', state)
    assert state.tacacs_hosts == []


def test_aaa_group_server_enters_submode_and_preserves_case():
    state = _nexus_state()
    out = app._handle_nexus_tacacs_config('nexus', 'aaa group server tacacs+ TAC-GROUP', state)
    assert out == ""
    assert state.mode == 'config-sg-tacacs'
    assert 'TAC-GROUP' in state.aaa_tacacs_groups


def test_server_inside_submode_adds_to_group():
    state = _nexus_state()
    app._handle_nexus_tacacs_config('nexus', 'aaa group server tacacs+ TAC-GROUP', state)
    app._handle_nexus_tacacs_config('nexus', 'server 127.0.0.1', state)
    assert state.aaa_tacacs_groups['TAC-GROUP']['servers'] == ['127.0.0.1']


def test_aaa_authentication_login_preserves_group_name_case():
    state = _nexus_state()
    app._handle_nexus_tacacs_config(
        'nexus', 'aaa authentication login default group TAC-GROUP local', state)
    assert state.aaa_authentication_login == {'group': 'TAC-GROUP', 'local_fallback': True}


def test_aaa_authorization_commands_preserves_group_name_case():
    state = _nexus_state()
    app._handle_nexus_tacacs_config(
        'nexus', 'aaa authorization commands default group TAC-GROUP local', state)
    assert state.aaa_authorization_commands == {'group': 'TAC-GROUP', 'local_fallback': True}


def test_aaa_accounting_commands_preserves_group_name_case():
    state = _nexus_state()
    app._handle_nexus_tacacs_config(
        'nexus', 'aaa accounting commands default group TAC-GROUP', state)
    assert state.aaa_accounting_commands == {'group': 'TAC-GROUP'}


def test_show_tacacs_server_lists_configured_hosts():
    state = _nexus_state()
    app._handle_nexus_tacacs_config('nexus', 'tacacs-server host 127.0.0.1 key demo', state)
    out = app._handle_nexus_tacacs_config('nexus', 'show tacacs-server', state)
    assert '127.0.0.1' in out
    assert 'demo' not in out  # 平文キーは表示しない


def test_show_tacacs_server_empty_when_none_configured():
    state = _nexus_state()
    out = app._handle_nexus_tacacs_config('nexus', 'show tacacs-server', state)
    assert 'No TACACS+ server configured' in out


def test_show_aaa_groups_lists_group_names():
    state = _nexus_state()
    app._handle_nexus_tacacs_config('nexus', 'aaa group server tacacs+ TAC-GROUP', state)
    out = app._handle_nexus_tacacs_config('nexus', 'show aaa groups', state)
    assert 'TAC-GROUP' in out
