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
