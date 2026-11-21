"""
ニルヴァーナ単体版: OSPF 画面を SNMP(OSPF-MIB)の直接読み取りで出す(cvs/snmp_ospf.py)

ヴァルハラに OSPF-MIB を貯める snmp_exporter が無い構成(設定画面 = gnmic のみ)でも OSPF 画面が動くように。
設定に無い相手(VRF の別プロセス・他社のルータ)は Router-ID の名前で1台として置き、相手側の状態は分からないので null。
ラボの模擬 Cisco 2台(OSPF Full)を本物の SNMP で読み、画面に隣接が出ることは docs/nirvana-gnmi-via-gnmic.md に記録。
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
from cvs import snmp_ospf as O  # noqa: E402

N = O.NBR


def _fake(monkeypatch):
    tables = {
        N: [(f'{N}.1.10.255.0.2.0', '10.255.0.2'), (f'{N}.3.10.255.0.2.0', '10.255.0.2'), (f'{N}.6.10.255.0.2.0', 8),
            (f'{N}.1.192.168.1.50.0', '192.168.1.50'), (f'{N}.3.192.168.1.50.0', '9.9.9.9'), (f'{N}.6.192.168.1.50.0', 3)],
        O.IFNAME: [(f'{O.IFNAME}.23', 'Gi1/0/23'), (f'{O.IFNAME}.100', 'Vl1')],
        O.IPADDR + '.2': [(f'{O.IPADDR}.2.10.255.0.1', 23), (f'{O.IPADDR}.2.192.168.1.1', 100)],
        O.IPADDR + '.3': [(f'{O.IPADDR}.3.10.255.0.1', '255.255.255.252'), (f'{O.IPADDR}.3.192.168.1.1', '255.255.255.0')],
        O.IF_AREA: [(f'{O.IF_AREA}.10.255.0.1.0', '0.0.0.0'), (f'{O.IF_AREA}.192.168.1.1.0', '0.0.0.1')],
    }
    monkeypatch.setattr(O.SC, 'get', lambda h, c, oids, **k: {O.RID: '192.168.1.1', O.ADMIN: 1})
    monkeypatch.setattr(O.SC, 'walk', lambda h, c, root, **k: tables.get(root, []))


def test_read_maps_neighbors_to_interfaces_and_areas(monkeypatch):
    _fake(monkeypatch)
    r = O.read('192.168.1.1', 'public')
    assert r['rid'] == '192.168.1.1' and r['areas'] == ['0.0.0.0', '0.0.0.1']
    assert {(n['ip'], n['rid'], n['state'], n['iface'], n['area']) for n in r['neighbors']} == {
        ('10.255.0.2', '10.255.0.2', 'Full', 'Gi1/0/23', '0.0.0.0'), ('192.168.1.50', '9.9.9.9', 'Init', 'Vl1', '0.0.0.1')}
    monkeypatch.setattr(O.SC, 'get', lambda h, c, oids, **k: {O.ADMIN: 2})
    assert O.read('192.168.1.1', 'public') is None                      # OSPF が無効


def test_snapshot_places_unknown_peers_and_one_sided_state(monkeypatch):
    _fake(monkeypatch)
    cfg = {'devices': [{'id': 'sw1', 'hostname': 'SW1'}]}
    s = O.snapshot(cfg, {'sw1': O.read('192.168.1.1', 'public')}, lambda *k: None)
    assert s['enabled'] and s['source'] == 'snmp' and s['tree'] is None
    assert {d['id'] for d in s['devices']} == {'sw1', 'ospf:10.255.0.2', 'ospf:9.9.9.9'}
    sw = next(d for d in s['devices'] if d['id'] == 'sw1')
    assert sw['abr'] and sw['neighbors_full'] == 1
    adj = {a['b']: a for a in s['adjacencies']}
    assert adj['ospf:10.255.0.2']['state'] == 'full' and adj['ospf:10.255.0.2']['state_b'] is None
    assert adj['ospf:9.9.9.9']['state'] == 'partial' and adj['ospf:9.9.9.9']['iface_a'] == 'Vl1'


def test_two_polled_devices_make_one_adjacency(monkeypatch):
    a = {'rid': '1.1.1.1', 'areas': ['0.0.0.0'], 'neighbors': [{'ip': '10.0.0.2', 'rid': '2.2.2.2', 'state': 'Full', 'iface': 'Gi0/0', 'area': '0.0.0.0'}]}
    b = {'rid': '2.2.2.2', 'areas': ['0.0.0.0'], 'neighbors': [{'ip': '10.0.0.1', 'rid': '1.1.1.1', 'state': 'Full', 'iface': 'Gi0/1', 'area': '0.0.0.0'}]}
    s = O.snapshot({'devices': [{'id': 'r1', 'hostname': 'R1'}, {'id': 'r2', 'hostname': 'R2'}]}, {'r1': a, 'r2': b}, lambda *k: None)
    assert len(s['adjacencies']) == 1
    x = s['adjacencies'][0]
    assert (x['state'], x['state_a'], x['state_b'], x['iface_a'], x['iface_b']) == ('full', 'Full', 'Full', 'Gi0/0', 'Gi0/1')
