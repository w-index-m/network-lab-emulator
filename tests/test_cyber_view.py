"""
NIRVANA改風 3D ビュー(/cyber-view)と、その元データの回帰テスト

- ページのルート・認証除外・トークンのキーが index.html と一致すること
- /api/traffic/hosts の形(装置ごとのIP・IF状態・カウンタ、ループバックは除外)
- フローイベントに送信元IP(経路の次ホップ側IFのIP)が入ること
"""

import os
import re
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.traffic_analytics import recorder  # noqa: E402

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


@pytest.fixture(scope='module')
def lab(client):
    """r1 Gi0/0/0 ─ r2 Gi0/0/0 (10.243.12.0/30)、r2 Gi0/0/1 = 10.243.2.1/24"""
    r1, r2, pc = f'cv-r1-{_RUN}', f'cv-r2-{_RUN}', f'cv-pc-{_RUN}'
    client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'CV-R1'})
    client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'CV-R2'})
    client.post('/api/device', json={'id': pc, 'type': 'pc', 'hostname': 'CV-PC'})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    _cli(client, r1, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'ip address 10.243.12.1 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.243.1.1 255.255.255.0', 'no shutdown', 'exit',
         'ip route 10.243.2.0 255.255.255.0 10.243.12.2', 'end')
    _cli(client, r2, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'ip address 10.243.12.2 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.243.2.1 255.255.255.0', 'no shutdown', 'exit',
         'ip route 10.243.1.0 255.255.255.0 10.243.12.1', 'end')
    return client, r1, r2, pc


class TestPage:
    def test_route_and_auth_exemption(self, client):
        r = client.get('/cyber-view')
        assert r.status_code == 200 and 'CYBER VIEW' in r.text
        assert '/cyber-view' in app_module._NO_AUTH_PATHS

    def test_token_key_matches_index(self):
        key = re.search(r"SESSION_KEY\s*=\s*'([^']+)'",
                        open(os.path.join(ROOT, 'static', 'index.html'), encoding='utf-8').read())
        page = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
        assert key and f"SESSION_KEY = '{key.group(1)}'" in page
        assert '/api/traffic/hosts' in page and '/api/traffic/flowmap' in page

    def test_pages_link_each_other(self):
        flow = open(os.path.join(ROOT, 'static', 'traffic_flow.html'), encoding='utf-8').read()
        page = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
        assert 'href="/cyber-view"' in flow and 'href="/traffic-flow"' in page


class TestHostsApi:
    def test_shape_and_values(self, lab):
        c, r1, r2, pc = lab
        _cli(c, r1, 'ping 10.243.2.1 repeat 10 size 1000')
        hosts = {h['id']: h for h in c.get('/api/traffic/hosts').json()['hosts']}
        h = hosts[r1]
        assert h['hostname'] == 'CV-R1' and h['type'] == 'cisco' and r2 in h['peers']
        itf = {i['name']: i for i in h['interfaces']}
        g0 = itf['GigabitEthernet0/0/0']
        assert g0['ip'] == '10.243.12.1' and g0['prefix'] == 30 and g0['up'] is True
        assert g0['out_bytes'] >= 10000 and g0['in_bytes'] >= 10000

    def test_shutdown_shows_down(self, lab):
        c, r1, _r2, _pc = lab
        _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/1', 'shutdown', 'end')
        try:
            h = next(x for x in c.get('/api/traffic/hosts').json()['hosts'] if x['id'] == r1)
            assert {i['name']: i['up'] for i in h['interfaces']}['GigabitEthernet0/0/1'] is False
        finally:
            _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/1', 'no shutdown', 'end')

    def test_loopback_addresses_excluded(self, lab):
        c, _r1, _r2, _pc = lab
        for h in c.get('/api/traffic/hosts').json()['hosts']:
            assert all(not i['ip'].startswith('127.') for i in h['interfaces']), h


class TestFlowSrcIp:
    def test_src_ip_is_egress_interface(self, lab):
        c, r1, r2, _pc = lab
        seq = recorder.seq
        _cli(c, r1, 'ping 10.243.2.1')
        ev = [e for e in c.get(f'/api/traffic/flowmap?since={seq}').json()['events'] if e['src'] == r1][-1]
        assert ev['src_ip'] == '10.243.12.1'      # r2 へ向くIF(Gi0/0/0)のIP。LAN側ではない
        assert ev['dst_ip'] == '10.243.2.1' and ev['dst'] == r2

    def test_record_flow_keeps_src_ip(self):
        ev = recorder.record_flow('x', '10.9.9.9', None, ['x'], False, 1, 100, src_ip='10.9.9.1')
        assert ev['src_ip'] == '10.9.9.1'
        ev = recorder.record_flow('x', '10.9.9.9', None, ['x'], False, 1, 100)
        assert ev['src_ip'] is None
