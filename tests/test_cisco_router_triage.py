"""
tools/cisco_router_triage.py テスト

CPU/メモリ/インターフェース/ログの出力パーサーと、閾値に基づく
severity判定ロジックを検証する。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.cisco_router_triage import (
    parse_cpu, parse_memory, parse_interfaces_down, parse_interface_errors,
    parse_recent_errors, diagnose, CPU_CRIT,
)


def test_parse_cpu_extracts_all_fields():
    out = 'CPU utilization for five seconds: 11%/6%; one minute: 6%; five minutes: 7%'
    cpu = parse_cpu(out)
    assert cpu == {'five_sec': 11, 'five_sec_interrupt': 6, 'one_min': 6, 'five_min': 7}


def test_parse_cpu_returns_none_on_unparseable_output():
    assert parse_cpu('garbage output') is None


def test_parse_memory_computes_used_percent():
    out = 'Total: 512MB, Used: 128MB, Free: 384MB'
    mem = parse_memory(out)
    assert mem['total_mb'] == 512
    assert mem['used_pct'] == 25.0


def test_parse_interfaces_down_finds_down_lines():
    out = (
        'Interface              IP-Address      OK? Method Status                Protocol\n'
        'GigabitEthernet0/0/0   10.9.9.2        YES NVRAM   up                    up\n'
        'GigabitEthernet0/0/1   unassigned      YES unset   administratively down down\n'
    )
    down = parse_interfaces_down(out)
    assert len(down) == 1
    assert down[0]['interface'] == 'GigabitEthernet0/0/1'


def test_parse_interfaces_down_empty_when_all_up():
    out = (
        'Interface              IP-Address      OK? Method Status                Protocol\n'
        'GigabitEthernet0/0/0   10.9.9.2        YES NVRAM   up                    up\n'
    )
    assert parse_interfaces_down(out) == []


def test_parse_interface_errors_finds_nonzero_counters():
    out = (
        'Port        Align-Err     FCS-Err    Xmit-Err     Rcv-Err  UnderSize  OutDiscards\n'
        'Gi0/0/0     0             0          0            0        0          0\n'
        'Gi0/0/1     3             0          0            0        0          0\n'
    )
    errored = parse_interface_errors(out)
    assert len(errored) == 1
    assert errored[0]['interface'] == 'Gi0/0/1'
    assert errored[0]['total_errors'] == 3


def test_parse_recent_errors_filters_by_severity():
    out = (
        '*Sep 01 23:20:46.906: %LINK-3-UPDOWN: Interface Gi0/0/1, changed state to down\n'
        '*Sep 01 23:20:47.000: %SYS-5-CONFIG_I: Configured from console\n'
    )
    errors = parse_recent_errors(out)
    assert len(errors) == 1
    assert 'LINK-3-UPDOWN' in errors[0]


class _FakeClient:
    """diagnose()をエミュレーターに繋がずテストするためのモック"""
    def __init__(self, responses: dict):
        self.responses = responses

    def cli(self, device_id, command):
        return self.responses.get(command, '')


def test_diagnose_flags_critical_cpu():
    client = _FakeClient({
        'show processes cpu': f'CPU utilization for five seconds: 90%/5%; one minute: 88%; five minutes: {CPU_CRIT}%',
        'show memory statistics': 'Total: 512MB, Used: 100MB, Free: 412MB',
        'show ip interface brief': 'Interface IP-Address OK? Method Status Protocol\nGi0/0/0 1.1.1.1 YES NVRAM up up\n',
        'show interfaces counters errors': '',
        'show logging': '',
    })
    findings = diagnose(client, 'cisco')
    cpu_finding = next(f for f in findings if 'CPU' in f.title)
    assert cpu_finding.severity == 'critical'


def test_diagnose_all_ok_when_healthy():
    client = _FakeClient({
        'show processes cpu': 'CPU utilization for five seconds: 2%/1%; one minute: 3%; five minutes: 4%',
        'show memory statistics': 'Total: 512MB, Used: 50MB, Free: 462MB',
        'show ip interface brief': 'Interface IP-Address OK? Method Status Protocol\nGi0/0/0 1.1.1.1 YES NVRAM up up\n',
        'show interfaces counters errors': 'Port Align-Err FCS-Err Xmit-Err Rcv-Err UnderSize OutDiscards\nGi0/0/0 0 0 0 0 0 0\n',
        'show logging': 'no errors here',
    })
    findings = diagnose(client, 'cisco')
    assert all(f.severity == 'ok' for f in findings)
