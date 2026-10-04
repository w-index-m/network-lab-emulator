"""
Juniper Junos (device_type='juniper') の最小対応。

cml-communityの node-definitions/juniper/ (vSRX, vJunos-Router/Switch/
Evolved, QFX, vMX) 調査から、Arista(Ciscoライクな共有ツリーに乗った)とは
対照的に「Cisco風共有ツリーに全く乗らない初めてのベンダー」として追加した。
Junosの最大の特徴はcandidate/active二段階コンフィグ(set/deleteで編集し、
commitして初めてactiveに反映される。Ciscoの即時反映とは根本的に異なる)
なので、他のapresia/bigip同様の専用ハンドラ(_juniper_process)を新設し、
RuleEngine.process()の先頭でdevice_type=='juniper'を他の全処理より先に
横取りする(Cisco系の共有ツリーには一切触れない)。

スコープ(実装した範囲):
  - configure / exit / commit / commit and-quit / rollback のモード遷移
  - set interfaces <if> unit <n> family inet address <ip>/<prefix>
    (commit後にstate.interfacesへ反映。commit前は無反映)
  - set system host-name <name> (commit後にstate.hostnameへ反映)
  - delete <path> (candidate/committedから該当行を除去)
  - show configuration (波括弧階層表示) / show configuration | display set
  - show version (Junos形式) / show interfaces terse / show lldp neighbors
  - インタフェース名: ge-0/0/<N> (GigabitEthernet0/0/Nではなく)

実装していない範囲(スコープ外、将来必要になれば拡張):
  - edit <path> によるパス階層への降下(フルパスのset/deleteのみ対応)
  - uncommitted changesがある状態でのexit時の確認プロンプト
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_):
    client.post('/api/device', json={'id': id_, 'type': 'juniper', 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _mode(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['mode']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _cli(id_, cmd)
    return out


class TestJuniperBasics:
    def test_default_interfaces_use_ge_naming(self):
        _dev('jnpr-basic-1')
        rc = _cli('jnpr-basic-1', 'show interfaces terse')
        assert 'ge-0/0/0.0' in rc
        assert 'GigabitEthernet' not in rc

    def test_show_version_is_junos_style(self):
        _dev('jnpr-basic-2')
        out = _cli('jnpr-basic-2', 'show version')
        assert 'Junos:' in out
        assert 'Cisco' not in out

    def test_exec_prompt_uses_angle_bracket_not_config_paren(self):
        """モード文字列自体はexecだが、promptはフロントエンド(getPrompt)が
        組み立てる。バックエンド側はmode文字列が従来通りexec/configである
        ことだけを確認する(getPrompt側のJunos分岐はブラウザUI専用)。"""
        _dev('jnpr-basic-3')
        assert _mode('jnpr-basic-3', 'show version') == 'exec'


class TestJuniperCommitFlow:
    def test_set_interface_address_not_applied_before_commit(self):
        _dev('jnpr-commit-1')
        _cli('jnpr-commit-1', 'configure')
        _cli('jnpr-commit-1', 'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24')
        terse = _cli('jnpr-commit-1', 'show interfaces terse')
        assert '172.16.5.1' not in terse

    def test_set_interface_address_applied_after_commit(self):
        _dev('jnpr-commit-2')
        _run('jnpr-commit-2', [
            'configure',
            'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24',
            'commit',
        ])
        terse = _cli('jnpr-commit-2', 'show interfaces terse')
        assert '172.16.5.1/24' in terse

    def test_commit_and_quit_returns_to_exec_mode(self):
        _dev('jnpr-commit-3')
        _cli('jnpr-commit-3', 'configure')
        mode = _mode('jnpr-commit-3', 'commit and-quit')
        assert mode == 'exec'

    def test_rollback_discards_uncommitted_set(self):
        _dev('jnpr-commit-4')
        _run('jnpr-commit-4', [
            'configure',
            'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24',
            'rollback',
            'commit',
        ])
        terse = _cli('jnpr-commit-4', 'show interfaces terse')
        assert '172.16.5.1' not in terse

    def test_hostname_set_applies_after_commit(self):
        _dev('jnpr-commit-5')
        _run('jnpr-commit-5', ['configure', 'set system host-name spine-j1', 'commit'])
        state = app_module.device_sessions['jnpr-commit-5']
        assert state.hostname == 'spine-j1'

    def test_delete_removes_committed_interface_address(self):
        _dev('jnpr-commit-6')
        _run('jnpr-commit-6', [
            'configure',
            'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24',
            'commit',
        ])
        rc_before = _cli('jnpr-commit-6', 'show configuration | display set')
        assert 'ge-0/0/1' in rc_before
        _run('jnpr-commit-6', ['configure', 'delete interfaces ge-0/0/1', 'commit'])
        rc_after = _cli('jnpr-commit-6', 'show configuration | display set')
        assert 'ge-0/0/1' not in rc_after


class TestJuniperShowConfiguration:
    def test_show_configuration_hierarchy_braces(self):
        _dev('jnpr-show-1')
        _run('jnpr-show-1', [
            'configure',
            'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24',
            'commit',
        ])
        rc = _cli('jnpr-show-1', 'show configuration')
        assert 'interfaces {' in rc
        assert 'ge-0/0/1 {' in rc
        assert 'address 172.16.5.1/24;' in rc

    def test_show_configuration_display_set_is_flat(self):
        _dev('jnpr-show-2')
        _run('jnpr-show-2', [
            'configure',
            'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24',
            'commit',
        ])
        rc = _cli('jnpr-show-2', 'show configuration | display set')
        assert rc == 'set interfaces ge-0/0/1 unit 0 family inet address 172.16.5.1/24'


class TestJuniperLldp:
    def test_lldp_enabled_by_default_shows_neighbor(self):
        """実機JunosもLLDPは既定で有効。"""
        _dev('jnpr-lldp-1')
        _dev2 = 'jnpr-lldp-1-cisco'
        client.post('/api/device', json={'id': _dev2, 'type': 'cisco', 'hostname': _dev2})
        client.post('/api/link', json={'a': 'jnpr-lldp-1', 'b': _dev2})
        out = _cli('jnpr-lldp-1', 'show lldp neighbors')
        assert 'Total entries displayed: 1' in out
