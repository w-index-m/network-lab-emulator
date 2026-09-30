"""
IOS系(cisco/catalyst)のAAA名前付きメソッドリスト + line con 0。

CiscoDevNet/cml-communityの`lab-topologies/aaa-tacacs-exploration`
(コンソール回線をローカル認証だけに固定するAAA構成ガイド)で使われている
下記のコマンド列を試したところ、"default"以外の名前付きメソッドリストが
一切実装されておらず、"line con 0"自体も受理されない(config-lineに
遷移せず、中のサブコマンドも黙って無視される)ことが分かった:

    aaa authentication login CONSOLE local
    aaa authorization console
    aaa authorization exec CONSOLE local
    line con 0
     login authentication CONSOLE
     authorization exec CONSOLE

これらを実装し、既存の"default"専用メソッドリスト
(aaa authentication login default / aaa authorization exec default)
との後方互換性(state.aaa_authentication_login /
state.aaa_authorization_exec単体属性、tests/test_nexus_tacacs_config.py
等が前提にしている形)を壊していないことを合わせて固定する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'device_id': id_, 'device_type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _cli(id_, cmd)
    return out


class TestNamedAuthenticationLogin:
    def test_named_list_with_local_is_accepted_and_stored(self):
        _dev('aaa-authc-1')
        _run('aaa-authc-1', ['configure terminal', 'aaa new-model',
                              'aaa authentication login CONSOLE local'])
        state = app_module.device_sessions['aaa-authc-1']
        # local_fallback: 既存の"default"実装と同じく、group有無を問わず
        # "local"キーワードが付けば立つ(groupが空ならrunning-config側で
        # 単独の"local"として出す)。
        assert state.aaa_authentication_login_lists['CONSOLE'] == {
            'group': '', 'local_fallback': True}

    def test_named_list_preserves_case(self):
        _dev('aaa-authc-2')
        _run('aaa-authc-2', ['configure terminal', 'aaa new-model',
                              'aaa authentication login CONSOLE local'])
        rc = _run('aaa-authc-2', ['end', 'show running-config'])
        assert 'aaa authentication login CONSOLE local' in rc
        assert 'aaa authentication login console' not in rc

    def test_named_list_with_group_and_local_fallback(self):
        _dev('aaa-authc-3')
        _run('aaa-authc-3', ['configure terminal', 'aaa new-model',
                              'aaa authentication login VTY-AUTH group TACGRP local'])
        state = app_module.device_sessions['aaa-authc-3']
        assert state.aaa_authentication_login_lists['VTY-AUTH'] == {
            'group': 'TACGRP', 'local_fallback': True}

    def test_default_list_still_sets_single_attribute_for_backward_compat(self):
        _dev('aaa-authc-4')
        _run('aaa-authc-4', ['configure terminal', 'aaa new-model',
                              'aaa authentication login default group TACGRP local'])
        state = app_module.device_sessions['aaa-authc-4']
        assert state.aaa_authentication_login == {
            'group': 'TACGRP', 'local_fallback': True}
        assert state.aaa_authentication_login_lists['default'] == {
            'group': 'TACGRP', 'local_fallback': True}


class TestAuthorizationConsole:
    def test_aaa_authorization_console_sets_flag(self):
        _dev('aaa-authz-console-1')
        _run('aaa-authz-console-1', ['configure terminal', 'aaa new-model',
                                      'aaa authorization console'])
        state = app_module.device_sessions['aaa-authz-console-1']
        assert state.aaa_authorization_console is True

    def test_no_aaa_authorization_console_clears_flag(self):
        _dev('aaa-authz-console-2')
        _run('aaa-authz-console-2', ['configure terminal', 'aaa new-model',
                                      'aaa authorization console',
                                      'no aaa authorization console'])
        state = app_module.device_sessions['aaa-authz-console-2']
        assert state.aaa_authorization_console is False

    def test_shows_in_running_config(self):
        _dev('aaa-authz-console-3')
        _run('aaa-authz-console-3', ['configure terminal', 'aaa new-model',
                                      'aaa authorization console'])
        rc = _run('aaa-authz-console-3', ['end', 'show running-config'])
        assert 'aaa authorization console' in rc


class TestNamedAuthorizationExec:
    def test_named_list_with_local_is_accepted_and_stored(self):
        _dev('aaa-authz-exec-1')
        _run('aaa-authz-exec-1', ['configure terminal', 'aaa new-model',
                                   'aaa authorization exec CONSOLE local'])
        state = app_module.device_sessions['aaa-authz-exec-1']
        assert state.aaa_authorization_exec_lists['CONSOLE'] == {
            'group': '', 'local_fallback': True}

    def test_default_list_still_sets_single_attribute_for_backward_compat(self):
        _dev('aaa-authz-exec-2')
        _run('aaa-authz-exec-2', ['configure terminal', 'aaa new-model',
                                   'aaa authorization exec default group TACGRP local'])
        state = app_module.device_sessions['aaa-authz-exec-2']
        assert state.aaa_authorization_exec == {
            'group': 'TACGRP', 'local_fallback': True}


class TestLineCon0:
    def test_line_con_0_enters_config_line_mode(self):
        _dev('line-con-1')
        _cli('line-con-1', 'configure terminal')
        result = client.post('/api/cli', json={'device_id': 'line-con-1',
                                                 'command': 'line con 0'}).json()
        assert result['mode'] == 'config-line'

    def test_login_authentication_under_line_con_0_is_stored(self):
        _dev('line-con-2')
        _run('line-con-2', ['configure terminal',
                             'aaa authentication login CONSOLE local',
                             'line con 0', 'login authentication CONSOLE'])
        state = app_module.device_sessions['line-con-2']
        assert state.line_con_login_authentication == 'CONSOLE'

    def test_authorization_exec_under_line_con_0_is_stored(self):
        _dev('line-con-3')
        _run('line-con-3', ['configure terminal',
                             'aaa authorization exec CONSOLE local',
                             'line con 0', 'authorization exec CONSOLE'])
        state = app_module.device_sessions['line-con-3']
        assert state.line_con_authorization_exec == 'CONSOLE'

    def test_login_authentication_under_line_vty_is_stored_separately_from_con(self):
        _dev('line-con-4')
        _run('line-con-4', ['configure terminal',
                             'line vty 0 4', 'login authentication VTY-AUTH',
                             'exit',
                             'line con 0', 'login authentication CONSOLE'])
        state = app_module.device_sessions['line-con-4']
        assert state.line_vty_login_authentication == 'VTY-AUTH'
        assert state.line_con_login_authentication == 'CONSOLE'

    def test_full_cml_community_aaa_tacacs_exploration_scenario(self):
        """CiscoDevNet/cml-communityのaaa-tacacs-explorationラボで
        使われている実際のコマンド列を通しで流し、show running-configに
        全項目が(大文字小文字を保った状態で)反映されることを確認する。"""
        _dev('cml-scenario')
        _run('cml-scenario', [
            'configure terminal',
            'aaa new-model',
            'aaa authentication login CONSOLE local',
            'aaa authorization console',
            'aaa authorization exec CONSOLE local',
            'line con 0',
            'login authentication CONSOLE',
            'authorization exec CONSOLE',
            'end',
        ])
        rc = _cli('cml-scenario', 'show running-config')
        assert 'aaa new-model' in rc
        assert 'aaa authentication login CONSOLE local' in rc
        assert 'aaa authorization console' in rc
        assert 'aaa authorization exec CONSOLE local' in rc
        assert 'line con 0' in rc
        assert ' login authentication CONSOLE' in rc
        assert ' authorization exec CONSOLE' in rc
