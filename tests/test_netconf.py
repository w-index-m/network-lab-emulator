"""
NETCONF（RFC 6241 / RFC 6242）のテスト

engine/netconf_agent.py のRPC処理・フレーミング・データモデルを検証する。
実SSHセッションを張るテスト（ncclient相互接続）は、鍵生成に時間がかかり
CI環境でポートを掴む必要があるため、ここではプロトコル層のみを対象にする。
実クライアント(ncclient)からの接続は docs/netconf-catalyst.md に手順と
実際の出力を記録している。
"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.netconf_agent import (          # noqa: E402
    NS, build_interfaces_xml, frame_chunked, handle_rpc, server_hello,
    unframe_chunked,
)
from engine.rules import DeviceState        # noqa: E402


def _dev():
    state = DeviceState('catalyst', 'SW1')
    state.interfaces = {
        'GigabitEthernet1/0/1': {'ip': '10.0.0.1', 'prefix': 24,
                                 'status': 'up', 'desc': 'uplink'},
        'GigabitEthernet1/0/2': {'ip': '', 'prefix': 0, 'status': 'down'},
    }
    return state


def _rpc(op_xml: str) -> ET.Element:
    return ET.fromstring(op_xml)


# ── hello / capabilities ───────────────────────────────
def test_server_hello_is_valid_xml_and_has_session_id():
    """helloがXMLとして妥当であること

    capability URI には "?module=...&revision=..." のように & が含まれる。
    エスケープしないとクライアント側でパースエラーになり、
    セッションが張れない（実際にncclientで発生した）。
    """
    hello = server_hello(42)
    root = ET.fromstring(hello)
    assert root.tag == f'{{{NS["nc"]}}}hello'
    sid = root.find(f'{{{NS["nc"]}}}session-id')
    assert sid is not None and sid.text == '42'
    caps = [c.text for c in root.iter(f'{{{NS["nc"]}}}capability')]
    assert 'urn:ietf:params:netconf:base:1.0' in caps
    assert any('ietf-interfaces' in c for c in caps)


# ── フレーミング（RFC 6242 chunked）───────────────────
def test_chunked_framing_roundtrip():
    payload = '<rpc-reply><ok/></rpc-reply>'
    msg, rest = unframe_chunked(frame_chunked(payload))
    assert msg == payload
    assert rest == b''


def test_chunked_framing_waits_for_complete_message():
    """途中までしか届いていないバッファではNoneを返して待つ"""
    framed = frame_chunked('<rpc-reply><ok/></rpc-reply>')
    msg, rest = unframe_chunked(framed[:8])
    assert msg is None
    assert rest == framed[:8]


# ── データモデル ───────────────────────────────────────
def test_build_interfaces_xml_reflects_device_state():
    xml = build_interfaces_xml(_dev())
    root = ET.fromstring(xml)
    names = [e.text for e in root.iter(f'{{{NS["if"]}}}name')]
    assert 'GigabitEthernet1/0/1' in names
    assert '10.0.0.1' in xml
    # shutdown中のインタフェースは enabled=false
    assert '<enabled>false</enabled>' in xml


# ── RPC ────────────────────────────────────────────────
def test_get_config_returns_interfaces():
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="101">
          <get-config><source><running/></source></get-config>
        </rpc>''')
    root = ET.fromstring(reply)
    assert root.get('message-id') == '101'
    assert root.find(f'{{{NS["nc"]}}}data') is not None
    assert 'GigabitEthernet1/0/1' in reply


def test_get_config_rejects_non_running_datastore():
    reply = handle_rpc(_dev(), f'''
        <rpc xmlns="{NS['nc']}" message-id="1">
          <get-config><source><candidate/></source></get-config>
        </rpc>''')
    assert 'operation-not-supported' in reply


def test_edit_config_updates_device_state():
    """edit-configがCLI側の状態（state.interfaces）を実際に書き換える"""
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="7">
          <edit-config>
            <target><running/></target>
            <config>
              <interfaces xmlns="{NS['if']}">
                <interface>
                  <name>GigabitEthernet1/0/2</name>
                  <description>set-by-netconf</description>
                  <enabled>true</enabled>
                  <ipv4 xmlns="{NS['ip']}">
                    <address><ip>192.0.2.5</ip><netmask>255.255.255.0</netmask></address>
                  </ipv4>
                </interface>
              </interfaces>
            </config>
          </edit-config>
        </rpc>''')
    assert '<ok/>' in reply
    info = state.interfaces['GigabitEthernet1/0/2']
    assert info['ip'] == '192.0.2.5'
    assert info['prefix'] == 24
    assert info['status'] == 'up'
    assert info['desc'] == 'set-by-netconf'


def test_edit_config_unknown_interface_is_rpc_error():
    """存在しないインタフェースはrpc-errorになり、応答は妥当なXMLのまま

    エラーメッセージにコマンド由来の文字列を素で埋め込むと、
    < や & が混ざったときに応答XML自体が壊れてクライアントが
    パースできなくなる（実際にncclientで発生した）。
    """
    reply = handle_rpc(_dev(), f'''
        <rpc xmlns="{NS['nc']}" message-id="9">
          <edit-config>
            <target><running/></target>
            <config>
              <interfaces xmlns="{NS['if']}">
                <interface><name>GigabitEthernet9/9/9</name></interface>
              </interfaces>
            </config>
          </edit-config>
        </rpc>''')
    root = ET.fromstring(reply)          # 妥当なXMLであること
    assert root.find(f'{{{NS["nc"]}}}rpc-error') is not None
    assert 'does not exist' in reply


def test_edit_config_delete_removes_address():
    state = _dev()
    reply = handle_rpc(state, f'''
        <rpc xmlns="{NS['nc']}" message-id="11">
          <edit-config>
            <target><running/></target>
            <config>
              <interfaces xmlns="{NS['if']}">
                <interface xmlns:nc="{NS['nc']}" nc:operation="delete">
                  <name>GigabitEthernet1/0/1</name>
                </interface>
              </interfaces>
            </config>
          </edit-config>
        </rpc>''')
    assert '<ok/>' in reply
    assert state.interfaces['GigabitEthernet1/0/1']['ip'] == ''


def test_close_session_returns_ok():
    reply = handle_rpc(_dev(), f'''
        <rpc xmlns="{NS['nc']}" message-id="99"><close-session/></rpc>''')
    assert '<ok/>' in reply


def test_unsupported_operation_is_rpc_error():
    reply = handle_rpc(_dev(), f'''
        <rpc xmlns="{NS['nc']}" message-id="5"><commit/></rpc>''')
    assert 'operation-not-supported' in reply


def test_malformed_rpc_is_rpc_error():
    reply = handle_rpc(_dev(), '<rpc><unclosed>')
    assert 'malformed-message' in reply
    ET.fromstring(reply)                 # 応答自体は妥当なXML
