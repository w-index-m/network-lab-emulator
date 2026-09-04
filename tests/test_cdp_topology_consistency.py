"""
CDP/LLDPのネイバー表示が実トポロジー(vnet.links)と一致することのテスト

以前は DeviceState.__init__ が全装置に固定のサンプルネイバー
（Core-SW / GW-Router）を持たせていたため、リンクを一切張っていない
装置でも `show cdp neighbors` に隣接装置が表示されていた。その結果
「CDPでは繋がって見えるのに OSPF/RIP のパケットが一切流れない」という
極めて切り分けにくい食い違いが発生していた（実際に catalyst↔cisco で
踏んだ）。この回帰を防ぐためのテスト。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.rules import DeviceState, RuleEngine


def test_new_device_has_no_phantom_cdp_neighbors():
    """リンクを張っていない装置は CDP/LLDP ネイバーを持たない"""
    state = DeviceState('catalyst', 'Dist-SW')
    assert state.cdp_neighbors == [], \
        'リンク未接続なのにCDPネイバーが存在する（サンプルデータの混入）'
    assert state.lldp_neighbors == [], \
        'リンク未接続なのにLLDPネイバーが存在する（サンプルデータの混入）'


def test_show_cdp_neighbors_empty_for_unlinked_device():
    """リンク未接続の装置の show cdp neighbors は 0 件と表示される"""
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    out = engine._show_cdp(state)
    assert 'Total cdp entries displayed : 0' in out
    # 過去に埋め込まれていたサンプル装置名が出ないこと
    assert 'Core-SW' not in out
    assert 'GW-Router' not in out


def test_show_cdp_columns_do_not_collide():
    """インターフェース名が長くても列が繋がって表示されない

    実インターフェース名(GigabitEthernet1/0/1)は固定幅18文字を超えるため、
    以前は holdtime と繋がって "GigabitEthernet1/0/1150" と表示されていた。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    state.cdp_neighbors = [{
        'device': 'GW-Router', 'local_if': 'GigabitEthernet1/0/1',
        'hold': 150, 'cap': 'R', 'platform': 'CISCO',
        'port': 'GigabitEthernet0/0/0',
    }]
    out = engine._show_cdp(state)
    assert 'GigabitEthernet1/0/1150' not in out, '列がくっついている'
    # 実機IOSと同様に短縮表示される
    assert 'Gig 1/0/1' in out
    assert 'Gig 0/0/0' in out
    # 各カラムの値が独立したトークンとして読めること
    row = [ln for ln in out.splitlines() if ln.startswith('GW-Router')][0]
    assert '150' in row.split()


def test_eigrp_shows_not_configured_instead_of_phantom_neighbor():
    """EIGRPは設定コマンド・エンジンとも未実装なので、未設定と応答する

    以前は DeviceState に固定のサンプルネイバー(10.0.0.2)が入っており、
    EIGRPを一切設定していない装置でも `show ip eigrp neighbors` に
    隣接が確立しているかのように表示されていた（CDPと同じ罠）。
    """
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    assert state.eigrp.get('enabled') is False
    assert state.eigrp['neighbors'] == []

    out = engine._show_eigrp_neighbors(state)
    assert 'not configured' in out
    assert '10.0.0.2' not in out, '実在しないEIGRPネイバーが表示されている'

    topo = engine._show_eigrp_topology(state)
    assert 'not configured' in topo


def test_cdp_abbrev_if_variants():
    assert RuleEngine._cdp_abbrev_if('GigabitEthernet1/0/1') == 'Gig 1/0/1'
    assert RuleEngine._cdp_abbrev_if('TenGigabitEthernet1/1') == 'Ten 1/1'
    assert RuleEngine._cdp_abbrev_if('FastEthernet0/1') == 'Fas 0/1'
    # 短縮対象外の名前はそのまま
    assert RuleEngine._cdp_abbrev_if('lan0') == 'lan0'
    assert RuleEngine._cdp_abbrev_if('') == ''
