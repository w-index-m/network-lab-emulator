"""
インターフェース shutdown/no shutdown 時の実syslog(%LINK-3-UPDOWN)+
実SNMP trap(linkDown/linkUp) 送信テスト

FastAPI TestClientで実際に /api/cli を叩き、UDP送信の実処理関数
(send_syslog_async/send_snmp_trap_async)をmonkeypatchして呼び出しの
有無・内容を検証する（実UDPソケットは使わない）。
"""

import os
import sys
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module
import engine.syslog_sender as syslog_sender


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture
def capture_dispatches(monkeypatch):
    calls = {'syslog': [], 'trap': []}

    async def fake_syslog(host, port, facility, severity, hostname, msg):
        calls['syslog'].append((host, port, severity, hostname, msg))
        return True

    async def fake_trap(host, port, community, hostname, trap_oid, description):
        calls['trap'].append((host, port, community, hostname, trap_oid, description))
        return True

    monkeypatch.setattr(syslog_sender, 'send_syslog_async', fake_syslog)
    monkeypatch.setattr(syslog_sender, 'send_snmp_trap_async', fake_trap)
    return calls


def _cli(client, device_id, command):
    r = client.post('/api/cli', json={'device_id': device_id, 'command': command})
    assert r.status_code == 200
    return r.json()


def test_shutdown_sends_syslog_and_linkdown_trap(client, capture_dispatches):
    _cli(client, 'catalyst', 'configure terminal')
    _cli(client, 'catalyst', 'logging host 127.0.0.1 5514')
    _cli(client, 'catalyst', 'snmp-server host 127.0.0.1 public')
    _cli(client, 'catalyst', 'interface GigabitEthernet1/0/1')
    _cli(client, 'catalyst', 'shutdown')
    _cli(client, 'catalyst', 'end')

    syslog_msgs = [c[4] for c in capture_dispatches['syslog']]
    assert any('changed state to down' in m and 'GigabitEthernet1/0/1' in m
               for m in syslog_msgs)

    trap_oids = [c[4] for c in capture_dispatches['trap']]
    assert '1.3.6.1.6.3.1.1.5.3' in trap_oids  # linkDown


def test_no_shutdown_sends_syslog_and_linkup_trap(client, capture_dispatches):
    _cli(client, 'catalyst', 'configure terminal')
    _cli(client, 'catalyst', 'logging host 127.0.0.1 5514')
    _cli(client, 'catalyst', 'snmp-server host 127.0.0.1 public')
    _cli(client, 'catalyst', 'interface GigabitEthernet1/0/1')
    _cli(client, 'catalyst', 'shutdown')
    _cli(client, 'catalyst', 'no shutdown')
    _cli(client, 'catalyst', 'end')

    syslog_msgs = [c[4] for c in capture_dispatches['syslog']]
    assert any('changed state to up' in m for m in syslog_msgs)

    trap_oids = [c[4] for c in capture_dispatches['trap']]
    assert '1.3.6.1.6.3.1.1.5.4' in trap_oids  # linkUp


def test_shutdown_without_configured_targets_sends_nothing(client, capture_dispatches):
    """logging host / snmp-server host を設定していない装置ではUDP送信自体が
    発生しない(SyslogDispatcher/SnmpDispatcherはtargetsが空ならno-op)"""
    _cli(client, 'nexus', 'configure terminal')
    _cli(client, 'nexus', 'interface GigabitEthernet0/0/1')
    _cli(client, 'nexus', 'shutdown')
    _cli(client, 'nexus', 'end')

    nexus_syslog = [c for c in capture_dispatches['syslog'] if c[3] == 'nexus']
    nexus_trap = [c for c in capture_dispatches['trap'] if c[3] == 'nexus']
    assert nexus_syslog == []
    assert nexus_trap == []
