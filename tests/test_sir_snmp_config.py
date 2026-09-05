"""
Si-R の SNMP設定コマンド（snmp agent/manager/service/user/view）の回帰テスト。

以前は実機に存在しない構文（"snmp community ... ro" / "snmp trap host ..."）
をパースしていた（実機で <ERROR> : 2 : format error になることを確認済み）。
2026-09-05に実機Tab補完で確認した正式構文に置き換えた。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _fresh_sir(device_id: str):
    client.post('/api/device', json={
        'id': device_id, 'type': 'sir', 'hostname': device_id,
    })

    def run(cmd):
        return client.post('/api/cli', json={
            'device_id': device_id, 'command': cmd,
        }).json()['output']
    return run


def test_snmp_agent_fields_are_stored():
    run = _fresh_sir('sir-snmp-agent-1')
    run('conf')
    run('snmp agent contact admin@example.com')
    run('snmp agent sysname sir-lab')
    run('snmp agent location tokyo-dc')
    out = run('show snmp')
    assert 'admin@example.com' in out
    assert 'sir-lab' in out
    assert 'tokyo-dc' in out


def test_snmp_service_enable_disable():
    run = _fresh_sir('sir-snmp-service-1')
    run('conf')
    assert 'enable' not in run('show snmp')  # 未設定時値はdisable
    run('snmp service enable')
    assert 'SNMP service : enable' in run('show snmp')


def test_snmp_manager_definition():
    run = _fresh_sir('sir-snmp-mgr-1')
    run('conf')
    run('snmp manager 1 10.0.0.100 public trap')
    out = run('show snmp')
    assert '#1 10.0.0.100 community=public trap=trap' in out
    # 同じmanager_numberを再定義すると上書きされる
    run('snmp manager 1 10.0.0.200 private trap write')
    out = run('show snmp')
    assert '10.0.0.100' not in out
    assert '#1 10.0.0.200 community=private trap=trap write=write' in out


def test_snmp_user_subcommands_apply_to_current_user():
    run = _fresh_sir('sir-snmp-user-1')
    run('conf')
    run('snmp user name testuser')
    run('snmp user address 10.0.0.101')
    run('snmp user auth sha')
    run('snmp user priv des')
    run('snmp user write all')
    run('snmp user read view 1')
    run('snmp user notify all')
    out = run('show snmp')
    assert ('testuser address=10.0.0.101 auth=sha priv=des write=all '
            'read=view1 notify=all') in out


def test_snmp_user_switching_current_user():
    run = _fresh_sir('sir-snmp-user-2')
    run('conf')
    run('snmp user name alice')
    run('snmp user auth md5')
    run('snmp user name bob')
    run('snmp user auth sha')
    out = run('show snmp')
    assert 'alice address=(未設定) auth=md5' in out
    assert 'bob address=(未設定) auth=sha' in out


def test_snmp_view_subtree_include_exclude():
    run = _fresh_sir('sir-snmp-view-1')
    run('conf')
    run('snmp view 1 subtree 0 include system')
    run('snmp view 1 subtree 1 exclude enterprises')
    out = run('show snmp')
    assert 'view 1 subtree 0 include system' in out
    assert 'view 1 subtree 1 exclude enterprises' in out


def test_old_fabricated_syntax_is_no_longer_parsed():
    """以前実装していた偽構文が、他の何かとして誤って処理されないことの確認
    （少なくともクラッシュしない、意味のあるエラーにならないことだけ担保）。"""
    run = _fresh_sir('sir-snmp-old-1')
    run('conf')
    run('snmp community public ro')
    run('snmp trap host 192.168.1.200 community public')
    out = run('show snmp')
    assert 'managers     : (未設定)' in out
