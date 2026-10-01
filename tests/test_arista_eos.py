"""
Arista EOS (device_type='arista') の最小対応。

cml-communityの調査(docs/)でArista EOSが未対応ベンダーの1つに挙がり、
「Cisco IOSに近い文法で既存のCisco系共有ツリーに乗せやすい」という
判断で追加した。EOSは"configure"/"configure terminal"、
"write"/"write memory"/"copy running-config startup-config"、
"interface"→config-if のモード遷移がIOSとほぼ同一なため、
engine/rules.py の RuleEngine.process() は専用の_xxx_processハンドラ
無しで(apresia/bigip等と違い)既存のCisco風共有ツリーにそのまま乗る
(CLAUDE.mdの「Device-specific CLI dialects」参照)。

固有に実装したのはこの3点のみ:
  - インタフェース名: Ethernet<N>（GigabitEthernet0/0/N ではなく）
  - show version: EOS形式の短い項目列挙(Cisco IOSの長いバナーではない)
  - show running-config: ヘッダ("! Command: show running-config"等)

(開発中に見つけた注意点: /api/device のボディキーは
"id"/"type"/"hostname" であって "device_id"/"device_type" ではない。
/api/cli 側は "device_id" を使うため紛らわしく、誤ったキーで装置を
作ろうとすると/api/cli初回呼び出し時にDEFAULT_DEVICES不在分の
フォールバックで黙って device_type="cisco" になってしまう。)
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_):
    client.post('/api/device', json={'id': id_, 'type': 'arista', 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _cli(id_, cmd)
    return out


class TestAristaBasics:
    def test_default_interfaces_use_ethernet_naming(self):
        _dev('arista-basic-1')
        rc = _cli('arista-basic-1', 'show running-config')
        assert 'interface Ethernet1' in rc
        assert 'interface Ethernet2' in rc
        assert 'GigabitEthernet' not in rc

    def test_show_version_is_eos_style_not_ios(self):
        _dev('arista-basic-2')
        out = _cli('arista-basic-2', 'show version')
        assert 'Arista' in out
        assert 'Software image version:' in out
        assert 'Cisco' not in out

    def test_running_config_header_is_eos_style(self):
        _dev('arista-basic-3')
        rc = _cli('arista-basic-3', 'show running-config')
        assert rc.startswith('! Command: show running-config')
        assert 'Building configuration...' not in rc


class TestAristaConfigFlow:
    def test_configure_terminal_and_interface_mode_transitions(self):
        _dev('arista-cfg-1')
        r1 = client.post('/api/cli', json={'device_id': 'arista-cfg-1',
                                            'command': 'configure terminal'}).json()
        assert r1['mode'] == 'config'
        r2 = client.post('/api/cli', json={'device_id': 'arista-cfg-1',
                                            'command': 'interface Ethernet4'}).json()
        assert r2['mode'] == 'config-if'

    def test_new_interface_ip_address_persists_to_running_config(self):
        _dev('arista-cfg-2')
        _run('arista-cfg-2', ['configure terminal', 'interface Ethernet4',
                               'ip address 192.168.50.1 255.255.255.0',
                               'no shutdown', 'end'])
        rc = _cli('arista-cfg-2', 'show running-config')
        assert 'interface Ethernet4' in rc
        assert 'ip address 192.168.50.1 255.255.255.0' in rc

    def test_write_memory_is_accepted(self):
        _dev('arista-cfg-3')
        out = _cli('arista-cfg-3', 'write memory')
        assert 'OK' in out

    def test_ospf_router_config_mode_works_via_shared_tree(self):
        """専用ハンドラ無しで既存のCisco共有ツリーに乗ることの確認
        (apresia/bigip等と違い、_arista_processは実装していない)。"""
        _dev('arista-cfg-4')
        client.post('/api/cli', json={'device_id': 'arista-cfg-4',
                                       'command': 'configure terminal'})
        r = client.post('/api/cli', json={'device_id': 'arista-cfg-4',
                                           'command': 'router ospf 1'}).json()
        assert r['mode'] == 'config-router'


class TestAristaHostnameAndPrompt:
    def test_hostname_appears_in_running_config(self):
        _dev('arista-host-1')
        client.post('/api/cli', json={'device_id': 'arista-host-1',
                                       'command': 'configure terminal'})
        client.post('/api/cli', json={'device_id': 'arista-host-1',
                                       'command': 'hostname spine1'})
        rc = _cli('arista-host-1', 'show running-config')
        assert 'hostname spine1' in rc
