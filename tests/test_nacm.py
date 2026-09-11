"""
モデルベースAAA = NACM（RFC 8341 / ietf-netconf-acm）のテスト

Cisco IOS-XE の「モデルベースAAA」の実体はNACMで、NETCONF/RESTCONFからの
読み書きをユーザの所属グループ単位で許可/拒否する。設定はCLIではなく
NETCONF経由（/nacm サブツリー）で行うのが規格。

既定値はRFC 8341のYANGモジュール（ietf-netconf-acm@2018-02-14）どおり:
    enable-nacm            = true
    read-default           = permit
    write-default          = deny      ← 既定は「読めるが書けない」
    exec-default           = permit
    enable-external-groups = true
"""

import os
import sys
import xml.etree.ElementTree as ET

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.netconf_agent import (          # noqa: E402
    NS, apply_nacm_edit, build_nacm_xml, get_nacm, handle_rpc, nacm_check,
)
from engine.rules import DeviceState        # noqa: E402


def _dev():
    state = DeviceState('catalyst', 'SW1')
    state.interfaces = {
        'GigabitEthernet1/0/1': {'ip': '10.0.0.1', 'prefix': 24,
                                 'status': 'up', 'desc': 'uplink'},
    }
    return state


def _edit_nacm(state, inner_xml):
    return apply_nacm_edit(state, ET.fromstring(
        f'<nacm xmlns="{NS["nacm"]}">{inner_xml}</nacm>'))


# ── 既定値 ─────────────────────────────────────────────
def test_defaults_match_rfc8341():
    n = get_nacm(_dev())
    assert n['enable-nacm'] is True
    assert n['read-default'] == 'permit'
    assert n['write-default'] == 'deny'
    assert n['exec-default'] == 'permit'
    assert n['enable-external-groups'] is True


def test_default_allows_read_but_denies_write():
    """既定のNACMは「読めるが書けない」"""
    state = _dev()
    assert nacm_check(state, 'bob', 'read')
    assert not nacm_check(state, 'bob', 'update')
    assert not nacm_check(state, 'bob', 'create')
    assert not nacm_check(state, 'bob', 'delete')
    assert nacm_check(state, 'bob', 'exec')


def test_disabling_nacm_permits_everything():
    state = _dev()
    get_nacm(state)['enable-nacm'] = False
    assert nacm_check(state, 'bob', 'update')


def test_denied_counters_increment_on_denial():
    state = _dev()
    n = get_nacm(state)
    nacm_check(state, 'bob', 'update')
    assert n['denied-operations'] == 1
    assert n['denied-data-writes'] == 1
    # readは既定permitなので増えない
    nacm_check(state, 'bob', 'read')
    assert n['denied-operations'] == 1


# ── グループとルール ───────────────────────────────────
def test_recovery_user_can_bootstrap_nacm():
    """復旧セッションが無いと誰もNACMを設定できず装置を締め出す

    write-default は既定で deny なので、NACMを迂回できる管理者が
    いないと「書けないので書き込み許可も設定できない」状態になる。
    RFC 8341 3.3 の recovery session に相当する経路を必ず残す。
    """
    state = _dev()
    assert nacm_check(state, 'admin', 'update', 'ietf-netconf-acm', '/nacm')
    # 一般ユーザは当然拒否される
    assert not nacm_check(state, 'bob', 'update', 'ietf-netconf-acm', '/nacm')


def test_recovery_user_is_not_affected_by_deny_rules():
    state = _dev()
    _edit_nacm(state, """
        <rule-list><name>lockout</name><group>*</group>
          <rule><name>deny-everything</name>
            <access-operations>*</access-operations>
            <action>deny</action></rule>
        </rule-list>""")
    assert nacm_check(state, 'admin', 'update')
    assert not nacm_check(state, 'bob', 'update')


def test_recovery_group_bypasses_all_rules():
    """復旧用グループは全ルールを迂回して常に許可される"""
    state = _dev()
    _edit_nacm(state, '<groups><group><name>ndm-admin</name>'
                      '<user-name>root</user-name></group></groups>')
    assert nacm_check(state, 'root', 'update')
    assert nacm_check(state, 'root', 'delete')


def test_rule_permits_write_for_matching_group():
    state = _dev()
    _edit_nacm(state, """
        <groups><group><name>netops</name>
          <user-name>alice</user-name></group></groups>
        <rule-list>
          <name>netops-rules</name>
          <group>netops</group>
          <rule>
            <name>allow-ifaces</name>
            <module-name>ietf-interfaces</module-name>
            <access-operations>create update delete</access-operations>
            <action>permit</action>
          </rule>
        </rule-list>""")
    assert nacm_check(state, 'alice', 'update', 'ietf-interfaces')
    # ルールに載っていないユーザは既定(write-default=deny)のまま
    assert not nacm_check(state, 'bob', 'update', 'ietf-interfaces')


def test_rule_can_deny_explicitly():
    state = _dev()
    _edit_nacm(state, """
        <groups><group><name>ro</name>
          <user-name>carol</user-name></group></groups>
        <rule-list>
          <name>ro-rules</name><group>ro</group>
          <rule><name>no-read-nacm</name>
            <module-name>ietf-netconf-acm</module-name>
            <access-operations>read</access-operations>
            <action>deny</action></rule>
        </rule-list>""")
    assert not nacm_check(state, 'carol', 'read', 'ietf-netconf-acm')
    # 別モジュールの読みは既定(permit)のまま
    assert nacm_check(state, 'carol', 'read', 'ietf-interfaces')


def test_wildcard_group_matches_every_user():
    state = _dev()
    _edit_nacm(state, """
        <rule-list><name>all</name><group>*</group>
          <rule><name>allow-all-writes</name>
            <access-operations>*</access-operations>
            <action>permit</action></rule>
        </rule-list>""")
    assert nacm_check(state, 'anyone', 'update')


def test_first_matching_rule_wins():
    """先に書いたルールが勝つ（後続のdenyに落ちない）"""
    state = _dev()
    _edit_nacm(state, """
        <groups><group><name>g1</name>
          <user-name>dave</user-name></group></groups>
        <rule-list><name>rl</name><group>g1</group>
          <rule><name>permit-first</name>
            <access-operations>update</access-operations>
            <action>permit</action></rule>
          <rule><name>deny-later</name>
            <access-operations>update</access-operations>
            <action>deny</action></rule>
        </rule-list>""")
    assert nacm_check(state, 'dave', 'update')


def test_write_default_permit_opens_writes():
    state = _dev()
    _edit_nacm(state, '<write-default>permit</write-default>')
    assert nacm_check(state, 'bob', 'update')


# ── 入力検証 ───────────────────────────────────────────
def test_invalid_default_action_is_rejected():
    state = _dev()
    err = _edit_nacm(state, '<write-default>maybe</write-default>')
    assert err and 'permit' in err


def test_rule_without_valid_action_is_rejected():
    state = _dev()
    err = _edit_nacm(state, """
        <rule-list><name>rl</name><group>*</group>
          <rule><name>r</name><action>whatever</action></rule>
        </rule-list>""")
    assert err and 'action' in err


def test_group_delete_removes_it():
    state = _dev()
    _edit_nacm(state, '<groups><group><name>tmp</name>'
                      '<user-name>x</user-name></group></groups>')
    assert 'tmp' in get_nacm(state)['groups']
    _edit_nacm(state,
               f'<groups><group xmlns:nc="{NS["nc"]}" nc:operation="delete">'
               f'<name>tmp</name></group></groups>')
    assert 'tmp' not in get_nacm(state)['groups']


# ── NETCONF経由（RPC）─────────────────────────────────
def test_get_config_of_nacm_subtree():
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="1">
          <get-config><source><running/></source>
            <filter><nacm xmlns="{NS['nacm']}"/></filter>
          </get-config>
        </rpc>''', user='admin')
    assert '<write-default>deny</write-default>' in reply
    ET.fromstring(reply)


def test_edit_config_writes_nacm_over_netconf():
    """NACM自体をNETCONF経由で設定する（規格どおりCLIでは触らない）"""
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="2">
          <edit-config><target><running/></target>
            <config>
              <nacm xmlns="{NS['nacm']}">
                <groups><group><name>ndm-admin</name>
                  <user-name>admin</user-name></group></groups>
              </nacm>
            </config>
          </edit-config>
        </rpc>''', user='admin')
    assert '<ok/>' in reply
    assert get_nacm(state)['groups']['ndm-admin'] == ['admin']


def test_edit_config_on_interfaces_is_denied_by_default():
    """既定(write-default=deny)ではインタフェース変更が拒否される"""
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="3">
          <edit-config><target><running/></target>
            <config><interfaces xmlns="{NS['if']}"><interface>
              <name>GigabitEthernet1/0/1</name>
              <description>blocked</description>
            </interface></interfaces></config>
          </edit-config>
        </rpc>''', user='bob')
    assert 'access-denied' in reply
    # 装置状態は変わっていない
    assert state.interfaces['GigabitEthernet1/0/1']['desc'] == 'uplink'


def test_edit_config_succeeds_for_recovery_group_user():
    state = _dev()
    _edit_nacm(state, '<groups><group><name>ndm-admin</name>'
                      '<user-name>admin</user-name></group></groups>')
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="4">
          <edit-config><target><running/></target>
            <config><interfaces xmlns="{NS['if']}"><interface>
              <name>GigabitEthernet1/0/1</name>
              <description>set-by-admin</description>
            </interface></interfaces></config>
          </edit-config>
        </rpc>''', user='admin')
    assert '<ok/>' in reply
    assert state.interfaces['GigabitEthernet1/0/1']['desc'] == 'set-by-admin'


# ── ローカルユーザと privilege ──────────────────────────
def test_privilege15_local_user_is_the_recovery_session():
    """ローカルユーザが定義されていれば privilege 15 だけが復旧セッション"""
    state = _dev()
    state.users = [
        {'name': 'netadmin', 'privilege': 15, 'password': 'x'},
        {'name': 'alice', 'privilege': 5, 'password': 'y'},
    ]
    assert nacm_check(state, 'netadmin', 'update')
    assert not nacm_check(state, 'alice', 'update')
    # ローカルユーザを定義したら、組み込みadminはもう特別扱いされない
    assert not nacm_check(state, 'admin', 'update')


def test_builtin_admin_is_recovery_only_when_no_local_users():
    state = _dev()
    state.users = []
    assert nacm_check(state, 'admin', 'update')


def test_nacm_xml_is_wellformed_and_namespaced():
    state = _dev()
    _edit_nacm(state, """
        <groups><group><name>g</name><user-name>u</user-name></group></groups>
        <rule-list><name>rl</name><group>g</group>
          <rule><name>r</name><action>permit</action>
            <comment>a &amp; b</comment></rule>
        </rule-list>""")
    root = ET.fromstring(build_nacm_xml(state))
    assert root.tag == f'{{{NS["nacm"]}}}nacm'
