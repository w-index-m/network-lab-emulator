"""
カバレッジの穴を埋めるテスト — EtherChannel / SPAN / Si-R STP・OSPF

engine/rules.py の未実行関数を潰す過程で書いたもの。
`show etherchannel <group> detail` がグループ番号付きの構文に
マッチせず summary にフォールスルーしていた不具合を、ここで見つけた。

**注意**: 自分が実装した直後の関数でもテストが無いものが残っていた
（Si-Rの `_format_spanning_tree_sir` / `_format_ospf_neighbor_sir` は
今セッションで実装したのにカバレッジ0だった）。実装したら必ず
テストまで書くこと。
"""

import asyncio
import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app as app_module                             # noqa: E402
from engine.rules import DeviceState, RuleEngine     # noqa: E402

engine = RuleEngine()


def _sw(hostname='SW1', dtype='catalyst'):
    st = DeviceState(dtype, hostname)
    st.mode = 'exec'
    return st


def _run(st, cmds):
    for c in cmds:
        engine.process(c, st)
    return st


# ══════════════════════════════════════════
# EtherChannel
# ══════════════════════════════════════════
def _po(st=None):
    st = st or _sw()
    _run(st, ['configure terminal',
              'interface GigabitEthernet1/0/1', 'channel-group 1 mode active',
              'exit',
              'interface GigabitEthernet1/0/2', 'channel-group 1 mode active',
              'exit',
              'interface GigabitEthernet1/0/3', 'channel-group 2 mode on',
              'end'])
    return st


def test_channel_group_registers_every_member():
    st = _po()
    assert st.channel_groups[1]['members'] == ['GigabitEthernet1/0/1',
                                               'GigabitEthernet1/0/2']
    assert st.channel_groups[2]['mode'] == 'on'


def test_summary_lists_all_members_of_the_bundle():
    st = _po()
    out = engine.process('show etherchannel summary', st)
    assert 'Number of channel-groups in use: 2' in out
    line = next(l for l in out.splitlines() if l.startswith('1 '))
    assert 'Gi1/0/1(P)' in line and 'Gi1/0/2(P)' in line
    assert 'Po1(SU)' in line
    assert 'LACP' in line


def test_summary_shows_pagp_for_mode_on():
    st = _po()
    out = engine.process('show etherchannel summary', st)
    line = next(l for l in out.splitlines() if l.startswith('2 '))
    assert 'PAgP' in line


def test_detail_without_group_lists_every_group():
    st = _po()
    out = engine.process('show etherchannel detail', st)
    assert 'Group: 1' in out
    assert 'Group: 2' in out


def test_detail_with_group_number_is_not_treated_as_summary():
    """`show etherchannel <group> detail` が summary に落ちないこと

    実機は "show etherchannel [<group>] {summary|detail|...}" と
    グループ番号を挟める。対応していなかったため、番号付きで
    detail を指定すると summary が返っていた。
    """
    st = _po()
    out = engine.process('show etherchannel 1 detail', st)
    assert out.startswith('Group: 1')
    assert 'Group: 2' not in out          # 指定グループだけ
    assert 'Flags:' not in out            # summaryのヘッダが出ていない


def test_summary_with_group_number_filters():
    st = _po()
    out = engine.process('show etherchannel 1 summary', st)
    assert 'Number of channel-groups in use: 1' in out
    assert not any(l.startswith('2 ') for l in out.splitlines())


def test_unknown_group_is_reported():
    st = _po()
    assert 'does not exist' in engine.process('show etherchannel 9 detail', st)
    assert 'does not exist' in engine.process('show etherchannel 9 summary', st)


def test_bare_show_etherchannel_defaults_to_summary():
    st = _po()
    assert 'Flags:' in engine.process('show etherchannel', st)


def test_no_etherchannel_configured():
    assert 'No EtherChannels configured' in engine.process(
        'show etherchannel detail', _sw())


def test_invalid_channel_group_number_is_rejected():
    st = _sw()
    _run(st, ['configure terminal', 'interface GigabitEthernet1/0/1'])
    out = engine.process('channel-group 99 mode active', st) or ''
    assert 'Invalid' in out or '99' in out


# ══════════════════════════════════════════
# SPAN（monitor session）
# ══════════════════════════════════════════
def test_monitor_session_source_and_destination():
    st = _sw()
    _run(st, ['configure terminal',
              'monitor session 1 source interface GigabitEthernet1/0/3',
              'monitor session 1 destination interface GigabitEthernet1/0/4',
              'end'])
    out = engine.process('show monitor session 1', st)
    assert 'Session 1' in out
    assert 'Type                   : Local Session' in out
    assert 'GigabitEthernet1/0/3' in out
    assert 'Destination Ports      : GigabitEthernet1/0/4' in out


def test_monitor_session_unknown_id():
    st = _sw()
    out = engine.process('show monitor session 5', st) or ''
    assert out.strip() != ''        # 黙って空を返さない


# ══════════════════════════════════════════
# Si-R: spanning-tree / ospf neighbor
# ══════════════════════════════════════════
def _sir(dev='t-sir-cov'):
    app_module.device_sessions.pop(dev, None)
    st = DeviceState('sir', 'SIR-A')
    app_module.device_sessions[dev] = st
    return dev, st


def _aconf(dev, st, cmds):
    async def _go():
        out = ''
        for c in cmds:
            out = await app_module.handle_protocol_config(dev, c, st)
        return out
    return asyncio.run(_go())


def _ashow(dev, st, cmd):
    return asyncio.run(app_module.handle_protocol_show(dev, cmd, st))


def test_sir_spanning_tree_reflects_mode_and_priority():
    dev, st = _sir()
    _aconf(dev, st, ['configure', 'stp mode stp',
                     'stp domain 0 priority 8192'])
    out = _ashow(dev, st, 'show spanning-tree')
    assert 'Spanning tree enabled protocol IEEE' in out
    assert 'Priority    8192' in out
    assert 'STP Mode   stp' in out
    assert 'Hello Time 2sec' in out


def test_sir_stp_rejects_nonzero_instance_id():
    """この機種のSTPインスタンスIDは0のみ（マニュアル5.1.5）"""
    dev, st = _sir()
    out = _aconf(dev, st, ['configure', 'stp mode stp',
                           'stp domain 1 priority 8192'])
    assert 'ERROR' in out
    assert 'インスタンスID' in out


def test_sir_stp_rejects_priority_not_multiple_of_4096():
    dev, st = _sir()
    out = _aconf(dev, st, ['configure', 'stp mode stp',
                           'stp domain 0 priority 8193'])
    assert 'ERROR' in out
    assert '4096' in out


@pytest.mark.parametrize('pri', [0, 4096, 61440])
def test_sir_stp_accepts_valid_priorities(pri):
    dev, st = _sir()
    out = _aconf(dev, st, ['configure', 'stp mode stp',
                           f'stp domain 0 priority {pri}'])
    assert not (out or '').startswith('<ERROR>')
    assert f'Priority    {pri}' in _ashow(dev, st, 'show spanning-tree')


def test_sir_ospf_neighbor_requires_an_interface_in_ospf():
    """`ospf use on` だけではOSPFは起動しない

    Si-Rは `lan <n> ip ospf use on` でインタフェースを参加させて
    初めてOSPFプロセスが立つ。それまでは show がエラーを返す。
    """
    dev, st = _sir()
    _aconf(dev, st, ['configure', 'ospf use on', 'ospf area 0.0.0.0'])
    assert 'No OSPF is configured' in _ashow(dev, st, 'show ip ospf neighbor')


def test_sir_ospf_neighbor_when_none():
    dev, st = _sir()
    _aconf(dev, st, ['configure', 'lan 0 ip address 10.60.0.1/24 3',
                     'ospf use on', 'ospf area 0.0.0.0',
                     'lan 0 ip ospf use on'])
    out = _ashow(dev, st, 'show ip ospf neighbor')
    assert 'Neighbor information with all interfaces' in out
    assert 'No neighbors' in out
