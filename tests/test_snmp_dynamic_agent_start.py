"""
回帰テスト: アプリ起動後に動的追加した装置にも実UDP SNMPエージェントが
起動すること（以前は start_all_snmp_agents がアプリ起動時に存在した装置
しか対象にしておらず、後から /api/device で追加した装置はsnmpget/snmpwalk
に一切応答しなかった）。
"""

import pytest

from engine.snmp_udp_agent import ensure_snmp_agent, _running_agents


class _FakeState:
    def __init__(self, interfaces):
        self.interfaces = interfaces


class _FakeSnmpAgent:
    pass


@pytest.mark.asyncio
async def test_ensure_snmp_agent_starts_for_a_dynamically_added_device():
    device_id = 'dyn-snmp-test-1'
    _running_agents.pop(device_id, None)
    device_sessions = {device_id: _FakeState({'eth0': {'ip': '127.0.0.1', 'prefix': 24}})}
    try:
        await ensure_snmp_agent(device_id, device_sessions, _FakeSnmpAgent(), port=19161)
        assert device_id in _running_agents
        # 二重起動しても例外にならない（既に起動済みなら何もしない）
        await ensure_snmp_agent(device_id, device_sessions, _FakeSnmpAgent(), port=19161)
        assert device_id in _running_agents
    finally:
        entry = _running_agents.pop(device_id, None)
        if entry:
            entry[0].close()


@pytest.mark.asyncio
async def test_ensure_snmp_agent_skips_device_without_ip():
    device_id = 'dyn-snmp-test-2'
    _running_agents.pop(device_id, None)
    device_sessions = {device_id: _FakeState({})}
    await ensure_snmp_agent(device_id, device_sessions, _FakeSnmpAgent(), port=19162)
    assert device_id not in _running_agents


@pytest.mark.asyncio
async def test_ensure_snmp_agent_skips_unknown_device():
    device_id = 'dyn-snmp-test-3-does-not-exist'
    _running_agents.pop(device_id, None)
    await ensure_snmp_agent(device_id, {}, _FakeSnmpAgent(), port=19163)
    assert device_id not in _running_agents
