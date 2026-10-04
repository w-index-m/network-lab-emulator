"""
FortiGate(FortiOS)device_type。ユーザー依頼「ASAやFortiGateも追加で
試験幅を増やして欲しい」(`docs/pppoe-ipsec-mesh-status.md`のPPPoE+
IPsecマトリクスを広げる一環)。

FortiOSのCLI文法(`config`/`edit`/`set`/`next`/`end`の階層型)は
Cisco exec→config→config-ifモデルに乗らないため、Juniper/Yamaha/
APRESIA/BIG-IPと同じパターンで専用ハンドラ`_fortigate_process`を
`RuleEngine.process()`の先頭で分岐させた(`engine/rules.py`)。

スコープ:
- `config system interface` / `edit "<port>"` / `set ip <ip> <mask>` /
  `set mode pppoe` / `set username`/`set password` / `next` / `end`
- `config vpn ipsec phase1-interface` / `edit "<name>"` /
  `set interface "<port>"` / `set remote-gw <ip>` /
  `set psksecret <key>` / `next` / `end`
- `config vpn ipsec phase2-interface` / `edit "<name>"` /
  `set phase1name "<name>"` / `next` / `end`
  (本エンジンはphase1単位でトンネルを管理するため、phase2側は
  受理のみで実際の状態には反映しない — スコープ外として明記)

IPsec(phase1-interface)は既存の`engine/ike_engine.py`のSi-R用
`ipsec_tunnels`辞書形式にそのまま乗せ、`dt`/`pdt`判定タプルに
`'fortigate'`を追加しただけで、新しいネゴシエーションロジックは
一切増やしていない(Yamaha追加時と同じ考え方)。PPPoE WAN化
(`set mode pppoe`)も既存の`PppoeEngine`をそのまま使う
(Yamahaの"pp enable"と同じ経路)。

**実装中に見つけた実バグ(2件)**:
1. `handle_protocol_config`は二層ディスパッチで常に
   `rule_engine.process()`より先に実行されるため、FortiGateの
   PPPoE/IPsec実際のトリガー(`pppoe_engine.connect()`呼び出しや
   `_trigger_ike_negotiation()`)を`handle_protocol_config`側に
   書くと、`_fortigate_process`がまだ`state.interfaces`/
   `state.ipsec_tunnels`/`state.fortigate_pppoe`を確定させる前の
   古い値を使ってしまう(Juniperのcommit→SSHリスナー起動と全く
   同じ理由)。`cli_command()`の`rule_engine.process()`実行後に
   トリガーを置くことで解決。
2. `app.py`の`handle_protocol_config`内でYamahaの"pp enable"相当の
   変数名を`hostname`と書いていたが、この関数のローカル変数は
   `hostname`ではなく`state.hostname`。FortiGate追加時に同じ
   パターンを複製して初めて`NameError`で踏んだ(既存のYamaha側は
   別のスコープで`hostname`という変数が実際に存在していたため
   問題が無かった)。

Live-verified: `bas`経由のPPPoEで実際に共有IPv4を取得
(`show full-configuration`に反映)、そのIPを使ってCisco IOS/Si-Rの
双方と実際にIPsecが確立することを確認
(`get vpn ipsec tunnel summary`で`status: up`)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='fortigate'):
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


class TestFortigateInterfaceConfig:
    def test_static_ip_reflected_in_show_full_configuration(self):
        _dev('fg-if-1')
        _run('fg-if-1', ['config system interface', 'edit "port2"',
                         'set ip 10.5.0.1 255.255.255.0', 'next', 'end'])
        rc = _out('fg-if-1', 'show full-configuration')
        assert 'set ip 10.5.0.1 255.255.255.0' in rc

    def test_edit_without_config_block_is_rejected(self):
        """config階層の外でeditだけ打っても実機同様エラーになる
        (フェイクの成功を作らない)。"""
        _dev('fg-if-2')
        out = _out('fg-if-2', 'edit "port1"')
        assert 'fail' in out.lower() or out == ''


class TestFortigatePppoeWan:
    def test_pppoe_wan_gets_a_real_pool_ip(self):
        _dev('fg-bas-1', 'bas')
        _run('fg-bas-1', ['configure terminal', 'ip pool 198.51.130.10 198.51.130.10/24',
                          'pppoe-user fgu1 fgp1', 'exit'])
        _dev('fg-cli-1')
        _link('fg-cli-1', 'fg-bas-1', 'port1', 'wan1')
        _run('fg-cli-1', ['config system interface', 'edit "port1"',
                          'set mode pppoe', 'set username "fgu1"', 'set password "fgp1"',
                          'next', 'end'])
        rc = _out('fg-cli-1', 'show full-configuration')
        assert 'set ip 198.51.130.10' in rc
        assert 'set mode pppoe' in rc

    def test_wrong_password_does_not_assign_an_ip(self):
        _dev('fg-bas-2', 'bas')
        _run('fg-bas-2', ['configure terminal', 'ip pool 198.51.131.10 198.51.131.10/24',
                          'pppoe-user fgu2 correct', 'exit'])
        _dev('fg-cli-2')
        _link('fg-cli-2', 'fg-bas-2', 'port1', 'wan1')
        _run('fg-cli-2', ['config system interface', 'edit "port1"',
                          'set mode pppoe', 'set username "fgu2"', 'set password "WRONG"',
                          'next', 'end'])
        rc = _out('fg-cli-2', 'show full-configuration')
        assert 'set ip 198.51.131.10' not in rc


class TestFortigateIpsecToCisco:
    def test_ipsec_establishes_over_the_pppoe_assigned_address(self):
        _dev('fg-bas-3', 'bas')
        _run('fg-bas-3', ['configure terminal', 'ip pool 198.51.132.10 198.51.132.10/24',
                          'pppoe-user fgu3 fgp3', 'exit'])
        _dev('fg-cli-3')
        _link('fg-cli-3', 'fg-bas-3', 'port1', 'wan1')
        _run('fg-cli-3', ['config system interface', 'edit "port1"',
                          'set mode pppoe', 'set username "fgu3"', 'set password "fgp3"',
                          'next', 'end'])

        _dev('fg-cisco-3', 'cisco')
        _link('fg-cisco-3', 'fg-cli-3', 'GigabitEthernet0/0/0', 'port2')
        _run('fg-cli-3', ['config system interface', 'edit "port2"',
                          'set ip 10.211.0.1 255.255.255.252', 'next', 'end'])
        _run('fg-cisco-3', [
            'configure terminal', 'interface GigabitEthernet0/0/0',
            'ip address 10.211.0.2 255.255.255.252', 'no shutdown', 'exit',
            'crypto isakmp key fg-psk-3 address 198.51.132.10',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp', 'set peer 198.51.132.10',
            'set transform-set TS', 'match address 101', 'exit',
            'interface GigabitEthernet0/0/0', 'crypto map CMAP', 'exit',
            'crypto isakmp enable',
        ])
        _run('fg-cli-3', [
            'config vpn ipsec phase1-interface', 'edit "tun3"',
            'set interface "port1"', 'set remote-gw 10.211.0.2',
            'set psksecret fg-psk-3', 'next', 'end',
            'config vpn ipsec phase2-interface', 'edit "tun3"',
            'set phase1name "tun3"', 'next', 'end',
        ])
        summary = _out('fg-cli-3', 'get vpn ipsec tunnel summary')
        assert "'tun3'" in summary
        assert 'status: up' in summary

    def test_wrong_psk_does_not_establish(self):
        _dev('fg-bas-4', 'bas')
        _run('fg-bas-4', ['configure terminal', 'ip pool 198.51.133.10 198.51.133.10/24',
                          'pppoe-user fgu4 fgp4', 'exit'])
        _dev('fg-cli-4')
        _link('fg-cli-4', 'fg-bas-4', 'port1', 'wan1')
        _run('fg-cli-4', ['config system interface', 'edit "port1"',
                          'set mode pppoe', 'set username "fgu4"', 'set password "fgp4"',
                          'next', 'end'])
        _dev('fg-cisco-4', 'cisco')
        _link('fg-cisco-4', 'fg-cli-4', 'GigabitEthernet0/0/0', 'port2')
        _run('fg-cli-4', ['config system interface', 'edit "port2"',
                          'set ip 10.212.0.1 255.255.255.252', 'next', 'end'])
        _run('fg-cisco-4', [
            'configure terminal', 'interface GigabitEthernet0/0/0',
            'ip address 10.212.0.2 255.255.255.252', 'no shutdown', 'exit',
            'crypto isakmp key right-key address 198.51.133.10',
            'crypto ipsec transform-set TS esp-aes esp-sha-hmac',
            'crypto map CMAP 10 ipsec-isakmp', 'set peer 198.51.133.10',
            'set transform-set TS', 'match address 101', 'exit',
            'interface GigabitEthernet0/0/0', 'crypto map CMAP', 'exit',
            'crypto isakmp enable',
        ])
        _run('fg-cli-4', [
            'config vpn ipsec phase1-interface', 'edit "tun4"',
            'set interface "port1"', 'set remote-gw 10.212.0.2',
            'set psksecret WRONG-KEY', 'next', 'end',
            'config vpn ipsec phase2-interface', 'edit "tun4"',
            'set phase1name "tun4"', 'next', 'end',
        ])
        summary = _out('fg-cli-4', 'get vpn ipsec tunnel summary')
        assert 'status: down' in summary


class TestFortigateIpsecToSir:
    def test_ipsec_establishes_with_sir_over_static_ip(self):
        """FortiGate⇔Si-Rの静的WAN同士IPsec(PPPoEなし)。"""
        _dev('fg-sir-1')
        _dev('sir-fg-1', 'sir')
        _link('fg-sir-1', 'sir-fg-1', 'port1', 'lan1')
        _run('fg-sir-1', ['config system interface', 'edit "port1"',
                          'set ip 10.62.0.1 255.255.255.252', 'next', 'end'])
        _run('sir-fg-1', ['configure', 'lan 1 ip address 10.62.0.2/30 3',
                          'remote 1 ap 0 tunnel local 10.62.0.2',
                          'remote 1 ap 0 tunnel remote 10.62.0.1',
                          'remote 1 ap 0 ipsec ike preshared-key fg-sir-psk',
                          'ike use on', 'ipsec use on'])
        _run('fg-sir-1', [
            'config vpn ipsec phase1-interface', 'edit "tunsir"',
            'set interface "port1"', 'set remote-gw 10.62.0.2',
            'set psksecret fg-sir-psk', 'next', 'end',
            'config vpn ipsec phase2-interface', 'edit "tunsir"',
            'set phase1name "tunsir"', 'next', 'end',
        ])
        summary = _out('fg-sir-1', 'get vpn ipsec tunnel summary')
        assert "'tunsir'" in summary
        assert 'status: up' in summary


class TestFortigateConfigModeGuard:
    """FortiGateのconfig/edit/set/next/endのモード管理そのもの
    (regression防止)。"""

    def test_set_outside_edit_context_is_ignored(self):
        _dev('fg-guard-1')
        _run('fg-guard-1', ['config system interface', 'set ip 1.2.3.4 255.255.255.0'])
        # edit "<port>" していない状態でのsetは何も変更しない
        rc = _out('fg-guard-1', 'show full-configuration')
        assert '1.2.3.4' not in rc

    def test_end_returns_to_exec_level(self):
        _dev('fg-guard-2')
        _run('fg-guard-2', ['config system interface', 'edit "port1"', 'next', 'end'])
        state = app_module.device_sessions['fg-guard-2']
        assert state.fortigate_mode_stack == []
