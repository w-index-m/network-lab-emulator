"""
show ip route / show vlan の出力が実機IOS-XEと同じ形かのテスト

実機(WS-C3650-24TD / IOS-XE 16.12.11)の出力と突き合わせて見つかった
3つの食い違いに対する回帰テスト。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from engine.protocols import RibEngine, VlanEngine, icmp_engine
from engine.rules import DeviceState, RuleEngine


# ── ① connected経路の書式 / ② L(local)経路 ──────────────────

@pytest.fixture
def rib():
    e = RibEngine()
    icmp_engine.register_device(
        'd', 'SW', {'Vlan10': {'ip': '192.168.10.1', 'prefix': 24}})
    e.add_static_route('d', 'SW', '192.168.10.0', 24, '0.0.0.0', 0)
    e.add_static_route('d', 'SW', '0.0.0.0', 0, '203.0.113.1', 1)
    return e


def test_connected_route_uses_real_ios_wording(rib):
    """connected経路に "[AD/metric] via ..." は付かない

    以前は静的経路と同じ書式で描画しており、next-hopが 'directly' の
    ため "C 192.168.10.0/24 [0/0] via directly, Vlan10" という
    実機に存在しない表記になっていた。
    """
    out = rib.format_show_ip_route('d')
    assert 'is directly connected, Vlan10' in out, out
    assert 'via directly' not in out, out
    assert 'C        192.168.10.0/24 is directly connected' in out, out


def test_local_route_is_present(rib):
    """実機はインターフェースIPの /32 を L 経路として持つ"""
    out = rib.format_show_ip_route('d')
    assert 'L        192.168.10.1/32 is directly connected, Vlan10' in out, out


def test_gateway_of_last_resort_line(rib):
    """デフォルトルートの有無を実機同様に明示する"""
    out = rib.format_show_ip_route('d')
    assert 'Gateway of last resort is 203.0.113.1 to network 0.0.0.0' in out, out

    empty = RibEngine()
    empty.add_static_route('e', 'SW', '10.0.0.0', 8, '10.1.1.1', 1)
    assert 'Gateway of last resort is not set' in empty.format_show_ip_route('e')


def test_default_route_is_flagged_with_star(rib):
    """候補デフォルトルートには * が付く（実機の S* 表記）"""
    out = rib.format_show_ip_route('d')
    assert 'S*' in out, out


def test_rules_renderer_also_uses_real_wording():
    """rules.py側のフォールバック描画も同じ書式であること

    設定を一切入れていない起動直後は、エンジンではなくこちらが使われる。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    out = engine.process('show ip route', state)
    assert 'via directly' not in out, out
    assert 'is directly connected' in out, out
    assert 'Gateway of last resort is' in out, out


# ── ③ VLAN1の既定ポート所属 ─────────────────────────────

@pytest.fixture
def vlan():
    e = VlanEngine()
    e.register_ports('d', [f'GigabitEthernet1/0/{i}' for i in range(1, 6)]
                     + ['Vlan1', 'Port-channel1'])
    return e


def test_unassigned_ports_belong_to_vlan1(vlan):
    """実機ではaccessポートは既定でVLAN1に所属する

    以前はVLAN1のPorts欄が常に空で、Genieの vlans.1.interfaces も
    取れなかった。
    """
    out = vlan.format_show_vlan('d', brief=True)
    for port in ('Gi1/0/1', 'Gi1/0/2', 'Gi1/0/5'):
        assert port in out, f'{port} がVLAN1に出ていない:\n{out}'


def test_svi_and_portchannel_are_not_vlan_members(vlan):
    """SVIや論理インターフェースはVLANのメンバーポートではない"""
    out = vlan.format_show_vlan('d', brief=True)
    assert 'Vlan1,' not in out and 'Port-channel1' not in out, out


def test_default_reserved_vlans_exist(vlan):
    """実機が必ず持つ 1002-1005 が存在する"""
    out = vlan.format_show_vlan('d', brief=True)
    for vid, name in ((1002, 'fddi-default'), (1003, 'token-ring-default'),
                      (1004, 'fddinet-default'), (1005, 'trnet-default')):
        assert f'{vid}' in out and name in out, f'VLAN {vid} が無い:\n{out}'
    assert 'act/unsup' in out


def test_ports_wrap_like_real_device(vlan):
    """ポートが多い場合はPorts列幅で折り返す（実機と同じ継続行）"""
    vlan.register_ports('d2', [f'GigabitEthernet1/0/{i}' for i in range(1, 25)])
    out = vlan.format_show_vlan('d2', brief=True)
    body = [l for l in out.splitlines() if 'Gi1/0/' in l]
    assert len(body) > 1, f'折り返されていない:\n{out}'
    # 継続行はPorts列までインデントされる
    assert body[1].startswith(' ' * 48), repr(body[1])


# ── 実機比較で見つかった追加の食い違い ────────────────────

def test_mac_table_has_no_explanatory_text():
    """MACテーブルに説明文を混ぜない（装置の出力として成立させる）

    以前は学習が0件のとき日本語の案内文が装置の出力に混ざっており、
    パーサーが解釈できない行になっていた。
    """
    from engine.protocols import DataPlaneEngine

    out = DataPlaneEngine().format_mac_table('mactest-empty')
    assert '動的に学習' not in out, out
    # 行はヘッダ・区切り・エントリ・合計のいずれかに限られる
    for line in out.splitlines():
        if not line.strip():
            continue
        ok = line.startswith((' ', '-', 'Vlan', 'Total')) or 'Mac Address Table' in line
        assert ok, f'想定外の行が出ている: {line!r}'


def test_lldp_reports_not_enabled_until_lldp_run():
    """LLDPは実機同様、lldp run を入れるまで無効

    有効化していないのに隣接テーブルを表示すると、機能が動いて
    いるように見えてしまう（EIGRPの幽霊ネイバーと同じ問題）。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')

    assert engine.process('show lldp neighbors', state) == '% LLDP is not enabled'
    assert engine.process('show lldp detail', state) == '% LLDP is not enabled'

    state.mode = 'config'
    engine.process('lldp run', state)
    assert engine.process('show lldp neighbors', state) != '% LLDP is not enabled'

    engine.process('no lldp run', state)
    assert engine.process('show lldp neighbors', state) == '% LLDP is not enabled'


@pytest.mark.parametrize('raw,expected', [
    ('00:a6:ca:54:36:00', '00a6.ca54.3600'),
    ('00-a6-ca-54-36-00', '00a6.ca54.3600'),
    ('00a6.ca54.3600',    '00a6.ca54.3600'),
])
def test_cisco_mac_formatting(raw, expected):
    """MACはCisco表記(ドット区切り)にする。コロン区切りは実機に無い"""
    from engine.protocols import cisco_mac
    assert cisco_mac(raw) == expected
    assert RuleEngine.cisco_mac(raw) == expected


def test_cisco_mac_leaves_unparseable_value_alone():
    from engine.protocols import cisco_mac
    assert cisco_mac('') == ''
    assert cisco_mac('CPU') == 'CPU'


def test_arp_output_uses_dotted_mac():
    """show ip arp のMACがドット区切りで、列がずれないこと"""
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    out = engine.process('show ip arp', state)
    assert ':' not in out.replace('Age (min)', ''), out
    assert '.' in out


def test_spanning_tree_priority_includes_sys_id_ext():
    """Root ID の Priority が空欄にならず、VLAN番号が加算されること

    実機は "priority + sys-id-ext" を表示する（VLAN10 なら 32778）。
    以前は Root ID の Priority が空欄で、Bridge ID もVLAN番号を
    足していなかった。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    out = engine.process('show spanning-tree', state)

    assert 'Root ID    Priority    32778' in out, out
    assert 'Bridge ID  Priority    32778  (priority 32768 sys-id-ext 10)' in out, out
    # MACはドット区切り。優先度プレフィックス(8001.)が残っていないこと
    assert '8001.' not in out, out
    for line in out.splitlines():
        if 'Address' in line:
            assert ':' not in line, f'コロン区切りのMACが残っている: {line}'


# ── 実機比較 第3弾（CPUエントリ / Flags / Capability Codes）──────

def test_mac_table_has_cpu_reserved_entries():
    """実機は学習が0件でも予約マルチキャストMACを21件持つ

    0100.0ccc.cccc(CDP/VTP)、0180.c200.000x(STP/LLDP等)、
    ffff.ffff.ffff(broadcast) が STATIC/CPU として常に載る。
    """
    from engine.protocols import DataPlaneEngine

    # icmp_engineはモジュール共有のため、他テストがSVIを登録していない
    # 装置IDを使う（登録済みIDだとSVI分がエントリ数に加わる）
    out = DataPlaneEngine().format_mac_table('mactest-empty')
    for mac in ('0100.0ccc.cccc', '0100.0ccc.cccd',
                '0180.c200.0000', '0180.c200.0010',
                '0180.c200.0021', 'ffff.ffff.ffff'):
        assert mac in out, f'{mac} が無い:\n{out}'
    assert out.count('STATIC      CPU') == 21, out
    assert 'Total Mac Addresses for this criterion: 21' in out


def test_mac_table_column_widths_match_real_device():
    """列幅が実機と一致すること（Vlanは右寄せ、MACは左18、Typeは左12）"""
    from engine.protocols import DataPlaneEngine

    out = DataPlaneEngine().format_mac_table('mactest-empty')
    row = [l for l in out.splitlines() if '0100.0ccc.cccc' in l][0]
    assert row == ' All    0100.0ccc.cccc    STATIC      CPU', repr(row)


def test_etherchannel_summary_has_full_flag_legend():
    """Flags説明が実機と同じ項目を持つ（以前は2行しか無かった）"""
    from engine.protocols import LacpEngine

    out = LacpEngine().format_etherchannel_summary('d')
    for flag in ('H - Hot-standby (LACP only)', 'R - Layer3', 'S - Layer2',
                 'U - in use', 'f - failed to allocate aggregator',
                 'M - not in use, minimum links not met',
                 'u - unsuitable for bundling',
                 'w - waiting to be aggregated',
                 'd - default port', 'A - formed by Auto LAG'):
        assert flag in out, f'{flag!r} が無い:\n{out}'


def test_etherchannel_summary_shows_group_header_when_empty():
    """チャネルが0個でも実機はGroupテーブルのヘッダを出す"""
    from engine.protocols import LacpEngine

    out = LacpEngine().format_etherchannel_summary('d')
    assert 'Number of channel-groups in use: 0' in out
    assert 'Group  Port-channel  Protocol    Ports' in out, out
    assert '------+-------------+-----------+' in out, out


def test_cdp_capability_codes_has_three_lines():
    """Capability Codes が実機同様3行（P/D/C/M の説明を含む）"""
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    out = engine.process('show cdp neighbors', state)
    assert 'P - Phone' in out, out
    assert 'D - Remote, C - CVTA, M - Two-port Mac Relay' in out, out


# ── show interfaces の実機比較 ────────────────────────────

@pytest.fixture
def intf_output():
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    state.mode = 'exec'
    return engine.process('show interfaces GigabitEthernet1/0/1', state)


def test_interfaces_has_output_broadcast_line(intf_output):
    """出力側のブロードキャスト内訳行があること

    実機は "Output N broadcasts (M multicasts)" を必ず出す。
    この1行だけが丸ごと欠けており、Genieの
    counters.out_broadcast_pkts が取れなかった。
    """
    assert 'Output ' in intf_output and 'broadcasts (' in intf_output, intf_output
    lines = intf_output.splitlines()
    out_idx = next(i for i, l in enumerate(lines) if 'packets output' in l)
    # 実機では packets output の直後に来る
    assert 'broadcasts' in lines[out_idx + 1], lines[out_idx:out_idx + 3]


def test_switch_input_queue_size_matches_platform(intf_output):
    """Catalystの入力キュー上限は2000（ルーター系の75ではない）"""
    assert 'Input queue: 0/2000/0/0' in intf_output, intf_output

    router = RuleEngine().process(
        'show interfaces GigabitEthernet0/0/0', DeviceState('cisco', 'ISR'))
    assert 'Input queue: 0/75/0/0' in router, router


def test_interfaces_counter_block_order(intf_output):
    """カウンタ行の並びが実機と同じであること"""
    expected = [
        'packets input', 'Received', 'runts', 'input errors', 'watchdog',
        'dribble condition', 'packets output', 'Output', 'output errors',
        'unknown protocol drops', 'babbles', 'lost carrier',
        'output buffer failures',
    ]
    lines = intf_output.splitlines()
    pos = -1
    for needle in expected:
        idx = next((i for i, l in enumerate(lines)
                    if needle in l and i > pos), None)
        assert idx is not None, f'{needle!r} の行が無い:\n{intf_output}'
        pos = idx


# ── show tech-support 由来の未実装コマンド ────────────────

@pytest.fixture
def cat():
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    state.mode = 'exec'
    return engine, state


def test_show_interfaces_counters_has_two_tables(cat):
    """実機は In と Out の2テーブルを出す（以前は未実装でエラー）"""
    engine, state = cat
    out = engine.process('show interfaces counters', state)
    assert 'Invalid input' not in out, out
    assert 'InOctets' in out and 'OutOctets' in out, out
    heads = [l for l in out.splitlines() if l.startswith('Port')]
    assert len(heads) == 2, heads


def test_show_interfaces_counters_column_positions_match_real(cat):
    """桁位置が実機(WS-C3650-24TD)と一致すること"""
    engine, state = cat
    out = engine.process('show interfaces counters', state)
    lines = out.splitlines()
    assert lines[1] == ('Port               InOctets    InUcastPkts'
                        '    InMcastPkts    InBcastPkts '), repr(lines[1])
    row = next(l for l in lines if l.startswith('Gi'))
    # 実機の値は 27 / 42 / 57 / 72 桁目で終わる
    ends = [i + 1 for i, ch in enumerate(row)
            if ch.isdigit() and not row[i + 1:i + 2].isdigit()]
    assert ends[-4:] == [27, 42, 57, 72], (ends, repr(row))


def test_show_interfaces_switchport_block_format(cat):
    """ポート単位ブロックが実機と同じ行構成であること"""
    engine, state = cat
    out = engine.process('show interfaces switchport', state)
    assert 'Invalid input' not in out, out
    assert out.count('Switchport: Enabled') >= 1, out
    for line in ('Administrative Trunking Encapsulation: dot1q',
                 'Pruning VLANs Enabled: 2-1001',
                 'Capture Mode Disabled',
                 'Appliance trust: none'):
        assert line in out, (line, out[:400])


def test_switchport_down_port_has_no_operational_encapsulation():
    """downポートに Operational Trunking Encapsulation 行は出ない

    実機の未接続ポートは dynamic auto / Operational Mode: down で、
    この行を持たない。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    for iface in state.interfaces.values():
        iface['status'] = 'notconnect'
        iface.pop('vlan', None)
    out = engine.process('show interfaces switchport', state)
    assert 'Administrative Mode: dynamic auto' in out, out[:400]
    assert 'Operational Mode: down' in out, out[:400]
    assert 'Operational Trunking Encapsulation' not in out, out[:400]
    assert 'Negotiation of Trunking: On' in out, out[:400]


def test_show_spanning_tree_summary_is_not_an_error(cat):
    """実機は必ずSTPが動いており "not configured" とは言わない"""
    engine, state = cat
    out = engine.process('show spanning-tree summary', state)
    assert 'not configured' not in out, out
    assert 'Switch is in rapid-pvst mode' in out, out
    assert 'Configured Pathcost method used is short' in out, out


def test_stp_summary_counter_columns_match_real(cat):
    """集計行の桁位置が実機と一致すること（実機はヘッダと1桁ずれる）"""
    engine, state = cat
    out = engine.process('show spanning-tree summary', state)
    row = next(l for l in out.splitlines() if l.startswith('VLAN0001'))
    ends = [i for i, ch in enumerate(row)
            if ch.isdigit() and not row[i + 1:i + 2].isdigit()]
    assert ends[-5:] == [29, 39, 48, 59, 70], (ends, repr(row))


def test_show_file_systems_marks_flash_as_default(cat):
    engine, state = cat
    out = engine.process('show file systems', state)
    assert 'Invalid input' not in out, out
    assert '       Size(b)       Free(b)      Type  Flags  Prefixes' in out, out
    flash = next(l for l in out.splitlines() if l.endswith('flash:'))
    assert flash.startswith('*'), repr(flash)


def test_show_redundancy_states_reports_simplex(cat):
    """スタンドアロン機は Simplex / Non-redundant"""
    engine, state = cat
    out = engine.process('show redundancy states', state)
    assert 'Invalid input' not in out, out
    assert 'Mode = Simplex' in out, out
    assert 'Redundancy Mode (Operational) = Non-redundant' in out, out


def test_show_interfaces_trunk_prints_nothing_without_trunks():
    """トランクが無いとき実機は日本語メッセージではなく無出力"""
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    for iface in state.interfaces.values():
        iface.pop('vlan', None)
    out = engine.process('show interfaces trunk', state)
    assert out.strip() == '', repr(out)
