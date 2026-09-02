"""
Nexus TACACS+/AAA 設定の永続化テスト

app.py の _save_config() / _load_config() が、TACACS+/AAA設定を
saved_config.json に正しく保存・復元することを検証する。
device_sessions/vnet/SAVED_CONFIG_PATH は monkeypatch でテスト用に差し替える。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app
from engine.rules import DeviceState


def test_tacacs_config_survives_save_and_load(tmp_path, monkeypatch):
    cfg_path = tmp_path / 'saved_config.json'
    monkeypatch.setattr(app, 'SAVED_CONFIG_PATH', cfg_path)
    monkeypatch.setattr(app, 'device_sessions', {})

    state = DeviceState('nexus', 'nexus')
    state.tacacs_feature_enabled = True
    state.tacacs_hosts = [{'host': '127.0.0.1', 'key': 'demo', 'port': 49}]
    state.aaa_tacacs_groups = {'TAC-GROUP': {'servers': ['127.0.0.1']}}
    state.aaa_authentication_login = {'group': 'TAC-GROUP', 'local_fallback': True}
    state.aaa_authorization_commands = {'group': 'TAC-GROUP', 'local_fallback': True}
    state.aaa_accounting_commands = {'group': 'TAC-GROUP'}
    state.aaa_accounting_exec = {'group': 'TAC-GROUP'}
    app.device_sessions['nexus'] = state

    app._save_config()
    assert cfg_path.exists()

    # 設定ファイルの中身を直接確認
    import json
    saved = json.loads(cfg_path.read_text(encoding='utf-8'))
    nexus_data = saved['devices']['nexus']
    assert nexus_data['tacacs_feature_enabled'] is True
    assert nexus_data['tacacs_hosts'] == [{'host': '127.0.0.1', 'key': 'demo', 'port': 49}]
    assert nexus_data['aaa_tacacs_groups'] == {'TAC-GROUP': {'servers': ['127.0.0.1']}}
    assert nexus_data['aaa_accounting_exec'] == {'group': 'TAC-GROUP'}

    # 新しいdevice_sessionsにロードし直して復元されることを確認
    monkeypatch.setattr(app, 'device_sessions', {})
    app._load_config()

    restored = app.device_sessions['nexus']
    assert restored.tacacs_feature_enabled is True
    assert restored.tacacs_hosts == [{'host': '127.0.0.1', 'key': 'demo', 'port': 49}]
    assert restored.aaa_tacacs_groups == {'TAC-GROUP': {'servers': ['127.0.0.1']}}
    assert restored.aaa_authentication_login == {'group': 'TAC-GROUP', 'local_fallback': True}
    assert restored.aaa_authorization_commands == {'group': 'TAC-GROUP', 'local_fallback': True}
    assert restored.aaa_accounting_commands == {'group': 'TAC-GROUP'}
    assert restored.aaa_accounting_exec == {'group': 'TAC-GROUP'}


def test_non_nexus_device_has_no_tacacs_fields_in_save(tmp_path, monkeypatch):
    cfg_path = tmp_path / 'saved_config.json'
    monkeypatch.setattr(app, 'SAVED_CONFIG_PATH', cfg_path)
    monkeypatch.setattr(app, 'device_sessions', {})

    state = DeviceState('cisco', 'r1')
    app.device_sessions['r1'] = state
    app._save_config()

    import json
    saved = json.loads(cfg_path.read_text(encoding='utf-8'))
    r1_data = saved['devices']['r1']
    assert 'tacacs_feature_enabled' not in r1_data
    assert 'aaa_tacacs_groups' not in r1_data


def test_nexus_without_tacacs_config_saves_no_tacacs_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / 'saved_config.json'
    monkeypatch.setattr(app, 'SAVED_CONFIG_PATH', cfg_path)
    monkeypatch.setattr(app, 'device_sessions', {})

    state = DeviceState('nexus', 'nexus')
    app.device_sessions['nexus'] = state
    app._save_config()

    import json
    saved = json.loads(cfg_path.read_text(encoding='utf-8'))
    nexus_data = saved['devices']['nexus']
    assert 'tacacs_feature_enabled' not in nexus_data
    assert 'tacacs_hosts' not in nexus_data
