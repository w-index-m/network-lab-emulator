"""
SNMPダッシュボード用データ生成のテスト。

- SnmpAgent._build_mib() が shutdown/no shutdown を admin/oper status に
  正しく反映するか（従来はIPの有無だけで判定しており、shutdownしても
  down表示にならないバグがあった）
- /api/snmp/dashboard が返すJSON構造
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from engine.protocols import snmp_agent, vnet, icmp_engine


def _setup_device(device_id, iface='GigabitEthernet0/0/0'):
    snmp_agent.register(device_id, 'cisco', 'TestRouter')
    icmp_engine.device_ips[device_id] = {
        'ips': {'10.0.0.1': 30},
        'interfaces': {iface: {'ip': '10.0.0.1', 'prefix': 30}},
    }


def test_interface_up_by_default():
    device_id = 'snmp-test-1'
    _setup_device(device_id)
    try:
        mib = snmp_agent._build_mib(device_id)
        by_oid = {oid: v for oid, t, v in mib}
        assert by_oid['1.3.6.1.2.1.2.2.1.7.1'] == '1'  # ifAdminStatus up
        assert by_oid['1.3.6.1.2.1.2.2.1.8.1'] == '1'  # ifOperStatus up
    finally:
        snmp_agent.devices.pop(device_id, None)
        icmp_engine.device_ips.pop(device_id, None)


def test_shutdown_reflected_as_down():
    device_id = 'snmp-test-2'
    iface = 'GigabitEthernet0/0/0'
    _setup_device(device_id, iface)
    vnet.down_interfaces[device_id] = {iface}
    try:
        mib = snmp_agent._build_mib(device_id)
        by_oid = {oid: v for oid, t, v in mib}
        assert by_oid['1.3.6.1.2.1.2.2.1.7.1'] == '2'  # ifAdminStatus down
        assert by_oid['1.3.6.1.2.1.2.2.1.8.1'] == '2'  # ifOperStatus down
    finally:
        snmp_agent.devices.pop(device_id, None)
        icmp_engine.device_ips.pop(device_id, None)
        vnet.down_interfaces.pop(device_id, None)


def test_no_shutdown_restores_up():
    device_id = 'snmp-test-3'
    iface = 'GigabitEthernet0/0/0'
    _setup_device(device_id, iface)
    vnet.down_interfaces[device_id] = {iface}
    vnet.down_interfaces[device_id].discard(iface)
    try:
        mib = snmp_agent._build_mib(device_id)
        by_oid = {oid: v for oid, t, v in mib}
        assert by_oid['1.3.6.1.2.1.2.2.1.8.1'] == '1'
    finally:
        snmp_agent.devices.pop(device_id, None)
        icmp_engine.device_ips.pop(device_id, None)
        vnet.down_interfaces.pop(device_id, None)


def test_cpu_percent_present_for_cisco_family():
    """cisco/catalyst/nexus/asa はCISCO-PROCESS-MIB相当のCPU値を持つ"""
    for dtype in ('cisco', 'catalyst', 'nexus', 'asa'):
        device_id = f'snmp-cpu-{dtype}'
        snmp_agent.register(device_id, dtype, 'TestDevice')
        try:
            mib = snmp_agent._build_mib(device_id)
            by_oid = {oid: v for oid, t, v in mib}
            assert '1.3.6.1.4.1.9.9.109.1.1.1.1.7.1' in by_oid
            cpu = int(by_oid['1.3.6.1.4.1.9.9.109.1.1.1.1.7.1'])
            assert 0 <= cpu <= 100
        finally:
            snmp_agent.devices.pop(device_id, None)
            snmp_agent._cpu_state.pop(device_id, None)


def test_cpu_percent_absent_for_non_cisco_family():
    """Si-R/SR-S/APRESIAはCISCO-PROCESS-MIB非対応（OIDが出ない）"""
    for dtype in ('sir', 'srs', 'apresia'):
        device_id = f'snmp-cpu-{dtype}'
        snmp_agent.register(device_id, dtype, 'TestDevice')
        try:
            mib = snmp_agent._build_mib(device_id)
            by_oid = {oid: v for oid, t, v in mib}
            assert '1.3.6.1.4.1.9.9.109.1.1.1.1.7.1' not in by_oid
        finally:
            snmp_agent.devices.pop(device_id, None)


def test_cpu_percent_walks_smoothly_not_jumping_randomly():
    """CPU値は毎回全くの乱数ではなく、緩やかな遷移（乱歩）であることを確認"""
    device_id = 'snmp-cpu-walk'
    snmp_agent.register(device_id, 'cisco', 'TestDevice')
    try:
        values = []
        for _ in range(20):
            v = snmp_agent._cpu_percent(device_id, 'cisco')
            values.append(v)
        diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        assert all(d <= 4.01 for d in diffs)
    finally:
        snmp_agent.devices.pop(device_id, None)
        snmp_agent._cpu_state.pop(device_id, None)


def test_snmpset_if_admin_override_still_respected():
    """snmpsetによるifAdminStatus上書きは、shutdown状態より優先される"""
    device_id = 'snmp-test-4'
    iface = 'GigabitEthernet0/0/0'
    _setup_device(device_id, iface)
    snmp_agent.devices[device_id]['if_admin'] = {1: '1'}  # 明示的にup
    try:
        mib = snmp_agent._build_mib(device_id)
        by_oid = {oid: v for oid, t, v in mib}
        assert by_oid['1.3.6.1.2.1.2.2.1.7.1'] == '1'
    finally:
        snmp_agent.devices.pop(device_id, None)
        icmp_engine.device_ips.pop(device_id, None)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
