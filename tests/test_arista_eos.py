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


def _dev(id_, type_='arista'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


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


class TestAristaCiscoLldp:
    """Arista⇔Cisco間でLLDPネイバー探索が実際に機能することの確認。

    ライブ検証中に見つかった副産物のバグ: Cisco系のshow lldp neighbors
    テーブルは Local Intf 列を固定幅16桁で出力していたが、
    GigabitEthernet0/0/0 のような20文字のフル長インタフェース名は
    Hold-time列と連結してしまい列がずれていた(実機Ciscoは
    Local Intf列を Gi0/0/0 のように短縮表記するため、この問題が起きない)。
    既存の _abbrev_if ヘルパーを local_if/port_id に適用して修正。
    """

    def test_lldp_not_enabled_by_default_on_cisco(self):
        _dev('lldp-cisco-1', type_='cisco')
        _dev('lldp-arista-1', type_='arista')
        client.post('/api/link', json={'a': 'lldp-arista-1', 'b': 'lldp-cisco-1'})
        out = _cli('lldp-cisco-1', 'show lldp neighbors')
        assert out == '% LLDP is not enabled'

    def test_cisco_sees_arista_neighbor_after_lldp_run(self):
        _dev('lldp-cisco-2', type_='cisco')
        _dev('lldp-arista-2', type_='arista')
        client.post('/api/link', json={'a': 'lldp-arista-2', 'b': 'lldp-cisco-2'})
        _run('lldp-cisco-2', ['configure terminal', 'lldp run'])
        out = _cli('lldp-cisco-2', 'show lldp neighbors')
        assert 'lldp-arista-2' in out  # system_name == hostname == device_id (この固定値)
        assert 'Total entries displayed: 1' in out
        assert 'Ethernet1' in out  # 相手(Arista)側インタフェース名

    def test_arista_sees_cisco_neighbor_without_lldp_run(self):
        """実機のArista EOSはLLDPが既定で有効(Cisco IOSと異なる)。"""
        _dev('lldp-cisco-3', type_='cisco')
        _dev('lldp-arista-3', type_='arista')
        client.post('/api/link', json={'a': 'lldp-arista-3', 'b': 'lldp-cisco-3'})
        out = _cli('lldp-arista-3', 'show lldp neighbors')
        assert 'Total entries displayed: 1' in out

    def test_long_interface_name_column_alignment_fixed(self):
        """GigabitEthernet0/0/0のような長いインタフェース名でも
        Local IntfとHold-time列がくっつかないことを固定する回帰テスト。"""
        _dev('lldp-cisco-4', type_='cisco')
        _dev('lldp-arista-4', type_='arista')
        client.post('/api/link', json={'a': 'lldp-arista-4', 'b': 'lldp-cisco-4'})
        _run('lldp-cisco-4', ['configure terminal', 'lldp run'])
        out = _cli('lldp-cisco-4', 'show lldp neighbors')
        assert 'Gi0/0/0' in out
        assert 'Gi0/0/0120' not in out
