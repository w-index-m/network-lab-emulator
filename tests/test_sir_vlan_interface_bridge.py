"""
Si-R の show vlan / show interface / show bridge / show bridgegroup
未実装だったコマンドの新規実装に対する回帰テスト。

実機 Si-R G110B の show tech-support（2026-09-04）で確認した出力形式に
合わせて実装した。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _fresh_sir(device_id: str):
    client.post('/api/device', json={
        'id': device_id, 'type': 'sir', 'hostname': device_id,
    })
    def run(cmd):
        return client.post('/api/cli', json={
            'device_id': device_id, 'command': cmd,
        }).json()['output']
    return run


def test_show_vlan_lists_ether_ports_grouped_by_vid():
    run = _fresh_sir('sir-vlan-1')
    out = run('show vlan')
    assert 'VID  Interface' in out
    assert 'ether 1 1          untagged      port     default' in out
    assert 'ether 2 1          untagged      port     v2' in out
    assert 'ether 2 2          untagged' in out
    assert 'Total Count :   2' in out


def test_show_interface_lists_lan_and_lo_with_mac_and_vlan():
    run = _fresh_sir('sir-int-1')
    out = run('show interface')
    assert 'lan0' in out and 'MTU 1500' in out
    assert 'VLAN ID is 1' in out
    assert 'lo0' in out and 'MTU 16384' in out
    assert 'IPv6 address/prefixlen:' in out


def test_show_interface_brief_table():
    run = _fresh_sir('sir-int-2')
    out = run('show interface brief')
    assert 'Interface        Status     Type' in out
    assert 'lan0' in out
    assert 'lo0' in out and 'loopback' in out


def test_show_interface_summary_counts():
    run = _fresh_sir('sir-int-3')
    out = run('show interface summary')
    assert 'interfaces' in out
    assert 'Loopback interface' in out
    assert 'Port VLAN interface' in out


def test_show_bridge_table():
    run = _fresh_sir('sir-bridge-1')
    out = run('show bridge')
    assert 'Codes: D - Dynamic entry' in out
    assert ' 1    cpu 0' in out
    assert ' 2    cpu 1' in out


def test_show_bridge_summary_counts():
    run = _fresh_sir('sir-bridge-2')
    out = run('show bridge summary')
    assert 'Registered station blocks' in out
    assert 'Free station blocks' in out


def test_show_bridgegroup_empty_table():
    run = _fresh_sir('sir-bg-1')
    out = run('show bridgegroup')
    assert 'Address             Group   Interface   Status      Remain time' in out


def test_show_bridgegroup_status_lists_vlans():
    run = _fresh_sir('sir-bg-2')
    out = run('show bridgegroup status')
    assert 'vlan1' in out
    assert 'vlan2' in out
    assert 'Routing' in out
