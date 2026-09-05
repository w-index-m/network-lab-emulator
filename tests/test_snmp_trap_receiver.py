"""
tools/snmp_trap_receiver.py のBERデコーダ回帰テスト。

engine/syslog_sender.py の build_snmp_v2c_trap() が実際に生成するバイト列を
そのままデコードできるかを確認する（実UDPソケットは使わない）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from engine.syslog_sender import build_snmp_v2c_trap
from snmp_trap_receiver import decode_snmp_v2c_trap, TRAP_OID_LINKDOWN, TRAP_OID_LINKUP


def test_decodes_linkdown_trap_built_by_the_emulator():
    pkt = build_snmp_v2c_trap('public', 'r1', TRAP_OID_LINKDOWN,
                               'GigabitEthernet0/1 down')
    parsed = decode_snmp_v2c_trap(pkt)
    assert parsed is not None
    assert parsed['community'] == 'public'
    assert parsed['trap_oid'] == TRAP_OID_LINKDOWN
    assert parsed['description'] == 'r1: GigabitEthernet0/1 down'


def test_decodes_linkup_trap_with_a_different_community():
    pkt = build_snmp_v2c_trap('mycomm', 'catalyst1', TRAP_OID_LINKUP,
                               'Vlan10 up')
    parsed = decode_snmp_v2c_trap(pkt)
    assert parsed is not None
    assert parsed['community'] == 'mycomm'
    assert parsed['trap_oid'] == TRAP_OID_LINKUP
    assert parsed['description'] == 'catalyst1: Vlan10 up'


def test_garbage_input_returns_none():
    assert decode_snmp_v2c_trap(b'\x00\x01\x02not-snmp') is None
