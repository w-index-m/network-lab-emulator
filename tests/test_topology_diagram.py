"""
tools/topology_diagram.py と GET /api/topology/neighbors のテスト。

CDP/LLDPの隣接情報からトポロジー図(Mermaid/Graphviz)を組み立てる
ツール。CDPはCisco系(catalyst/cisco/srs)同士でしか成立しない一方、
LLDPは全機種共通なので、異なるベンダー間のリンクはLLDPだけが頼り
になる（実機と同じ制約）。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from tools.topology_diagram import to_mermaid, to_dot, _short_if

client = TestClient(app_module.app)


def _dev(id_, type_, hostname):
    client.delete(f'/api/device/{id_}')
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': hostname})


def _link(a, a_if, b, b_if):
    return client.post('/api/link', json={'a': a, 'a_if': a_if, 'b': b, 'b_if': b_if})


def test_short_if_abbreviates_common_prefixes():
    assert _short_if('GigabitEthernet1/0/1') == 'Gi1/0/1'
    assert _short_if('TenGigabitEthernet1/1') == 'Te1/1'
    assert _short_if('Ethernet1/1') == 'Eth1/1'
    assert _short_if('') == ''
    assert _short_if(None) == ''


class TestNeighborsApi:
    def test_link_between_two_cisco_devices_produces_cdp_and_edge(self):
        _dev('topo-a', 'catalyst', 'Topo-A')
        _dev('topo-b', 'catalyst', 'Topo-B')
        _link('topo-a', 'GigabitEthernet1/0/1', 'topo-b', 'GigabitEthernet1/0/1')

        data = client.get('/api/topology/neighbors').json()
        a = data['devices']['topo-a']
        assert any(n['device'] == 'Topo-B' for n in a['cdp_neighbors'])
        edge = next(e for e in data['edges']
                   if {e['a'], e['b']} == {'topo-a', 'topo-b'})
        assert edge['a_if'] and edge['b_if']

    def test_edge_is_not_duplicated_in_both_directions(self):
        _dev('topo-c', 'catalyst', 'Topo-C')
        _dev('topo-d', 'catalyst', 'Topo-D')
        _link('topo-c', 'GigabitEthernet1/0/1', 'topo-d', 'GigabitEthernet1/0/1')

        data = client.get('/api/topology/neighbors').json()
        matches = [e for e in data['edges']
                  if {e['a'], e['b']} == {'topo-c', 'topo-d'}]
        assert len(matches) == 1

    def test_cross_vendor_link_relies_on_lldp_not_cdp(self):
        """CatalystとNexusの間はCDPが成立しないので、LLDPだけで
        エッジが検出できることを確認する（実機と同じ制約）。"""
        _dev('topo-e', 'catalyst', 'Topo-E')
        _dev('topo-f', 'nexus', 'Topo-F')
        _link('topo-e', 'GigabitEthernet1/0/1', 'topo-f', 'Ethernet1/1')

        data = client.get('/api/topology/neighbors').json()
        e_info = data['devices']['topo-e']
        assert e_info['cdp_neighbors'] == []
        assert any(n['device'] == 'Topo-F' for n in e_info['lldp_neighbors'])
        assert any({e['a'], e['b']} == {'topo-e', 'topo-f'} for e in data['edges'])

    def test_unlinked_device_has_no_neighbors(self):
        _dev('topo-lonely', 'catalyst', 'Topo-Lonely')
        data = client.get('/api/topology/neighbors').json()
        info = data['devices']['topo-lonely']
        assert info['cdp_neighbors'] == []
        assert info['lldp_neighbors'] == []
        assert not any('topo-lonely' in (e['a'], e['b']) for e in data['edges'])


class TestDiagramRendering:
    def _sample(self):
        return {
            'devices': {
                'r1': {'hostname': 'R1', 'type': 'catalyst'},
                'r2': {'hostname': 'R2', 'type': 'nexus'},
            },
            'edges': [
                {'a': 'r1', 'a_if': 'GigabitEthernet1/0/1',
                 'b': 'r2', 'b_if': 'Ethernet1/1'},
            ],
        }

    def test_to_mermaid_includes_nodes_and_edge_with_interface_labels(self):
        out = to_mermaid(self._sample())
        assert 'graph LR' in out
        assert 'r1["' in out and 'R1' in out
        assert 'r2["' in out and 'R2' in out
        assert 'Gi1/0/1 - Eth1/1' in out
        assert 'r1 ---|' in out

    def test_to_dot_includes_nodes_and_edge(self):
        out = to_dot(self._sample())
        assert 'graph topology {' in out
        assert '"r1" [label=' in out
        assert '"r1" -- "r2"' in out
        assert 'Gi1/0/1 - Eth1/1' in out

    def test_to_mermaid_handles_missing_interface_names(self):
        data = {
            'devices': {'r1': {'hostname': 'R1', 'type': 'catalyst'},
                       'r2': {'hostname': 'R2', 'type': 'catalyst'}},
            'edges': [{'a': 'r1', 'a_if': None, 'b': 'r2', 'b_if': None}],
        }
        out = to_mermaid(data)
        assert 'r1 --- r2' in out
