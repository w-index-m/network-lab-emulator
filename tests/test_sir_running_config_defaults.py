"""
Si-R の show running-config 末尾（コンソール/リモートアクセスの既定設定）
が実機と一致するかのテスト

実機 Si-R G110B の show running-config（2026-09-04、初回ログイン直後の
factory-default状態）と突き合わせて見つかった食い違いへの回帰テスト:

- "consoleinfo authtype password" / "telnetinfo authtype password" は
  実機のデフォルトには存在しない（実機は autologout 8h / 5m を出す）
- "rebootlog use on" は実機のデフォルト出力に存在しない
- "syslog facility 1" ではなく実機は "syslog facility 23"
- "terminal pager enable" が丸ごと欠けていた
- "save" はアクションコマンドであって running-config の行ではないのに
  末尾に紛れ込んでいた
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _fresh_sir(device_id: str):
    client.post('/api/device', json={
        'id': device_id, 'type': 'sir', 'hostname': device_id,
    })
    return client.post('/api/cli', json={
        'device_id': device_id, 'command': 'show run',
    }).json()['output']


def test_default_console_and_telnet_lines_match_real_device():
    out = _fresh_sir('sir-test-defaults-1')
    assert 'consoleinfo autologout 8h' in out, out
    assert 'telnetinfo autologout 5m' in out, out
    assert 'authtype password' not in out, out


def test_default_syslog_facility_matches_real_device():
    out = _fresh_sir('sir-test-defaults-2')
    assert 'syslog facility 23' in out, out
    assert 'syslog facility 1' not in out, out


def test_terminal_pager_enable_is_present():
    out = _fresh_sir('sir-test-defaults-3')
    assert 'terminal pager enable' in out, out


def test_rebootlog_line_is_not_fabricated():
    out = _fresh_sir('sir-test-defaults-4')
    assert 'rebootlog' not in out, out


def test_save_is_not_echoed_as_a_config_line():
    out = _fresh_sir('sir-test-defaults-5')
    assert not any(line.strip() == 'save' for line in out.splitlines()), out


def test_terminal_charset_sjis_is_still_present():
    """実機に実在する行は残っていること（退行防止）"""
    out = _fresh_sir('sir-test-defaults-6')
    assert 'terminal charset SJIS' in out, out
