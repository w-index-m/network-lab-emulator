"""
擬似FLETS網 — device_type='bas'(PPPoEアクセスコンセントレータ/収容局相当)
とYamaha RTXの"pp select"を実際に接続する最小対応。

ユーザー依頼「擬似フレッツ網も作れますか？」「擬似ふれっつ網と繋ぎたいと
言えば繋がる？」への回答: いいえ、設定を投入するだけでは繋がらず
(相手役のBASが実在しないと本物同様PPPoEは確立しない)、実際に
`engine.protocols.PppoeEngine`を実装し、RTXの"pp enable"が本当に
認証・IP払い出しまで行うようにした。

**アーキテクチャ上の制約(実装前にユーザーへ説明し合意を得た点)**:
実PPPoEのDiscovery段階(PADI/PADO/PADS)はEthernetフレームそのもの
(Ethertype 0x8863、IPヘッダすら無い)を使うが、このエミュレータの装置は
ループバックIPエイリアスで繋がっているだけで実L2隣接を持たないため、
本物のEthernetフレームとしては再現できない。代わりに、既存の
OspfEngine/RipEngineと同じ「vnet.links/device_typesを見てソフトウェア的
にネゴシエーションする」方式を採用し、LCP/PAP・CHAP認証/IPCPのIP払い出し
という段階そのものは忠実に再現する(PppoeEngine.connect())。

テスト対象のエンドツーエンド経路:
  1. BASに `ip pool <start> <end>/<prefix>` でプールを、
     `pppoe-user <name> <password>` で認証ユーザーを設定
  2. RTXに `pp select <N>` → `pppoe use lanN` → `pp auth myname <user> <pw>`
  3. `/api/link` でRTXのpppoe_lan用インタフェースとBASを接続
  4. `pp enable <N>` で実際に認証チェック・IPプールからの払い出しが行われ、
     成功すれば state.interfaces['pp<N>'] に実IPが入り、
     `ip route default gateway pp <N>` を設定済みなら、払い出されたgateway
     (BAS自身のIP)で実際に rib_engine に既定経路が登録される
     (show ip route / show config の両方に反映)。
  5. 認証失敗・リンク未接続では一切反映されないことも固定する。

「NAT記述子・PPPoE設定の受理と表示のみ」という以前のスコープのうち、
PPPoEのネゴシエーション部分はこのテストで実際に実装範囲に入った
(NAT記述子は引き続き受理・表示のみ)。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_):
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


def _setup_bas(bas_id, pool_start, pool_end, prefix, user, password):
    _dev(bas_id, 'bas')
    _run(bas_id, [
        'configure terminal',
        f'ip pool {pool_start} {pool_end}/{prefix}',
        f'pppoe-user {user} {password}',
    ])


def _setup_rtx_pp(rtx_id, pppoe_lan, pp_id, user, password, default_route=True):
    _dev(rtx_id, 'yamaha')
    cmds = ['administrator']
    if default_route:
        cmds.append(f'ip route default gateway pp {pp_id}')
    cmds += [
        f'pp select {pp_id}',
        f'pppoe use {pppoe_lan}',
        f'pp auth myname {user} {password}',
        'exit',
    ]
    _run(rtx_id, cmds)


class TestBasBasics:
    def test_bas_interfaces_use_wan_naming(self):
        _dev('bas-basic-1', 'bas')
        rc = _out('bas-basic-1', 'show running-config')
        assert 'ip wan1 address' in rc

    def test_bas_pool_and_user_reflected_in_show_config(self):
        _setup_bas('bas-basic-2', '198.51.100.10', '198.51.100.20', 16, 'alice', 'p@ss1')
        rc = _out('bas-basic-2', 'show running-config')
        assert 'ip pool 198.51.100.10 198.51.100.20/16' in rc
        assert 'pppoe-user alice p@ss1' in rc


class TestPppoeConnectSucceeds:
    def test_pp_enable_assigns_an_ip_from_the_bas_pool(self):
        _setup_bas('bas-ok-1', '100.64.1.10', '100.64.1.20', 16, 'user1', 'pass1')
        _setup_rtx_pp('rtx-ok-1', 'lan2', 1, 'user1', 'pass1', default_route=False)
        _link('rtx-ok-1', 'bas-ok-1', 'lan2', 'wan1')
        _out('rtx-ok-1', 'pp enable 1')
        rc = _out('rtx-ok-1', 'show config')
        assert 'ip pp1 address 100.64.1.10/16' in rc

    def test_bas_sees_the_session(self):
        _setup_bas('bas-ok-2', '100.64.2.10', '100.64.2.20', 16, 'user2', 'pass2')
        _setup_rtx_pp('rtx-ok-2', 'lan2', 1, 'user2', 'pass2', default_route=False)
        _link('rtx-ok-2', 'bas-ok-2', 'lan2', 'wan1')
        _out('rtx-ok-2', 'pp enable 1')
        sessions = _out('bas-ok-2', 'show pppoe session')
        assert 'rtx-ok-2' in sessions
        assert '100.64.2.10' in sessions

    def test_default_route_via_pp_uses_the_assigned_bas_gateway(self):
        """ユーザーの最優先の要望(ルーティングが実際に反映される)の
        PPPoE版: "ip route default gateway pp N" が実際に繋がった
        gatewayで rib_engine に登録されること。"""
        _setup_bas('bas-ok-3', '100.64.3.10', '100.64.3.20', 16, 'user3', 'pass3')
        _setup_rtx_pp('rtx-ok-3', 'lan2', 1, 'user3', 'pass3', default_route=True)
        _link('rtx-ok-3', 'bas-ok-3', 'lan2', 'wan1')
        _out('rtx-ok-3', 'pp enable 1')
        rc = _out('rtx-ok-3', 'show ip route')
        assert '0.0.0.0/0' in rc
        # gatewayはBASの実インタフェースIP(既定 100.64.0.254)
        assert '100.64.0.254' in rc
        cfg = _out('rtx-ok-3', 'show config')
        assert 'ip route default gateway 100.64.0.254' in cfg

    def test_pp_disable_removes_interface_and_route(self):
        _setup_bas('bas-ok-4', '100.64.4.10', '100.64.4.20', 16, 'user4', 'pass4')
        _setup_rtx_pp('rtx-ok-4', 'lan2', 1, 'user4', 'pass4', default_route=True)
        _link('rtx-ok-4', 'bas-ok-4', 'lan2', 'wan1')
        _out('rtx-ok-4', 'pp enable 1')
        _out('rtx-ok-4', 'pp disable 1')
        rc = _out('rtx-ok-4', 'show config')
        assert 'ip pp1' not in rc
        assert 'ip route default' not in rc


class TestPppoeConnectFails:
    def test_wrong_password_does_not_assign_an_ip(self):
        _setup_bas('bas-fail-1', '100.64.5.10', '100.64.5.20', 16, 'user5', 'correct')
        _setup_rtx_pp('rtx-fail-1', 'lan2', 1, 'user5', 'WRONG', default_route=False)
        _link('rtx-fail-1', 'bas-fail-1', 'lan2', 'wan1')
        _out('rtx-fail-1', 'pp enable 1')
        rc = _out('rtx-fail-1', 'show config')
        assert 'ip pp1' not in rc

    def test_no_bas_linked_does_not_assign_an_ip(self):
        _setup_rtx_pp('rtx-fail-2', 'lan2', 1, 'user6', 'pass6', default_route=False)
        _out('rtx-fail-2', 'pp enable 1')
        rc = _out('rtx-fail-2', 'show config')
        assert 'ip pp1' not in rc

    def test_linked_to_a_non_bas_device_does_not_assign_an_ip(self):
        _dev('cisco-notbas-1', 'cisco')
        _setup_rtx_pp('rtx-fail-3', 'lan2', 1, 'user7', 'pass7', default_route=False)
        _link('rtx-fail-3', 'cisco-notbas-1', 'lan2', 'GigabitEthernet0/0')
        _out('rtx-fail-3', 'pp enable 1')
        rc = _out('rtx-fail-3', 'show config')
        assert 'ip pp1' not in rc
