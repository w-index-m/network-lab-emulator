"""
engine/loopback_alias.py の sudo フォールバック回帰テスト。

GitHub Actions の pytest workflow (run #54, main branch) が
test_ssh_cli_server.py / test_telnet_cli_server.py /
test_openssh_interop.py / test_nexpose_real_scan.py の実ソケット系
テスト93件を丸ごと `OSError: [Errno 99] Cannot assign requested
address` で落としていた件の回帰テスト。

原因: 開発サンドボックスはrootでpytestを実行するため
`ip addr add ... dev lo` がそのまま通るが、GitHub Actionsの
ubuntu-latestランナーの既定ユーザ"runner"はrootではなく
（パスワード無しsudoは使える）、素の`ip addr add`が
"Operation not permitted"で失敗していた。ensure_loopback_alias側は
元々失敗を握りつぶす仕様なので、エラーメッセージも出ないまま
後続のソケットbindが Errno 99 で落ちる形で発覚した。

このテストはsubprocess.runをモックし、「素のip addrがPermission
deniedで失敗したら sudo -n ip addr add にリトライする」フォールバック
経路だけを固定する（実際にsudo/ip権限が必要なCI固有の挙動は、
ネットワーク名前空間を操作できないこのテスト環境では再現できないため）。
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine import loopback_alias as la


def _completed(returncode, stderr=b''):
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
    return r


def test_success_on_first_try_does_not_call_sudo():
    la._added.discard('10.250.0.1')
    with patch('engine.loopback_alias.subprocess.run') as m:
        m.return_value = _completed(0)
        assert la.ensure_loopback_alias('10.250.0.1') is True
    assert m.call_count == 1
    assert 'sudo' not in m.call_args[0][0]


def test_permission_denied_falls_back_to_sudo_and_succeeds():
    la._added.discard('10.250.0.2')
    with patch('engine.loopback_alias.subprocess.run') as m:
        m.side_effect = [
            _completed(2, stderr=b'RTNETLINK answers: Operation not permitted'),
            _completed(0),
        ]
        assert la.ensure_loopback_alias('10.250.0.2') is True
    assert m.call_count == 2
    first_cmd, second_cmd = m.call_args_list[0][0][0], m.call_args_list[1][0][0]
    assert 'sudo' not in first_cmd
    assert second_cmd[:2] == ['sudo', '-n']
    assert '10.250.0.2' in la._added


def test_permission_denied_and_sudo_also_fails_returns_false():
    la._added.discard('10.250.0.3')
    with patch('engine.loopback_alias.subprocess.run') as m:
        m.side_effect = [
            _completed(2, stderr=b'Operation not permitted'),
            _completed(1, stderr=b'sudo: a password is required'),
        ]
        assert la.ensure_loopback_alias('10.250.0.3') is False
    assert '10.250.0.3' not in la._added


def test_file_exists_is_treated_as_success_without_sudo():
    la._added.discard('10.250.0.4')
    with patch('engine.loopback_alias.subprocess.run') as m:
        m.return_value = _completed(2, stderr=b'RTNETLINK answers: File exists')
        assert la.ensure_loopback_alias('10.250.0.4') is True
    assert m.call_count == 1


def test_already_added_is_cached_and_skips_subprocess():
    la._added.add('10.250.0.5')
    with patch('engine.loopback_alias.subprocess.run') as m:
        assert la.ensure_loopback_alias('10.250.0.5') is True
    m.assert_not_called()
    la._added.discard('10.250.0.5')


def test_loopback_itself_is_always_ok_without_subprocess():
    with patch('engine.loopback_alias.subprocess.run') as m:
        assert la.ensure_loopback_alias('127.0.0.1') is True
    m.assert_not_called()
