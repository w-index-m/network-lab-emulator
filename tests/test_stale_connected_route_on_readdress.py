"""
インタフェースのIPを再割り当て(readdress)した際、旧サブネットの
直結ルート(C)が`rib_engine`に残り続ける実バグの回帰テスト。

ユーザー依頼「CatalystとCatalyst2をOSPFで繋いで経路やり取りしたい、
それぞれのCatalystの配下には2セグメントある」というライブ検証中に
発見した: Catalyst2のVlan10を既定値(192.168.10.1/24)から
192.168.30.1/24へ再IPしたところ、`show ip route`に
`192.168.10.0/24`がインタフェース名の空欄のまま(どのインタフェースも
もう所有していないため`_iface_for_network`が解決できない)残り続けた。

原因: `app.py`の`_register_icmp()`は設定変更のたびに「現在の
インタフェースIPから直結ルートを追加」する処理はあったが、古いIPの
直結ルートを削除する処理が無かったため、IP再割り当て後もrib_engineに
古いルートが残っていた。

修正: 現在のインタフェース群から求めた「今も直結しているネットワーク
集合」に含まれない古い直結ルート(ad=0, next_hop='0.0.0.0')を、
再登録前に削除するようにした。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _cli(device_id, cmd):
    return client.post('/api/cli', json={'device_id': device_id, 'command': cmd}).json()['output']


def _dev(id_, type_):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


class TestStaleConnectedRouteRemovedOnReaddress:
    def test_readdressing_an_svi_drops_the_old_connected_route(self):
        dev_id = 'readdr-cat-1'
        _dev(dev_id, 'catalyst')
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.10.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '192.168.10.0/24 is directly connected' in out

        # 再IP(readdress)
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.30.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '192.168.30.0/24 is directly connected, Vlan10' in out
        # 旧サブネットの直結ルートが残っていないこと(regression: 以前は
        # インタフェース名が空欄のまま残り続けていた)
        assert '192.168.10.0/24' not in out

    def test_readdressing_a_physical_interface_drops_the_old_connected_route(self):
        dev_id = 'readdr-cat-2'
        _dev(dev_id, 'catalyst')
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface GigabitEthernet1/0/1')
        _cli(dev_id, 'no switchport')
        _cli(dev_id, 'ip address 192.168.1.1 255.255.255.252')
        _cli(dev_id, 'no shutdown')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '192.168.1.0/30 is directly connected' in out

        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface GigabitEthernet1/0/1')
        _cli(dev_id, 'ip address 192.168.2.1 255.255.255.252')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '192.168.2.0/30 is directly connected, GigabitEthernet1/0/1' in out
        assert '192.168.1.0/30' not in out

    def test_other_interfaces_connected_routes_are_unaffected(self):
        """1つのインタフェースを再IPしても、他のインタフェースの直結
        ルートは影響を受けないこと(regression防止: 全消去になっていない
        か)。"""
        dev_id = 'readdr-cat-3'
        _dev(dev_id, 'catalyst')
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.10.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'vlan 20')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'interface Vlan20')
        _cli(dev_id, 'ip address 192.168.11.1 255.255.255.0')
        _cli(dev_id, 'no shutdown')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        # Vlan10だけ再IP
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.30.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '192.168.30.0/24 is directly connected, Vlan10' in out
        assert '192.168.11.0/24 is directly connected, Vlan20' in out
        assert '192.168.10.0/24' not in out

    def test_static_routes_are_not_affected_by_connected_route_cleanup(self):
        """直結ルート(ad=0)のクリーンアップが、スタティックルート
        (ad>=1)を誤って削除しないこと。"""
        dev_id = 'readdr-cat-4'
        _dev(dev_id, 'catalyst')
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.10.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'ip route 10.77.0.0 255.255.255.0 192.168.10.2')
        _cli(dev_id, 'end')

        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface Vlan10')
        _cli(dev_id, 'ip address 192.168.30.1 255.255.255.0')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        assert '10.77.0.0/24' in out
