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
    """空のMACテーブルに説明文を混ぜない（実機はヘッダと合計行だけ）

    以前は日本語の案内文が装置の出力に混ざっており、
    パーサーが解釈できない行になっていた。
    """
    from engine.protocols import DataPlaneEngine

    out = DataPlaneEngine().format_mac_table('d')
    assert '動的に学習' not in out, out
    assert 'Total Mac Addresses for this criterion: 0' in out
    body = [l for l in out.splitlines()
            if l and not l.startswith((' ', '-', 'Vlan', 'Total'))]
    assert body == [], f'ヘッダ以外の行が出ている: {body}'


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
