"""PC の既定ゲートウェイを変えたときの経路と、traceroute の最後の行(宛先そのもの)

Catalyst 3台の OSPF の各配下に PC を置いて確かめた時に見つけた2件:
  - `ip route add default via X` でゲートウェイを変えても、前のデフォルト経路(既定の 192.168.1.1)が
    RIB に残り、そちらへ送り続けて ping が届かなかった
  - traceroute が宛先の手前のルータで終わり、宛先そのものの行が出なかった
"""
import os
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as A  # noqa: E402


@pytest.fixture(scope='module')
def lab():
    c = TestClient(A.app)
    u = uuid.uuid4().hex[:5]
    ids = {k: f'{k}-{u}' for k in ('C1', 'C2', 'P1', 'P2')}

    def cli(d, *cmds):
        out = ''
        for x in cmds:
            out = c.post('/api/cli', json={'device_id': ids[d], 'command': x}).json()['output']
        return out
    for k in ('C1', 'C2'):
        c.post('/api/device', json={'id': ids[k], 'type': 'catalyst', 'hostname': ids[k]})
    for k in ('P1', 'P2'):
        c.post('/api/device', json={'id': ids[k], 'type': 'pc', 'hostname': ids[k]})
    c.post('/api/link', json={'a': ids['C1'], 'b': ids['C2'], 'iface_a': 'GigabitEthernet1/0/1', 'iface_b': 'GigabitEthernet1/0/1'})
    c.post('/api/link', json={'a': ids['C1'], 'b': ids['P1'], 'iface_a': 'GigabitEthernet1/0/3', 'iface_b': 'eth0'})
    c.post('/api/link', json={'a': ids['C2'], 'b': ids['P2'], 'iface_a': 'GigabitEthernet1/0/3', 'iface_b': 'eth0'})
    for k, n, peer, lan, plan in (('C1', 1, 2, 61, 62), ('C2', 2, 1, 62, 61)):
        cli(k, 'configure terminal', 'interface GigabitEthernet1/0/1', 'no switchport',
            f'ip address 10.252.12.{n} 255.255.255.252', 'no shutdown', 'exit',
            'interface Vlan10', f'ip address 192.168.{lan}.1 255.255.255.0', 'no shutdown', 'exit',
            'interface GigabitEthernet1/0/3', 'switchport mode access', 'switchport access vlan 10', 'no shutdown', 'exit',
            f'ip route 192.168.{plan}.0 255.255.255.0 10.252.12.{peer}', 'end')
    cli('P1', 'ip addr add 192.168.61.10/24 dev eth0', 'ip route add default via 192.168.61.1')
    cli('P2', 'ip addr add 192.168.62.10/24 dev eth0', 'ip route add default via 192.168.62.1')
    return ids, cli


def test_pc_default_gateway_change_replaces_old_route(lab):
    ids, cli = lab
    defaults = [r for r in A.rib_engine.get_best_routes(ids['P1']) if r['network'] == '0.0.0.0' and r['prefix'] == 0]
    assert [r['next_hop'] for r in defaults] == ['192.168.61.1']
    assert '0% packet loss' in cli('P1', 'ping -c 2 192.168.62.10')


def test_traceroute_ends_with_destination(lab):
    ids, cli = lab
    out = cli('P1', 'traceroute 192.168.62.10').splitlines()
    assert out[-1].lstrip().startswith('3  ') and f"{ids['P2']} (192.168.62.10)" in out[-1]
    out = cli('C1', 'traceroute 192.168.62.10').splitlines()
    assert f"{ids['P2']} (192.168.62.10)" in out[-1]
