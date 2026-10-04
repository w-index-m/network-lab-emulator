"""
Yamaha RTX1300 (device_type='yamaha') の最小対応。

ユーザー依頼「Ｗｅｂの画面まで確認して...」「できればルーティングなど
反映してほしいです」への対応。RTXシリーズはCisco/Junosいずれとも異なる
独自コマンド体系(administrator / ip lanN address / ip route ... gateway
.../ nat descriptor / pp select)を持つため、Juniper同様に専用ハンドラ
(_yamaha_process)をengine/rules.pyに新設した。

最大の要望だった「ルーティングの反映」は、他の全機種と同じく
rib_engine(engine/protocols.py)に委譲することで実現している
(CLAUDE.mdが明記する「状態を二重管理せず共有エンジンに委譲する」方針通り):
  - "ip route default gateway <ip>" / "ip route <net>/<prefix> gateway <ip>"
    は app.py の handle_protocol_config に専用正規表現を追加し、
    rib_engine.add_static_route() を直接呼ぶ(rules.py側の
    _yamaha_process はコマンド自体を無言で受理するだけ)。
  - "show ip route" は app.py の handle_protocol_show が device_type
    非依存で既に rib_engine.format_show_ip_route() 相当を返しており、
    Yamaha専用の実装は不要だった(接続ルート・スタティックルート・
    gateway of last resort が実際に反映されて出てくることを確認)。

スコープ(実装した範囲):
  - administrator でexec→adminモード遷移(パスワード検証は簡略化、
    実装していない)
  - ip lanN address <ip>/<prefix> (state.interfaces直接反映)
  - ip route (default|net/prefix) gateway <ip> [weight N] [hide]
    (rib_engineへのstatic route登録。weight/hideによる優先度制御・
    pp番号を次ホップにするマルチホーミングは非対応)
  - show config / show status lanN / show environment
  - nat descriptor type <id> masquerade / ip lanN nat descriptor <id>
    (設定の保存・show configへの反映のみ。実パケット変換は非対応)
  - pp select <N> サブコンテキスト(pppoe use lanN / pp auth myname /
    pp enable 等の設定保存・show configへの反映のみ。実PPPoE
    ネゴシエーションは非対応)
  - save (書き込み確認のみ)

インタフェース名: lan1/lan2/lan3 (GigabitEthernet0/0/NではなくRTX実機通り)
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_):
    client.post('/api/device', json={'id': id_, 'type': 'yamaha', 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()


def _out(id_, cmd):
    return _cli(id_, cmd)['output']


def _run(id_, cmds):
    out = ''
    for cmd in cmds:
        out = _out(id_, cmd)
    return out


class TestYamahaBasics:
    def test_default_interfaces_use_lan_naming(self):
        _dev('rtx-basic-1')
        rc = _out('rtx-basic-1', 'show config')
        assert 'ip lan1 address' in rc
        assert 'ip lan2 address' in rc
        assert 'GigabitEthernet' not in rc

    def test_exec_mode_prompt_is_not_admin(self):
        _dev('rtx-basic-2')
        r = _cli('rtx-basic-2', 'show config')
        assert r['mode'] == 'exec'

    def test_administrator_enters_admin_mode(self):
        _dev('rtx-basic-3')
        r = _cli('rtx-basic-3', 'administrator')
        assert r['mode'] == 'admin'

    def test_exit_from_admin_returns_to_exec(self):
        _dev('rtx-basic-4')
        _cli('rtx-basic-4', 'administrator')
        r = _cli('rtx-basic-4', 'exit')
        assert r['mode'] == 'exec'


class TestYamahaInterfaceConfig:
    def test_ip_lan_address_updates_interface(self):
        _dev('rtx-if-1')
        _run('rtx-if-1', ['administrator', 'ip lan1 address 192.168.77.1/24'])
        rc = _out('rtx-if-1', 'show config')
        assert 'ip lan1 address 192.168.77.1/24' in rc

    def test_show_status_reflects_configured_ip(self):
        _dev('rtx-if-2')
        _run('rtx-if-2', ['administrator', 'ip lan1 address 10.5.5.1/24'])
        out = _out('rtx-if-2', 'show status lan1')
        assert '10.5.5.1/24' in out
        assert 'Link: Up' in out


class TestYamahaRoutingIsReflected:
    """ユーザーの最優先の要望: ルーティングが実際に反映されること。"""

    def test_default_route_appears_in_show_ip_route(self):
        _dev('rtx-route-1')
        _run('rtx-route-1', ['administrator', 'ip route default gateway 192.168.100.254'])
        out = _out('rtx-route-1', 'show ip route')
        assert '192.168.100.254' in out
        assert '0.0.0.0/0' in out

    def test_static_route_appears_in_show_ip_route(self):
        _dev('rtx-route-2')
        _run('rtx-route-2', [
            'administrator',
            'ip lan1 address 10.1.1.1/24',
            'ip route 172.16.0.0/16 gateway 10.1.1.254',
        ])
        out = _out('rtx-route-2', 'show ip route')
        assert '172.16.0.0/16' in out
        assert '10.1.1.254' in out

    def test_connected_routes_for_configured_interfaces_appear(self):
        """スタティックルートだけでなく直結経路もrib_engine経由で出る
        (他の全機種と同じ共有エンジンに乗っていることの確認)。"""
        _dev('rtx-route-3')
        _run('rtx-route-3', ['administrator', 'ip lan1 address 10.9.9.1/24'])
        out = _out('rtx-route-3', 'show ip route')
        assert '10.9.9.0/24' in out
        assert 'directly connected' in out

    def test_route_reflected_in_show_config_too(self):
        _dev('rtx-route-4')
        _run('rtx-route-4', ['administrator', 'ip route default gateway 203.0.113.100'])
        rc = _out('rtx-route-4', 'show config')
        assert 'ip route default gateway 203.0.113.100' in rc

    def test_no_ip_route_removes_the_route(self):
        _dev('rtx-route-5')
        _run('rtx-route-5', [
            'administrator',
            'ip route default gateway 192.168.1.1',
            'no ip route default gateway 192.168.1.1',
        ])
        rc = _out('rtx-route-5', 'show config')
        assert 'ip route default' not in rc


class TestYamahaNatDescriptor:
    def test_nat_descriptor_masquerade_reflected_in_show_config(self):
        _dev('rtx-nat-1')
        _run('rtx-nat-1', [
            'administrator',
            'nat descriptor type 1 masquerade',
            'ip lan2 nat descriptor 1',
        ])
        rc = _out('rtx-nat-1', 'show config')
        assert 'nat descriptor type 1 masquerade' in rc
        assert 'ip lan2 nat descriptor 1' in rc


class TestYamahaPpSelect:
    def test_pp_select_enters_pp_mode(self):
        _dev('rtx-pp-1')
        _cli('rtx-pp-1', 'administrator')
        r = _cli('rtx-pp-1', 'pp select 1')
        assert r['mode'] == 'pp'

    def test_pppoe_config_reflected_in_show_config(self):
        _dev('rtx-pp-2')
        _run('rtx-pp-2', [
            'administrator', 'pp select 1', 'pppoe use lan2',
            'pp auth myname myuser@isp.example mypassword', 'exit',
            'pp enable 1',
        ])
        rc = _out('rtx-pp-2', 'show config')
        assert 'pp select 1' in rc
        assert 'pppoe use lan2' in rc
        assert 'pp auth myname myuser@isp.example mypassword' in rc
        assert 'pp enable 1' in rc

    def test_exit_from_pp_mode_returns_to_admin(self):
        _dev('rtx-pp-3')
        _cli('rtx-pp-3', 'administrator')
        _cli('rtx-pp-3', 'pp select 1')
        r = _cli('rtx-pp-3', 'exit')
        assert r['mode'] == 'admin'


class TestYamahaSave:
    def test_save_is_accepted(self):
        _dev('rtx-save-1')
        _cli('rtx-save-1', 'administrator')
        r = _cli('rtx-save-1', 'save')
        assert r['mode'] == 'admin'
