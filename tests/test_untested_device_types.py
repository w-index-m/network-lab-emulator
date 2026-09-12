"""
これまでテストが1件も無かった装置種別（ASA / BIG-IP / PC）の固定

カバレッジを測ったところ engine/rules.py に「一度も実行されない関数」が
125件あり、その上位は `_asa_process`(約345行) / `_bigip_process`(約134行)
/ `_pc_process`(約87行) だった。

当初これを「app.py との二層ディスパッチで到達しないコード」だと疑った
が、実際に動かして確認したところ **3つとも正常に動作していた**。
つまり到達不能なのではなく、単にテストが1件も無かっただけ。
ここで現状の動作を固定して、以後の変更で壊れたら気付けるようにする。

（実装の穴も見つかっているので、末尾に既知の未対応をまとめてある）
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.rules import DeviceState, RuleEngine     # noqa: E402

engine = RuleEngine()


def _dev(dtype, hostname):
    st = DeviceState(dtype, hostname)
    st.mode = 'exec'
    return st


def _run(st, cmds):
    out = ''
    for c in cmds:
        out = engine.process(c, st) or ''
    return out


# ══════════════════════════════════════════
# Cisco ASA
# ══════════════════════════════════════════
def _asa():
    return _dev('asa', 'ASA1')


def test_asa_enable_and_config_prompt():
    st = _asa()
    assert 'Type help' in engine.process('enable', st)
    assert 'ASA1(config)#' in engine.process('configure terminal', st)


def test_asa_interface_nameif_and_ip_are_applied():
    st = _asa()
    _run(st, ['configure terminal', 'interface GigabitEthernet0/0',
              'nameif outside', 'security-level 0',
              'ip address 203.0.113.1 255.255.255.0', 'no shutdown', 'end'])
    out = engine.process('show interface ip brief', st)
    assert 'GigabitEthernet0/0' in out
    assert '203.0.113.1' in out


def test_asa_show_nameif_lists_security_levels():
    st = _asa()
    out = engine.process('show nameif', st)
    assert 'Interface' in out and 'Security' in out
    # 既定のinside/outsideが用意されている
    assert 'outside' in out
    assert 'inside' in out
    assert '100' in out          # inside のセキュリティレベル


def test_asa_running_config_has_asa_shape():
    st = _asa()
    out = engine.process('show running-config', st)
    assert out.lstrip().startswith(': Saved')
    assert 'ASA Version' in out
    assert 'hostname ASA1' in out
    assert 'nameif' in out


def test_asa_running_config_reflects_interface_changes():
    st = _asa()
    _run(st, ['configure terminal', 'interface GigabitEthernet0/0',
              'nameif outside', 'security-level 0',
              'ip address 203.0.113.1 255.255.255.0', 'end'])
    out = engine.process('show running-config', st)
    assert ' nameif outside' in out
    assert ' ip address 203.0.113.1 255.255.255.0' in out


def test_asa_show_crypto_ipsec_sa_when_empty():
    st = _asa()
    assert 'no ipsec sas' in engine.process('show crypto ipsec sa', st).lower()


# ══════════════════════════════════════════
# F5 BIG-IP
# ══════════════════════════════════════════
def _bigip():
    return _dev('bigip', 'LTM1')


def test_bigip_show_sys_version():
    out = engine.process('tmsh show sys version', _bigip())
    assert 'Sys::Version' in out
    assert 'BIG-IP' in out


def test_bigip_empty_pool_and_virtual():
    st = _bigip()
    assert 'no pools configured' in engine.process('show ltm pool', st)
    assert 'no virtual servers configured' in engine.process(
        'show ltm virtual', st)


def test_bigip_unknown_subcommand_reports_syntax_error():
    """対応コマンドの案内が出ること（黙って空にならない）"""
    out = engine.process('show sys hardware', _bigip())
    assert 'Syntax Error' in out
    assert 'ltm pool' in out


# ══════════════════════════════════════════
# PC（Linuxホスト）
# ══════════════════════════════════════════
def _pc():
    return _dev('pc', 'PC1')


def test_pc_ifconfig_shows_eth0():
    out = engine.process('ifconfig', _pc())
    assert out.startswith('eth0:')
    assert 'inet ' in out
    assert 'netmask' in out


def test_pc_ip_addr_is_iproute2_style():
    out = engine.process('ip addr', _pc())
    assert '1: eth0:' in out
    assert 'link/ether' in out
    assert 'inet ' in out


def test_pc_routing_table_has_default_route():
    for cmd in ('netstat -rn', 'route -n'):
        out = engine.process(cmd, _pc())
        assert 'Kernel IP routing table' in out
        assert '0.0.0.0' in out, f'{cmd} にデフォルトルートが無い'


def test_pc_arp_table():
    out = engine.process('arp -a', _pc())
    assert 'HWaddress' in out


def test_pc_unknown_command_looks_like_bash():
    """実機(Linux)同様、未対応コマンドはbash風のエラーになる"""
    out = engine.process('nslookup example.com', _pc())
    assert 'command not found' in out


def test_pc_curl_returns_http_response():
    out = engine.process('curl http://10.0.0.1', _pc())
    assert '<!DOCTYPE html>' in out
    assert 'TCP Connection OK' in out


# ══════════════════════════════════════════
# 既知の未対応 / 実際は動いていたもの
# ══════════════════════════════════════════
def test_asa_access_list_is_stored_and_displayed():
    """ASAのACLはちゃんと動く

    探索中に一度 "Invalid input" が出たので未実装かと思ったが、
    config モードに入っていなかっただけだった。実際には保存され、
    show access-list がヒットカウンタ付きの実機書式で出る。
    """
    st = _asa()
    _run(st, ['configure terminal',
              'access-list OUT extended permit tcp any host 192.168.1.10 eq 80',
              'end'])
    assert st.acls['OUT'][0]['action'] == 'permit'
    assert st.acls['OUT'][0]['proto'] == 'tcp'
    out = engine.process('show access-list', st)
    assert 'access-list OUT line 1 extended permit tcp any host 192.168.1.10' in out
    assert 'hitcnt=' in out


def test_asa_object_network_submode_is_not_implemented():
    """object network 配下の host / subnet は未対応。

    `object network <name>` 自体は受理されるが、その配下のコマンドが
    通らない。黙って受理して「設定したのに効かない」状態になるより
    はっきり弾かれるほうが良い。実装したらこのテストを消すこと。
    """
    st = _asa()
    _run(st, ['configure terminal', 'object network WEB'])
    out = engine.process('host 192.168.1.10', st) or ''
    assert 'Invalid input' in out
