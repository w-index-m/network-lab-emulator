"""
NETCONF/RESTCONF サービスレベルACL と IOS名前付き標準ACL のテスト

実機構文（Cisco IOS-XE）:
    ip access-list standard <name>
     [<seq>] permit|deny {any | host A.B.C.D | A.B.C.D W.W.W.W}
    netconf-yang ssh {ipv4|ipv6} access-list name <acl>
    netconf-yang ssh port <n>
    restconf {ipv4|ipv6} access-list name <acl>

インタフェースACLと違い、サービスへの着信を送信元アドレスだけで絞る。

修正前の状態:
  - `ip access-list standard` サブモード自体が無く（拡張ACLのみ）、
    サービスレベルACLを書く土台が無かった
  - ACLの `A.B.C.D W.W.W.W`（ワイルドカードマスク形式）が
    _match_addr で解釈できず、どのルールにも一致せず暗黙denyになっていた
  - 標準ACLでも "Extended IP access list" と表示されていた
  - running-config がASA形式の1行表記で、投入し直せなかった
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient    # noqa: E402

import app as app_module                     # noqa: E402
from engine.protocols import ipfilter_engine  # noqa: E402

client = TestClient(app_module.app)

DEV = 't-svcacl'


def _cli(cmd, dev=DEV):
    return client.post('/api/cli',
                       json={'device_id': dev, 'command': cmd}).json()['output']


def _setup(dev=DEV):
    ipfilter_engine.acls.pop(dev, None)
    ipfilter_engine.acl_kind.pop(dev, None)
    app_module.device_sessions.pop(dev, None)
    client.post('/api/device',
                json={'id': dev, 'type': 'catalyst', 'hostname': dev})
    for c in ('configure terminal',
              'interface GigabitEthernet1/0/1', 'no switchport',
              'ip address 10.99.0.1 255.255.255.0', 'no shutdown', 'exit',
              'ip access-list standard MGMT_ACL',
              'permit 10.99.0.0 0.0.0.255',
              'permit host 192.0.2.7',
              'deny any', 'exit', 'end'):
        _cli(c, dev)
    return dev


# ── 名前付き標準ACL ────────────────────────────────────
def test_standard_acl_entries_are_stored_in_order():
    dev = _setup()
    rules = ipfilter_engine.acls[dev]['MGMT_ACL']
    assert [r.seq for r in rules] == [10, 20, 30]
    assert [r.action for r in rules] == ['permit', 'permit', 'deny']


def test_standard_acl_is_shown_as_standard_not_extended():
    dev = _setup()
    out = _cli('show ip access-lists', dev)
    assert 'Standard IP access list MGMT_ACL' in out
    assert 'Extended' not in out
    # 実機はワイルドカードを "wildcard bits" と表記する
    assert '10 permit 10.99.0.0, wildcard bits 0.0.0.255' in out
    # host 指定はアドレスだけになる
    assert '20 permit 192.0.2.7' in out


def test_running_config_emits_ios_named_acl_form():
    """running-configがそのまま投入し直せる形で出ること"""
    dev = _setup()
    out = _cli('show running-config', dev)
    assert 'ip access-list standard MGMT_ACL' in out
    assert ' 10 permit 10.99.0.0 0.0.0.255' in out
    # ASA形式の1行表記になっていないこと
    assert 'access-list MGMT_ACL permit' not in out


def test_explicit_sequence_number_inserts_before_existing_entries():
    dev = _setup()
    for c in ('configure terminal', 'ip access-list standard MGMT_ACL',
              '5 permit host 10.0.0.9', 'end'):
        _cli(c, dev)
    assert [r.seq for r in ipfilter_engine.acls[dev]['MGMT_ACL']] == \
        [5, 10, 20, 30]


# ── アドレス照合 ───────────────────────────────────────
def test_wildcard_mask_form_is_matched():
    """A.B.C.D W.W.W.W 形式が解釈できること（未対応だと全部暗黙denyになる）"""
    assert ipfilter_engine._match_addr('10.99.0.0 0.0.0.255', '10.99.0.55')
    assert not ipfilter_engine._match_addr('10.99.0.0 0.0.0.255', '10.99.1.55')


def test_check_source_follows_first_match_wins():
    dev = _setup()
    assert ipfilter_engine.check_source(dev, 'MGMT_ACL', '10.99.0.55')
    assert ipfilter_engine.check_source(dev, 'MGMT_ACL', '192.0.2.7')
    # deny any に落ちる
    assert not ipfilter_engine.check_source(dev, 'MGMT_ACL', '203.0.113.1')


def test_check_source_allows_when_acl_does_not_exist():
    """存在しないACL名を指定しても全拒否にはならない（実機同様素通し）"""
    dev = _setup()
    assert ipfilter_engine.check_source(dev, 'NO_SUCH_ACL', '203.0.113.1')


def test_check_source_without_acl_name_is_permitted():
    dev = _setup()
    assert ipfilter_engine.check_source(dev, '', '203.0.113.1')


# ── サービスレベルACLの設定 ────────────────────────────
def test_service_acl_commands_are_stored_and_emitted():
    dev = _setup()
    for c in ('configure terminal',
              'netconf-yang ssh ipv4 access-list name MGMT_ACL',
              'netconf-yang ssh port 8830',
              'ip http secure-server', 'restconf',
              'restconf ipv4 access-list name MGMT_ACL', 'end'):
        _cli(c, dev)
    state = app_module.device_sessions[dev]
    assert state.netconf_service_acl['ipv4'] == 'MGMT_ACL'
    assert state.restconf_service_acl['ipv4'] == 'MGMT_ACL'
    assert state.netconf_ssh_port == 8830
    out = _cli('show running-config', dev)
    assert 'netconf-yang ssh ipv4 access-list name MGMT_ACL' in out
    assert 'netconf-yang ssh port 8830' in out
    assert 'restconf ipv4 access-list name MGMT_ACL' in out


def test_no_form_removes_service_acl():
    dev = _setup()
    for c in ('configure terminal',
              'netconf-yang ssh ipv4 access-list name MGMT_ACL',
              'no netconf-yang ssh ipv4 access-list name MGMT_ACL',
              'netconf-yang ssh port 8830',
              'no netconf-yang ssh port 8830', 'end'):
        _cli(c, dev)
    state = app_module.device_sessions[dev]
    assert 'ipv4' not in (state.netconf_service_acl or {})
    assert state.netconf_ssh_port == 830


def test_invalid_netconf_port_is_rejected():
    dev = _setup()
    _cli('configure terminal', dev)
    assert 'Invalid' in _cli('netconf-yang ssh port 99999', dev)


# ── RESTCONFでの実際の遮断 ────────────────────────────
def _enable_restconf(dev):
    for c in ('configure terminal', 'ip http secure-server', 'restconf',
              'restconf ipv4 access-list name MGMT_ACL', 'end'):
        _cli(c, dev)


def test_restconf_request_from_denied_source_is_rejected():
    dev = _setup()
    _enable_restconf(dev)
    # TestClientの送信元は testclient / 127.0.0.1 で、MGMT_ACLでは
    # deny any に落ちる
    r = client.get(f'/restconf/{dev}/data/ietf-interfaces:interfaces',
                   auth=('admin', 'admin'))
    assert r.status_code == 403
    body = r.json()['ietf-restconf:errors']['error'][0]
    assert body['error-tag'] == 'access-denied'


def test_restconf_request_from_permitted_source_succeeds():
    dev = _setup()
    _enable_restconf(dev)
    # deny any より前(seq 5)に許可を入れる。先勝ちなので通るようになる。
    for c in ('configure terminal', 'ip access-list standard MGMT_ACL',
              '5 permit any', 'end'):
        _cli(c, dev)
    r = client.get(f'/restconf/{dev}/data/ietf-interfaces:interfaces',
                   auth=('admin', 'admin'))
    assert r.status_code == 200
    assert 'ietf-interfaces:interfaces' in r.json()


def test_restconf_without_service_acl_is_not_blocked():
    dev = _setup()
    for c in ('configure terminal', 'ip http secure-server', 'restconf', 'end'):
        _cli(c, dev)
    r = client.get(f'/restconf/{dev}/data/ietf-interfaces:interfaces',
                   auth=('admin', 'admin'))
    assert r.status_code == 200
