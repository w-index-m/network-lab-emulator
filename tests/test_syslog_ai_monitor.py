"""
tools/syslog_ai_monitor.py テスト

- RFC3164 syslogパケットのパース
- ルールベース要約（Ollama無しの場合のフォールバック）でのフラップ検知
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.syslog_ai_monitor import (
    parse_syslog_packet, _rule_based_summary, LogEntry, SyslogStore,
)
from engine.syslog_sender import _build_syslog_packet


def test_parse_syslog_packet_extracts_fields():
    pkt = _build_syslog_packet('local7', 'errors', 'R1',
                                '%OSPF-5-ADJCHG: Nbr 10.0.0.2 from FULL to DOWN')
    entry = parse_syslog_packet(pkt)
    assert entry.hostname == 'R1'
    assert entry.severity == 3  # errors
    assert entry.event_tag == '%OSPF-5-ADJCHG'
    assert 'FULL to DOWN' in entry.message


def test_parse_malformed_packet_falls_back_gracefully():
    entry = parse_syslog_packet(b'not a syslog packet')
    assert entry is not None
    assert entry.hostname == 'unknown'


def test_rule_based_summary_empty():
    out = _rule_based_summary([])
    assert '新規ログはありません' in out


def test_rule_based_summary_detects_flap():
    now = time.time()
    entries = [
        LogEntry(received_at=now, hostname='R1', facility=23, severity=5,
                  message='%OSPF-5-ADJCHG: flap', event_tag='%OSPF-5-ADJCHG')
        for _ in range(4)
    ]
    out = _rule_based_summary(entries)
    assert 'フラップの可能性' in out
    assert 'R1' in out


def test_rule_based_summary_flags_critical_severity():
    now = time.time()
    entries = [LogEntry(received_at=now, hostname='SIR-A', facility=23, severity=3,
                         message='%RIP-4-AUTH: Invalid authentication', event_tag='%RIP-4-AUTH')]
    out = _rule_based_summary(entries)
    assert '[重要]' in out
    assert 'SIR-A' in out


def test_store_since_filters_by_timestamp():
    store = SyslogStore(log_path=None)
    t0 = time.time()
    store.add(LogEntry(received_at=t0 - 100, hostname='old', facility=23, severity=6, message='old'))
    store.add(LogEntry(received_at=t0, hostname='new', facility=23, severity=6, message='new'))
    recent = store.since(t0 - 1)
    assert len(recent) == 1
    assert recent[0].hostname == 'new'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
