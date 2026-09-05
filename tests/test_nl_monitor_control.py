"""
tools/nl_monitor_control.py テスト

- 自然言語からIP/インターフェース/操作を抽出する正規表現ベース解析
  （日本語直後だと \\b が機能しないバグを修正した箇所）
- watchlistの追加/重複防止/削除
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.nl_monitor_control import (
    _rule_based_parse, _norm_iface, _parse_bulk,
)


@pytest.fixture(autouse=True)
def _isolate_watchlist(tmp_path, monkeypatch):
    """テスト間でwatchlistファイルが干渉しないよう一時パスに差し替える"""
    import tools.nl_monitor_control as mod
    tmp_file = tmp_path / 'watchlist.json'
    monkeypatch.setattr(mod, 'WATCHLIST_PATH', tmp_file)
    yield


def test_parse_extracts_ip_immediately_followed_by_japanese():
    """日本語文字の直後だと \\b が機能せず抽出漏れするバグの修正確認"""
    parsed = _rule_based_parse('192.168.10.1のVlan10を監視対象に追加して')
    assert parsed.ip == '192.168.10.1'
    assert parsed.iface == 'Vlan10'
    assert parsed.action == 'add'


def test_parse_shorthand_interface():
    parsed = _rule_based_parse('192.168.1.1のgi1/1を監視対象に追加して')
    assert parsed.ip == '192.168.1.1'
    assert parsed.iface == 'gi1/1'


def test_parse_full_interface_name():
    parsed = _rule_based_parse('10.4.1.1のGigabitEthernet1/0/1を監視対象から外して')
    assert parsed.ip == '10.4.1.1'
    assert parsed.iface == 'GigabitEthernet1/0/1'
    assert parsed.action == 'remove'


def test_parse_missing_ip_or_iface_returns_none():
    parsed = _rule_based_parse('よろしくお願いします')
    assert parsed.ip is None
    assert parsed.iface is None


def test_norm_iface_matches_shorthand_and_full_name():
    assert _norm_iface('Gi1/0/1') == _norm_iface('GigabitEthernet1/0/1')
    assert _norm_iface('gi1/0/1') == _norm_iface('GigabitEthernet1/0/1')


def test_add_and_list_watchlist_entry():
    import tools.nl_monitor_control as mod
    added = mod.add_watchlist_entry('catalyst', 'Dist-SW', 'GigabitEthernet1/0/1', '10.9.9.1')
    assert added is True
    entries = mod.load_watchlist()
    assert len(entries) == 1
    assert entries[0]['device_id'] == 'catalyst'


def test_add_duplicate_returns_false():
    import tools.nl_monitor_control as mod
    mod.add_watchlist_entry('catalyst', 'Dist-SW', 'GigabitEthernet1/0/1', '10.9.9.1')
    added_again = mod.add_watchlist_entry('catalyst', 'Dist-SW', 'Gi1/0/1', '10.9.9.1')
    assert added_again is False
    assert len(mod.load_watchlist()) == 1


def test_remove_entry():
    import tools.nl_monitor_control as mod
    mod.add_watchlist_entry('catalyst', 'Dist-SW', 'GigabitEthernet1/0/1', '10.9.9.1')
    removed = mod.remove_watchlist_entry('catalyst', 'Gi1/0/1')
    assert removed is True
    assert mod.load_watchlist() == []


def test_remove_nonexistent_returns_false():
    import tools.nl_monitor_control as mod
    removed = mod.remove_watchlist_entry('catalyst', 'GigabitEthernet9/9/9')
    assert removed is False


def test_parse_bulk_all_ports():
    bulk = _parse_bulk('catalystの全ポートを監視対象にして')
    assert bulk is not None
    assert bulk.device_hint == 'catalyst'
    assert bulk.scope == 'all'
    assert bulk.action == 'add'


def test_parse_bulk_link_up_only():
    bulk = _parse_bulk('catalystのlink upしているポートを監視対象にして')
    assert bulk is not None
    assert bulk.scope == 'up'


def test_parse_bulk_remove_action():
    bulk = _parse_bulk('catalystの全ポートを監視対象から外して')
    assert bulk is not None
    assert bulk.action == 'remove'


def test_parse_bulk_returns_none_when_ip_present():
    # IPアドレス付きの単一IF指定は一括指定として誤検知してはいけない
    assert _parse_bulk('10.9.9.1のGi1/0/1を監視対象にして') is None


def test_parse_bulk_returns_none_without_scope_or_device_hint():
    assert _parse_bulk('こんにちは') is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
