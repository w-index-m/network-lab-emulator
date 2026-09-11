"""
ローカルユーザ（username コマンド）のテスト

従来 `username` は受理されるだけで装置状態に保存されず、NETCONFの認証も
NACMのグループ/privilege判定も admin 固定でしか試せなかった。

実機構文:
    username <name> [privilege <0-15>] {password|secret} [0|5|7] <pw>
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient    # noqa: E402

import app as app_module                     # noqa: E402
from engine.netconf_agent import _device_users  # noqa: E402

client = TestClient(app_module.app)
DEV = 't-localusers'


def _cli(cmd, dev=DEV):
    return client.post('/api/cli',
                       json={'device_id': dev, 'command': cmd}).json()['output']


def _setup(dev=DEV):
    app_module.device_sessions.pop(dev, None)
    client.post('/api/device',
                json={'id': dev, 'type': 'catalyst', 'hostname': dev})
    _cli('configure terminal', dev)
    return dev


def test_username_is_stored_with_privilege_and_password():
    dev = _setup()
    _cli('username netadmin privilege 15 secret cisco123', dev)
    _cli('username alice privilege 5 secret alice123', dev)
    users = {u['name']: u for u in app_module.device_sessions[dev].users}
    assert users['netadmin']['privilege'] == 15
    assert users['netadmin']['password'] == 'cisco123'
    assert users['alice']['privilege'] == 5


def test_username_defaults_to_privilege_1():
    dev = _setup()
    _cli('username bob secret bob123', dev)
    users = {u['name']: u for u in app_module.device_sessions[dev].users}
    assert users['bob']['privilege'] == 1


def test_password_keyword_is_accepted_too():
    dev = _setup()
    _cli('username carol password 0 carol123', dev)
    users = {u['name']: u for u in app_module.device_sessions[dev].users}
    assert users['carol']['password'] == 'carol123'


def test_redefining_a_user_replaces_the_entry():
    dev = _setup()
    _cli('username dave privilege 1 secret old', dev)
    _cli('username dave privilege 15 secret new', dev)
    users = [u for u in app_module.device_sessions[dev].users
             if u['name'] == 'dave']
    assert len(users) == 1
    assert users[0]['privilege'] == 15
    assert users[0]['password'] == 'new'


def test_no_username_removes_the_entry():
    dev = _setup()
    _cli('username erin secret x', dev)
    _cli('no username erin', dev)
    assert not [u for u in app_module.device_sessions[dev].users
                if u['name'] == 'erin']


def test_invalid_privilege_level_is_rejected():
    dev = _setup()
    assert 'Invalid' in _cli('username frank privilege 99 secret x', dev)


def test_users_appear_in_running_config():
    dev = _setup()
    _cli('username netadmin privilege 15 secret cisco123', dev)
    _cli('username bob secret bob123', dev)
    _cli('end', dev)
    out = _cli('show running-config', dev)
    assert 'username netadmin privilege 15 secret cisco123' in out
    # privilege 1 は既定なので出力しない（実機と同じ）
    assert 'username bob secret bob123' in out


def test_netconf_auth_uses_local_users():
    """NETCONFの認証がローカルユーザDBを見ること"""
    dev = _setup()
    _cli('username netadmin privilege 15 secret cisco123', dev)
    _cli('username alice privilege 5 secret alice123', dev)
    state = app_module.device_sessions[dev]
    assert _device_users(state) == {'netadmin': 'cisco123',
                                    'alice': 'alice123'}


def test_netconf_auth_falls_back_to_admin_when_no_users():
    dev = _setup()
    state = app_module.device_sessions[dev]
    assert _device_users(state) == {'admin': 'admin'}


def test_interface_description_appears_in_running_config():
    """NETCONF/RESTCONFで書いたdescriptionがCLI側にも見えること"""
    dev = _setup()
    for c in ('interface GigabitEthernet1/0/1', 'no switchport',
              'description set-for-test', 'end'):
        _cli(c, dev)
    assert ' description set-for-test' in _cli('show running-config', dev)
