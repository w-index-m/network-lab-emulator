"""
show version の出力が実機IOS-XEと同じ項目を持つことのテスト

実機(WS-C3650-24TD / IOS-XE 16.12.11)の show version をGenieパーサーに
通した結果と突き合わせたところ、エミュレーターの出力には機種に依らず
IOS-XEなら必ず出る項目が27キー分欠けていた（ROM/BOOTLDR、Compiled行、
System image file、Last reload reason、ライセンス情報、ディスク情報、
次回リロード時のconfig register等）。

Genie本体はこのリポジトリの実行環境には入っていないため、ここでは
パーサーが手掛かりにする「行」が出力に含まれるかを直接検証する。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from engine.rules import DeviceState, RuleEngine


@pytest.fixture
def catalyst_version_output():
    engine = RuleEngine()
    state = DeviceState('catalyst', 'Dist-SW')
    return engine.process('show version', state)


@pytest.mark.parametrize('needle,why', [
    ('Cisco IOS-XE software, Copyright',
     "この行が無いとGenieは os を 'IOS-XE' でなく 'IOS' と判定する"),
    ('Compiled ', 'compiled_date / compiled_by'),
    ('ROM: ', 'version.rom'),
    ('BOOTLDR: ', 'version.bootldr'),
    ('System image file is "', 'version.system_image'),
    ('Last reload reason:', 'version.last_reload_reason'),
    ('System returned to ROM by', 'version.returned_to_rom_by'),
    ('Uptime for this control processor is', 'version.uptime_this_cp'),
    ('Technology Package License Information:', 'version.license_package.*'),
    ('bytes of Flash at flash:.', 'version.disks'),
    ('Base Ethernet MAC Address', 'version.switch_num.*.mac_address'),
    ('Motherboard Serial Number', 'version.switch_num.*.mb_sn'),
    ('Model Number', 'version.switch_num.*.model_num'),
    ('System Serial Number', 'version.switch_num.*.system_sn'),
    ('Switch Ports Model', 'version.switch_num テーブル'),
])
def test_show_version_contains_iosxe_field(catalyst_version_output, needle, why):
    assert needle in catalyst_version_output, \
        f'show version に {needle!r} が無い（{why} が取れなくなる）'


def test_config_register_reports_next_reload_value(catalyst_version_output):
    """実機同様 "(will be ... at next reload)" 形式で出す

    この形式でないとGenieは next_config_register を抽出できない。
    """
    assert 'Configuration register is 0x102 (will be 0x102 at next reload)' \
        in catalyst_version_output, catalyst_version_output


def test_uptime_line_uses_hostname(catalyst_version_output):
    """Genieは "<hostname> uptime is ..." から hostname と uptime を取る"""
    assert 'Dist-SW uptime is ' in catalyst_version_output
