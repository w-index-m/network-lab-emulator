"""
Si-R の `bgp neighbor <n> address <ip> remote-as <as>` が、指定 IP を持つ隣接装置をピアにすること

以前は常に「最初の隣接」をピアにしていたため、A ─ B ─ C の B で2本目のネイバー(C)を
設定すると A に付いてしまい(AS も食い違い)、B-C 間のセッションが無いまま
`show ip bgp summary` の Neighbor 列も `192.168.1.<装置ID>` のような壊れた表示だった。
"""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.protocols import bgp_engine  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


def test_second_neighbor_binds_to_device_owning_that_ip(client):
    a, b, c = (f'sbn-{x}-{_RUN}' for x in 'abc')
    for d in (a, b, c):
        client.post('/api/device', json={'id': d, 'type': 'sir', 'hostname': d.upper()})
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'lan0', 'iface_b': 'lan0'})
    client.post('/api/link', json={'a': b, 'b': c, 'iface_a': 'lan1', 'iface_b': 'lan0'})
    _cli(client, a, 'configure', 'lan 0 ip address 10.249.1.1/24 3', 'exit')
    _cli(client, b, 'configure', 'lan 0 ip address 10.249.1.2/24 3', 'lan 1 ip address 10.249.2.1/24 3', 'exit')
    _cli(client, c, 'configure', 'lan 0 ip address 10.249.2.2/24 3', 'exit')
    _cli(client, b, 'configure', 'bgp use on', 'bgp as 65002',
         'bgp neighbor 0 address 10.249.1.1 remote-as 65001',
         'bgp neighbor 1 address 10.249.2.2 remote-as 65003', 'exit')
    sess = bgp_engine.nodes[b]['sessions']
    assert set(sess) == {a, c}
    assert sess[a].remote_as == 65001 and sess[c].remote_as == 65003
    assert sess[c].neighbor_ip == '10.249.2.2'
    out = _cli(client, b, 'show ip bgp summary')
    assert '10.249.1.1' in out and '10.249.2.2' in out and '192.168.1.' not in out
