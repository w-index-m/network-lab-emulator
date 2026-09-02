"""
tools/device_log_to_loki.py テスト

`show logging`出力からタイムスタンプ付きログ行のみを抽出するロジックを
検証する。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.device_log_to_loki import extract_log_lines


def test_extract_log_lines_picks_timestamped_lines():
    out = (
        'Syslog logging: enabled (0 messages dropped, 0 messages rate-limited,\n'
        '                0 flushes, 0 overruns, xml disabled, filtering disabled)\n'
        '\n'
        '    Console logging: level debugging, 4 messages logged\n'
        '\n'
        '*Sep 02 07:03:31.797: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down\n'
        '*Sep 02 07:03:31.797: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/1, changed state to down\n'
    )
    lines = extract_log_lines(out)
    assert len(lines) == 2
    assert all(l.startswith('*Sep 02') for l in lines)
    assert 'LINK-3-UPDOWN' in lines[0]


def test_extract_log_lines_empty_when_no_timestamped_lines():
    out = 'Syslog logging: enabled\nConsole logging: level debugging, 0 messages logged\n'
    assert extract_log_lines(out) == []


def test_extract_log_lines_strips_whitespace():
    out = '   *Sep 02 07:03:31.797: %SYS-5-CONFIG_I: Configured from console\n'
    lines = extract_log_lines(out)
    assert lines == ['*Sep 02 07:03:31.797: %SYS-5-CONFIG_I: Configured from console']
