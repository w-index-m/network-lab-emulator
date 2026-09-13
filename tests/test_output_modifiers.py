"""
出力モディファイア（`show ... | include foo`）

実装が無く、`|` 以降が黙って無視されて全文が返っていた。
`show running-config | include transport input` が効かないことに
気付いたのが発端。

実機に合わせた点:
  - 引数は正規表現（`^hostname` など）
  - 大文字小文字は区別する
  - 短縮形を受ける（`| i` / `| exc` / `| beg` / `| sec`）
  - `section` は一致行＋その配下のインデント行

意図的にそうしている点:
  - `|` を分割するのは show / dir / more のときだけ。設定コマンドには
    出力モディファイアが無く、逆に `|` を値として含むもの
    （`ip as-path access-list 1 permit ^$|^100$` 等）があるため
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient       # noqa: E402

import app as app_module                        # noqa: E402
from app import (_apply_output_modifier,        # noqa: E402
                 _split_output_modifier)

client = TestClient(app_module.app)
DEV = 't-pipe'

SAMPLE = """hostname SW1
!
interface GigabitEthernet1/0/1
 description uplink
 ip address 10.0.0.1 255.255.255.0
!
interface GigabitEthernet1/0/2
 shutdown
!
line vty 0 4
 transport input ssh
end"""


def _cli(cmd):
    return client.post('/api/cli',
                       json={'device_id': DEV, 'command': cmd}).json()['output']


@pytest.fixture
def device():
    client.delete(f'/api/device/{DEV}')
    client.post('/api/device',
                json={'id': DEV, 'type': 'catalyst', 'hostname': 'PIPE-SW'})
    for c in ('configure terminal', 'interface GigabitEthernet1/0/1',
              'no switchport', 'ip address 10.230.0.1 255.255.255.0',
              'no shutdown', 'exit', 'snmp-server community public ro',
              'line vty 0 4', 'transport input all', 'end'):
        _cli(c)
    yield
    client.delete(f'/api/device/{DEV}')


# ══════════════════════════════════════════
# コマンドの分解
# ══════════════════════════════════════════
def test_split_recognises_the_modifiers():
    assert _split_output_modifier('show run | include foo') == \
        ('show run', 'include', 'foo')
    assert _split_output_modifier('show run | exclude foo') == \
        ('show run', 'exclude', 'foo')
    assert _split_output_modifier('show run | begin foo') == \
        ('show run', 'begin', 'foo')
    assert _split_output_modifier('show run | section foo') == \
        ('show run', 'section', 'foo')
    assert _split_output_modifier('show run | count') == \
        ('show run', 'count', '')


def test_split_accepts_abbreviations():
    assert _split_output_modifier('sh run | i foo')[1] == 'include'
    assert _split_output_modifier('sh run | exc foo')[1] == 'exclude'
    assert _split_output_modifier('sh run | beg foo')[1] == 'begin'
    assert _split_output_modifier('sh run | sec foo')[1] == 'section'


def test_split_ignores_commands_without_a_pipe():
    assert _split_output_modifier('show running-config') is None


def test_config_commands_keep_their_pipe():
    """設定値の `|` を出力モディファイアと誤認しないこと

    `ip as-path access-list 1 permit ^$|^100$` のように、`|` を
    値として含む設定コマンドがある。
    """
    assert _split_output_modifier(
        'ip as-path access-list 1 permit ^$|^100$') is None
    assert _split_output_modifier(
        'ip prefix-list X permit 0.0.0.0/0 | foo') is None


def test_unknown_modifier_is_not_treated_as_one():
    assert _split_output_modifier('show run | frobnicate foo') is None
    assert _split_output_modifier('show run |') is None
    assert _split_output_modifier('show run | include') is None


# ══════════════════════════════════════════
# 絞り込み
# ══════════════════════════════════════════
def test_include_keeps_only_matching_lines():
    out = _apply_output_modifier(SAMPLE, 'include', 'interface')
    assert out.splitlines() == ['interface GigabitEthernet1/0/1',
                                'interface GigabitEthernet1/0/2']


def test_include_takes_a_regex():
    assert _apply_output_modifier(SAMPLE, 'include', '^hostname') == \
        'hostname SW1'


def test_include_is_case_sensitive():
    """実機同様、大文字小文字は区別する"""
    assert _apply_output_modifier(SAMPLE, 'include', 'HOSTNAME') == ''


def test_exclude_drops_matching_lines():
    out = _apply_output_modifier(SAMPLE, 'exclude', 'interface')
    assert 'interface' not in out
    assert 'hostname SW1' in out


def test_begin_starts_at_the_first_match():
    out = _apply_output_modifier(SAMPLE, 'begin', 'line vty')
    assert out.startswith('line vty 0 4')
    assert 'hostname' not in out
    assert out.endswith('end')


def test_begin_with_no_match_returns_nothing():
    assert _apply_output_modifier(SAMPLE, 'begin', 'no-such-line') == ''


def test_section_keeps_the_matching_line_and_its_block():
    out = _apply_output_modifier(SAMPLE, 'section',
                                 'interface GigabitEthernet1/0/1')
    assert out.splitlines() == ['interface GigabitEthernet1/0/1',
                                ' description uplink',
                                ' ip address 10.0.0.1 255.255.255.0']


def test_section_stops_at_the_next_unindented_line():
    out = _apply_output_modifier(SAMPLE, 'section', 'line vty')
    assert out.splitlines() == ['line vty 0 4', ' transport input ssh']


def test_count_returns_the_number_of_lines():
    assert _apply_output_modifier(SAMPLE, 'count', '') == \
        str(len(SAMPLE.splitlines()))


def test_a_broken_regex_does_not_raise():
    """壊れたパターンを投げられても落ちないこと"""
    assert _apply_output_modifier(SAMPLE, 'include', '*vty') == ''
    assert _apply_output_modifier(SAMPLE, 'include', '(unclosed') == ''


def test_a_broken_regex_falls_back_to_substring_matching():
    text = 'description Site-A (primary)\ndescription Site-B'
    # "(primary" は正規表現として壊れているが、文字列としては存在する
    out = _apply_output_modifier(text, 'include', '(primary')
    assert out == 'description Site-A (primary)'


def test_empty_output_is_handled():
    assert _apply_output_modifier('', 'include', 'x') == ''
    assert _apply_output_modifier('', 'count', '') == '0'


# ══════════════════════════════════════════
# CLI経由（回帰）
# ══════════════════════════════════════════
def test_include_through_the_cli(device):
    """回帰テスト: 以前は `|` 以降が無視されて全文が返っていた"""
    out = _cli('show running-config | include transport input')
    lines = out.splitlines()
    assert lines and all('transport input' in l for l in lines)
    assert 'hostname' not in out


def test_abbreviated_form_through_the_cli(device):
    out = _cli('sh run | i snmp-server community')
    assert out.splitlines() == ['snmp-server community public RO']


def test_section_through_the_cli(device):
    out = _cli('show running-config | section line vty 0 4')
    assert out.startswith('line vty 0 4')
    assert 'transport input all' in out
    assert 'interface' not in out


def test_count_through_the_cli(device):
    full = _cli('show running-config')
    assert _cli('show running-config | count') == str(len(full.splitlines()))


def test_modifier_works_on_other_show_commands(device):
    out = _cli('show ip interface brief | include GigabitEthernet1/0/1')
    assert out.splitlines()[0].startswith('GigabitEthernet1/0/1')
    assert '10.230.0.1' in out


def test_no_match_returns_empty_not_the_whole_output(device):
    """一致が無いときに全文へフォールバックしないこと"""
    assert _cli('show running-config | include no-such-string-here') == ''
