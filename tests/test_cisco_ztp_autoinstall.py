"""
Cisco ZTP(AUTOINSTALL) — DHCP option 150(TFTPサーバー)+ option 67
(ブートファイル名)経由の自動プロビジョニング。

https://github.com/vincentbernat/network-lab の`lab-dhcp-ztp`を見て
持ち込んだ機能(ユーザーが「両方」実装してほしいと指定した2機能の
もう1つ、VRRP unicast-peerと合わせて)。実機のAUTOINSTALLは
「起動時にstartup-configが無ければ自動でDHCP→TFTP経由のconfig取得を
試みる」という挙動で、CLIで明示的に叩くコマンドではない。このエミュ
レータには「再起動」の概念が無いため、実機でこのシナリオを再現する
際の実際の手順そのもの(`write erase`でNVRAM/startup-configを空にし、
`reload`で再起動する)に相乗りした — 新しい疑似コマンドを増やさず、
2つの実コマンドの組み合わせをトリガーに使っている。

**実装**: DHCPサーバー役(既存の`ip dhcp pool`機能、`cisco`/`catalyst`
devices)に、既存の汎用`option <code> {ascii|hex|ip} <value>`機構で
`option 150 ip <tftp_ip>`(TFTPサーバーアドレス)と
`option 67 ascii <bootfile>`(ブートファイル名)を設定できる
(コード追加不要、既存の仕組みがそのまま使えた)。実際に配布する
"ファイル"の内容は、新設の`tftp-server config <bootfile>`サブモードで
そのままCLI行としてステージする(実機はflash上のファイルを配るが、
このエミュレータにファイルシステムは無いため、CLIで流し込んだものを
そのまま"ファイル"として保持するpragmaticな方式。`engine/rules.py`の
`CONFIG_SUBMODES`に`config-tftp-file`を1行追加し、このモード中は
exit/end/quit以外の行を一切解釈せず生テキストとしてキャプチャする—
解釈してしまうとステージ中の行がサーバー自身の設定として実際に
適用されてしまうため、app.pyの`handle_protocol_config`側にも同じ
ガードを追加した。二層ディスパッチでapp.py層が常にrule_engineより
先に実行されるため、片方だけガードしても防げない)。

クライアント側は`write erase`→`reload`で、直結隣接からDHCP
option150/67を持つ装置を探し、IP/デフォルトゲートウェイ(pool の
default-router)を実際に割り当て、ステージされたファイルの内容を
`configure terminal`に入ってから本物の`cli_command()`経由で1行ずつ
適用する(= ユーザーが手で打つのと同じ経路を再利用。新しい適用ロジック
は書かない)。見つからない/未staging の場合は何も変更せず、実機の
AUTOINSTALLログ風のエラー行だけを返す(フェイクの成功を作らない)。

Live-verified: サーバーにpool+option150/67+ステージ済みファイル
(hostname変更+新規interface)を設定し、クライアントで`write erase`→
`reload`を実行 → 実際にDHCP払い出しIP(`show running-config`に反映)、
デフォルトゲートウェイ経路、ステージされたhostname変更・新規
interfaceまで全て適用されることを確認。ブートファイル未設定/
サーバーリンクなしの各失敗ケースでも一切変更されないことを確認。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _link(a, b, iface_a, iface_b):
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': iface_a, 'iface_b': iface_b})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


def _out(id_, cmd):
    return _cli(id_, cmd)['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _out(id_, cmd)
    return out


def _setup_server(suffix, network, mask, router, tftp_ip, bootfile, staged_lines):
    srv = f'ztpsrv-{suffix}'
    _dev(srv)
    cmds = [
        'configure terminal',
        f'ip dhcp pool ZTP-{suffix}',
        f'network {network} {mask}',
        f'default-router {router}',
        f'option 150 ip {tftp_ip}',
        f'option 67 ascii {bootfile}',
        'exit',
        f'tftp-server config {bootfile}',
    ] + staged_lines + ['exit']
    _run(srv, cmds)
    return srv


class TestZtpServerStaging:
    def test_dhcp_pool_options_150_and_67_reflected_in_show_dhcp_pool(self):
        srv = _setup_server('stage1', '172.16.20.0', '255.255.255.0',
                             '172.16.20.254', '172.16.20.252',
                             'boot1.txt', ['hostname staged-host'])
        out = _out(srv, 'show ip dhcp pool')
        assert 'Option 150 (ip): 172.16.20.252' in out
        assert 'Option 67 (ascii): boot1.txt' in out

    def test_staged_config_lines_are_captured_verbatim_not_executed(self):
        """ステージ中はサーバー自身の設定として実際には適用されない
        (interfaceを新規追加するような行を混ぜても、サーバー自身の
        show running-configには出ない)こと。"""
        srv = _setup_server('stage2', '172.16.21.0', '255.255.255.0',
                             '172.16.21.254', '172.16.21.252', 'boot2.txt',
                             ['hostname should-not-apply-to-server',
                              'interface GigabitEthernet0/9',
                              'ip address 192.0.2.1 255.255.255.0'])
        rc = _out(srv, 'show running-config')
        assert 'should-not-apply-to-server' not in rc
        assert 'GigabitEthernet0/9' not in rc
        state = app_module.device_sessions[srv]
        assert state.tftp_files['boot2.txt'] == [
            'hostname should-not-apply-to-server',
            'interface GigabitEthernet0/9',
            'ip address 192.0.2.1 255.255.255.0',
        ]


class TestZtpClientAutoinstallSucceeds:
    def test_write_erase_then_reload_assigns_dhcp_address_and_default_route(self):
        srv = _setup_server('ok1', '172.16.22.0', '255.255.255.0',
                             '172.16.22.254', '172.16.22.252', 'boot-ok1.txt',
                             ['hostname ztp-applied-ok1'])
        cli = 'ztpcli-ok1'
        _dev(cli)
        _link(srv, cli, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        _run(cli, ['write erase'])
        reload_out = _out(cli, 'reload')
        assert 'AUTOINSTALL' in reload_out
        assert '[OK -' in reload_out

        rc = _out(cli, 'show running-config')
        assert '172.16.22.' in rc            # DHCPで払い出されたサブネット内のIP
        assert 'ip route 0.0.0.0 0.0.0.0 172.16.22.254' in rc

    def test_staged_config_is_actually_applied_to_the_client(self):
        srv = _setup_server('ok2', '172.16.23.0', '255.255.255.0',
                             '172.16.23.254', '172.16.23.252', 'boot-ok2.txt',
                             ['hostname ztp-applied-ok2',
                              'interface GigabitEthernet0/5',
                              'ip address 10.5.5.5 255.255.255.0',
                              'no shutdown'])
        cli = 'ztpcli-ok2'
        _dev(cli)
        _link(srv, cli, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        _run(cli, ['write erase'])
        _out(cli, 'reload')

        rc = _out(cli, 'show running-config')
        assert 'hostname ztp-applied-ok2' in rc
        assert 'GigabitEthernet0/5' in rc
        assert '10.5.5.5' in rc


class TestZtpClientAutoinstallFails:
    def test_no_server_linked_reports_no_server_and_changes_nothing(self):
        cli = 'ztpcli-fail1'
        _dev(cli)
        before = _out(cli, 'show running-config')
        _run(cli, ['write erase'])
        reload_out = _out(cli, 'reload')
        assert 'AUTOINSTALL-3-NO_SERVER' in reload_out
        after = _out(cli, 'show running-config')

        def _strip_byte_count(text):
            return '\n'.join(l for l in text.splitlines()
                              if not l.startswith('Current configuration'))
        assert _strip_byte_count(before) == _strip_byte_count(after)

    def test_dhcp_pool_without_bootfile_reports_no_bootfile_but_still_assigns_ip(self):
        """option 67(ブートファイル)が設定されていなければ、実機同様
        アドレスの割り当てまでは行われるがファイル取得はできない。"""
        srv_id = 'ztpsrv-nobf'
        _dev(srv_id)
        _run(srv_id, ['configure terminal', 'ip dhcp pool NOBF',
                      'network 172.16.24.0 255.255.255.0',
                      'default-router 172.16.24.254',
                      'option 150 ip 172.16.24.252', 'exit'])
        cli = 'ztpcli-nobf'
        _dev(cli)
        _link(srv_id, cli, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        _run(cli, ['write erase'])
        reload_out = _out(cli, 'reload')
        assert 'AUTOINSTALL-3-NO_BOOTFILE' in reload_out

    def test_bootfile_configured_but_never_staged_reports_file_not_found(self):
        srv_id = 'ztpsrv-nofile'
        _dev(srv_id)
        _run(srv_id, ['configure terminal', 'ip dhcp pool NOFILE',
                      'network 172.16.25.0 255.255.255.0',
                      'default-router 172.16.25.254',
                      'option 150 ip 172.16.25.252',
                      'option 67 ascii never-staged.txt', 'exit'])
        cli = 'ztpcli-nofile'
        _dev(cli)
        _link(srv_id, cli, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        _run(cli, ['write erase'])
        reload_out = _out(cli, 'reload')
        assert 'AUTOINSTALL-3-FILE_NOT_FOUND' in reload_out

    def test_reload_without_write_erase_first_does_not_trigger_ztp(self):
        """実機同様、startup-configが(write eraseされずに)残っていれば
        AUTOINSTALLは走らない — 既存の素の'reload'応答のままになる。"""
        srv_id = 'ztpsrv-noerase'
        _dev(srv_id)
        _run(srv_id, ['configure terminal', 'ip dhcp pool P',
                      'network 172.16.26.0 255.255.255.0',
                      'default-router 172.16.26.254',
                      'option 150 ip 172.16.26.252',
                      'option 67 ascii x.txt', 'exit',
                      'tftp-server config x.txt', 'hostname should-not-apply', 'exit'])
        cli = 'ztpcli-noerase'
        _dev(cli)
        _link(srv_id, cli, 'GigabitEthernet0/0', 'GigabitEthernet0/0')
        reload_out = _out(cli, 'reload')
        assert 'AUTOINSTALL' not in reload_out
        rc = _out(cli, 'show running-config')
        assert 'should-not-apply' not in rc
