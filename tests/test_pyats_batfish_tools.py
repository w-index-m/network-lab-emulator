"""
tools/pyats_testbed.py・tools/batfish_snapshot.py のテスト。

外部ツール(pyATS/Genie・pybatfish+Batfishコンテナ)に依存しない部分
(testbed生成・show ip route のパース・経路差分)は常に実行する。
Genie/pybatfish を使う結合部分は、それらが入っている環境でだけ実行する
(このリポジトリでは requirements に入れず、手動/オプションの検証扱い。
docs/external-tools-pyats-gobgp-gnmic-batfish.md 参照)。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools import batfish_snapshot as bfs  # noqa: E402
from tools import pyats_testbed as pt  # noqa: E402

ROUTE_OUT = """Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       O - OSPF, IA - OSPF inter area

Gateway of last resort is not set

C        10.227.1.0/24 is directly connected, GigabitEthernet0/0/1
L        10.227.1.1/32 is directly connected, GigabitEthernet0/0/1
S        10.227.50.0/24 [1/0] via 10.227.1.254, GigabitEthernet0/0/1
O IA     10.227.2.0/24 [110/20] via 10.227.12.2, GigabitEthernet0/0/0
O E2     172.16.0.0/16 [110/20] via 10.227.12.2, GigabitEthernet0/0/0
S*       0.0.0.0/0 [1/0] via 10.227.1.254
"""


class TestBatfishSnapshotHelpers:
    def test_parse_show_ip_route(self):
        r = bfs.parse_show_ip_route(ROUTE_OUT)
        assert ('10.227.1.0/24', 'connected') in r
        assert ('10.227.1.1/32', 'local') in r
        assert ('10.227.50.0/24', 'static') in r
        assert ('10.227.2.0/24', 'ospf') in r      # O IA
        assert ('172.16.0.0/16', 'ospf') in r      # O E2
        assert ('0.0.0.0/0', 'static') in r        # S*

    def test_parse_classful_heading_form(self):
        txt = ("      10.0.0.0/8 is variably subnetted, 2 subnets, 2 masks\n"
               "C        10.1.1.0 is directly connected, Gi0/0\n")
        assert ('10.1.1.0/8', 'connected') in bfs.parse_show_ip_route(txt)

    def test_diff_routes_normalizes_ospf_subtypes(self):
        emu = {('10.0.2.0/24', 'ospf'), ('10.0.1.0/24', 'connected')}
        bf = {('10.0.2.0/24', 'ospfIA'), ('10.0.1.0/24', 'connected'), ('10.9.0.0/24', 'static')}
        d = bfs.diff_routes(emu, bf)
        assert d['only_emulator'] == []
        assert d['only_batfish'] == [('10.9.0.0/24', 'static')]
        assert len(d['common']) == 2

    def test_batfish_node_name_from_hostname(self):
        assert bfs.batfish_node_name('!\nhostname BF-R1\n!', 'dev') == 'bf-r1'
        assert bfs.batfish_node_name('no hostname here', 'Dev-X') == 'dev-x'


class TestPyatsTestbedHelpers:
    def test_build_testbed(self):
        tb = pt.build_testbed({'R1': {'type': 'cisco', 'ip': '10.0.0.1'},
                               'SW1': {'type': 'catalyst', 'ip': '10.0.0.2', 'port': 2222},
                               'N1': {'type': 'nexus', 'ip': '10.0.0.3'}}, 'admin', 'pw')
        d = tb['devices']
        assert d['R1']['os'] == 'iosxe' and d['R1']['type'] == 'router'
        assert d['SW1']['connections']['cli']['port'] == 2222
        assert d['N1']['os'] == 'nxos'
        assert d['R1']['credentials']['default'] == {'username': 'admin', 'password': 'pw'}

    def test_to_yaml_roundtrip(self):
        yaml = pytest.importorskip('yaml')
        tb = pt.build_testbed({'R1': {'type': 'cisco', 'ip': '10.0.0.1'}}, 'admin', 'p"w:x')
        assert yaml.safe_load(pt.to_yaml(tb)) == tb


class TestGenieParsesEmulatorOutput:
    """Genieのパーサに、エミュレータが実際に返すshow出力をそのまま食わせる
    (SSH接続なし。Device.parse(output=...) を使う)"""

    @pytest.fixture(scope='class')
    def outputs(self):
        os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
        pytest.importorskip('genie.libs.parser')
        from fastapi.testclient import TestClient
        import app as app_module
        with TestClient(app_module.app) as c:
            dev = 'genie-parse-r1'
            c.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': 'GENIE-R1'})

            def cli(cmd):
                return c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
            for cmd in ('configure terminal', 'interface GigabitEthernet0/0/1',
                        'ip address 10.230.0.1 255.255.255.0', 'no shutdown', 'exit',
                        'ip route 10.230.9.0 255.255.255.0 10.230.0.254', 'end'):
                cli(cmd)
            yield {cmd: cli(cmd) for cmd in ('show version', 'show interfaces', 'show inventory',
                                             'show ip interface brief', 'show ip route')}

    @pytest.mark.parametrize('cmd', ['show version', 'show interfaces', 'show inventory',
                                     'show ip interface brief', 'show ip route'])
    def test_genie_parses(self, outputs, cmd):
        from genie.libs.parser.utils import get_parser
        from genie.conf.base import Device
        dev = Device('GENIE-R1', os='iosxe')
        dev.custom.setdefault('abstraction', {})['order'] = ['os']
        parsed = dev.parse(cmd, output=outputs[cmd])
        assert parsed
        assert get_parser(cmd, dev)
