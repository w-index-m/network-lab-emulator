"""
v6プラス等のIPoE+MAP-E方式でのIPv4インターネット接続。ユーザー依頼:
「ちなみに CiscoやYAMAHAからIPOEでの接続も検証できますか？」→
「v6プラスを想定してます」→「想定コンフィグを元に接続可能かを試験できる
ようにしたい。もちろんAI側でコンフィグを作って試験もして欲しい」。

**アーキテクチャ上の制約(実装前にユーザーへ説明し合意を得た点)**:
このエミュレータの共有エンジン(rib_engine/icmp_engine)はIPv4専用で、
IPv6アドレッシング/ルーティングの実体を持たない
(engine/protocols.pyに"ipv6"の実装は0件)。そのため実際のv6プラスの
流れのうち「IPv6 IPoEアクセス」部分(DHCPv6-PDでのプレフィックス取得等)
は見た目の設定反映のみ(state.yamaha_ipv6_prefix)にとどめ、「MAP-Eで
実際にIPv4インターネットに出られる」部分だけは、PppoeEngineと同じ方式
(vnet経由のソフトウェア的ネゴシエーション、実パケットではない)で実際の
状態遷移として再現する(engine.protocols.MapEEngine)。払い出された
共有IPv4アドレスは本物のstate.interfaces['map0']/rib_engineに乗るため、
既存のIPv4 ping/tracerouteがそのまま使える。

MAP-Eルールサーバー役は新しいdevice_typeを増やさず、既存の擬似FLETS
`bas`デバイスに相乗りさせた(PPPoE-BASもMAP-Eルールサーバーも「契約者が
繋ぐISP収容設備」という点で同じ、という判断)。

実際にCisco(別装置)からMAP-E払い出しIPv4アドレスへping/tracerouteが
通ることを対話的に(TestClientスクリプトで)確認済み:
  ping 203.0.116.50 → Success rate is 100 percent (5/5)
  traceroute 203.0.116.50 → 1 rtx-v6p-4 (10.200.0.1)
このテストファイルはその経路を固定するもので、show config/show ip route/
state.interfacesの内容で判定する(test_pseudo_flets_pppoe.py/
test_yamaha_cisco_ipsec_over_pppoe.pyと同じ方針。テスト自体はライブ
pingを再実行しない)。
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


def _setup_rule_server(bas_id, pool_start, pool_end, br_ipv6):
    _dev(bas_id, 'bas')
    _run(bas_id, [
        'configure terminal',
        f'map-e ipv4-pool {pool_start} {pool_end}',
        f'map-e br-address {br_ipv6}',
    ])


def _setup_rtx(rtx_id, ipv6_prefix=None):
    _dev(rtx_id, 'yamaha')
    cmds = ['administrator']
    if ipv6_prefix:
        cmds.append(f'ipv6 prefix {ipv6_prefix}')
    _run(rtx_id, cmds)


class TestMapERuleServerBasics:
    def test_pool_and_br_address_reflected_in_show_running_config(self):
        _setup_rule_server('bas-mape-1', '203.0.116.10', '203.0.116.20', '2001:db8:fff::1')
        rc = _out('bas-mape-1', 'show running-config')
        assert 'map-e ipv4-pool 203.0.116.10 203.0.116.20' in rc
        assert 'map-e br-address 2001:db8:fff::1' in rc


class TestMapEConnectSucceeds:
    def test_map_e_use_assigns_a_shared_ipv4_from_the_pool(self):
        _setup_rule_server('bas-mape-2', '203.0.117.10', '203.0.117.20', '2001:db8:fff::2')
        _setup_rtx('rtx-mape-2', ipv6_prefix='dhcp-prefix@lan2::/64')
        _link('rtx-mape-2', 'bas-mape-2', 'lan2', 'wan1')
        _out('rtx-mape-2', 'map-e use lan2')
        rc = _out('rtx-mape-2', 'show config')
        assert 'ip map0 address 203.0.117.10/32' in rc
        assert 'ipv6 prefix dhcp-prefix@lan2::/64' in rc
        assert 'map-e use lan2' in rc

    def test_show_map_e_reports_assigned_ipv4_port_range_psid_and_br(self):
        _setup_rule_server('bas-mape-3', '203.0.118.10', '203.0.118.20', '2001:db8:fff::3')
        _setup_rtx('rtx-mape-3')
        _link('rtx-mape-3', 'bas-mape-3', 'lan2', 'wan1')
        _out('rtx-mape-3', 'map-e use lan2')
        status = _out('rtx-mape-3', 'show map-e')
        assert '203.0.118.10' in status
        assert 'PSID=0' in status
        assert '2001:db8:fff::3' in status

    def test_bas_sees_the_rule(self):
        _setup_rule_server('bas-mape-4', '203.0.119.10', '203.0.119.20', '2001:db8:fff::4')
        _setup_rtx('rtx-mape-4')
        _link('rtx-mape-4', 'bas-mape-4', 'lan2', 'wan1')
        _out('rtx-mape-4', 'map-e use lan2')
        rules = _out('bas-mape-4', 'show map-e rules')
        assert 'rtx-mape-4' in rules
        assert '203.0.119.10' in rules

    def test_assigned_ipv4_is_reachable_through_the_connected_route(self):
        """ユーザーの最優先の要望(ルーティングが実際に反映される)の
        MAP-E版: 払い出されたmap0インタフェースのIPが実際にrib_engine上の
        connected routeとしてshow ip routeに現れること(実機でも同様に、
        map0インタフェースがupすれば/32の接続経路が入る)。"""
        _setup_rule_server('bas-mape-5', '203.0.120.10', '203.0.120.20', '2001:db8:fff::5')
        _setup_rtx('rtx-mape-5')
        _link('rtx-mape-5', 'bas-mape-5', 'lan2', 'wan1')
        _out('rtx-mape-5', 'map-e use lan2')
        rc = _out('rtx-mape-5', 'show ip route')
        assert '203.0.120.10' in rc
        state = app_module.device_sessions['rtx-mape-5']
        assert state.interfaces['map0']['ip'] == '203.0.120.10'
        assert state.interfaces['map0']['status'] == 'up'

    def test_no_map_e_use_tears_down_the_interface(self):
        _setup_rule_server('bas-mape-6', '203.0.121.10', '203.0.121.20', '2001:db8:fff::6')
        _setup_rtx('rtx-mape-6')
        _link('rtx-mape-6', 'bas-mape-6', 'lan2', 'wan1')
        _out('rtx-mape-6', 'map-e use lan2')
        assert 'ip map0' in _out('rtx-mape-6', 'show config')
        _out('rtx-mape-6', 'no map-e use lan2')
        rc = _out('rtx-mape-6', 'show config')
        assert 'ip map0' not in rc
        state = app_module.device_sessions['rtx-mape-6']
        assert 'map0' not in state.interfaces


class TestMapEConnectFails:
    def test_no_rule_server_linked_does_not_assign_an_address(self):
        _setup_rtx('rtx-mape-fail-1')
        _out('rtx-mape-fail-1', 'map-e use lan2')
        rc = _out('rtx-mape-fail-1', 'show config')
        assert 'ip map0' not in rc

    def test_linked_to_a_non_bas_device_does_not_assign_an_address(self):
        _dev('cisco-notbas-mape-1', 'cisco')
        _setup_rtx('rtx-mape-fail-2')
        _link('rtx-mape-fail-2', 'cisco-notbas-mape-1', 'lan2', 'GigabitEthernet0/0')
        _out('rtx-mape-fail-2', 'map-e use lan2')
        rc = _out('rtx-mape-fail-2', 'show config')
        assert 'ip map0' not in rc

    def test_bas_without_pool_configured_does_not_assign_an_address(self):
        _dev('bas-mape-nopool', 'bas')
        _setup_rtx('rtx-mape-fail-3')
        _link('rtx-mape-fail-3', 'bas-mape-nopool', 'lan2', 'wan1')
        _out('rtx-mape-fail-3', 'map-e use lan2')
        rc = _out('rtx-mape-fail-3', 'show config')
        assert 'ip map0' not in rc
