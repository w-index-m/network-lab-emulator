"""
- OSPF の `network 10.200.0.0 0.0.255.255 area 0` のように広い範囲で書いても、実機どおり
  範囲に入るインタフェースのサブネットを1つずつ広告すること(以前は範囲そのものを stub にしていて、
  全ルータが同じ /16 を広告し、互いのサブネットが経路にならなかった)
- /cyber-view のデモ API(start/status/stop)
"""
import os
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402

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


def test_wide_network_statement_advertises_interface_subnets(client):
    r1, r2 = f'onr-1-{_RUN}', f'onr-2-{_RUN}'
    for d in (r1, r2):
        client.post('/api/device', json={'id': d, 'type': 'cisco', 'hostname': d.upper()})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    _cli(client, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.252.12.1 255.255.255.0',
         'no shutdown', 'end')
    _cli(client, r2, 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.252.12.2 255.255.255.0',
         'no shutdown', 'interface GigabitEthernet0/0/1', 'ip address 10.252.99.1 255.255.255.0', 'no shutdown', 'end')
    for d in (r1, r2):
        _cli(client, d, 'configure terminal', 'router ospf 1', 'network 10.252.0.0 0.0.255.255 area 0', 'end')
    end = time.time() + 30
    line = None
    while time.time() < end and not line:
        line = next((l for l in _cli(client, r1, 'show ip route').splitlines()
                     if l.startswith('O') and '10.252.99.0/24' in l), None)
        time.sleep(0.5)
    assert line and 'via 10.252.12.2' in line, _cli(client, r1, 'show ip route')
    # 範囲そのもの(/16)を経路として持たない
    assert '10.252.0.0/16' not in _cli(client, r1, 'show ip route')


def test_demo_api_start_status_stop(client):
    st = client.get('/api/demo/status').json()
    assert st['running'] is False and st['period'] == 90 and len(st['devices']) == 4
    r = client.post('/api/demo/start').json()
    assert r['ok'] and r['started'] is True
    assert client.post('/api/demo/start').json()['started'] is False      # 二重起動しない
    r = client.post('/api/demo/stop').json()
    assert r['ok'] and r['running'] is False
