"""
tools/MockNetLbfo/MockNetLbfo.psm1 の動作確認テスト。

pwsh (PowerShell 7, cross-platform) がインストールされている環境でのみ
実行される。無ければ自動skip。
"""

import os
import shutil
import subprocess

import pytest

PWSH = shutil.which("pwsh")
MODULE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'tools', 'MockNetLbfo', 'MockNetLbfo.psm1'
)

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh (PowerShell 7) not installed")


def _run_ps(script: str) -> str:
    """スクリプトを実行し、最後の空でない行を返す。

    モジュール内のWrite-Host呼び出しもこの非対話実行環境ではstdoutに
    混ざって出力されるため、テスト対象の最終出力行だけを抽出する。
    """
    result = subprocess.run(
        [PWSH, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"pwsh failed: {result.stderr}"
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def test_get_netadapter_returns_four_mock_adapters():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        (Get-NetAdapter).Count
    """)
    assert out == "4"


def test_new_netlbfoteam_creates_team_with_members():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 -TeamingMode LACP | Out-Null
        $t = Get-NetLbfoTeam -Name Team1
        "$($t.TeamingMode)|$($t.Members -join ',')|$($t.Status)"
    """)
    assert out == "LACP|Ethernet1,Ethernet2|Up"


def test_new_netlbfoteam_rejects_adapter_already_in_team():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        try {{
            New-NetLbfoTeam -Name Team2 -TeamMembers Ethernet1,Ethernet3 -ErrorAction Stop
            "NO_ERROR"
        }} catch {{
            "ERROR_RAISED"
        }}
    """)
    assert out == "ERROR_RAISED"


def test_failover_marks_team_degraded_then_recovers():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        Set-MockAdapterStatus -Name Ethernet1 -Status Disconnected
        $degraded = (Get-NetLbfoTeam -Name Team1).Status
        Set-MockAdapterStatus -Name Ethernet1 -Status Up
        $recovered = (Get-NetLbfoTeam -Name Team1).Status
        "$degraded|$recovered"
    """)
    assert out == "Degraded|Up"


def test_all_members_down_marks_team_down():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        Set-MockAdapterStatus -Name Ethernet1 -Status Disconnected
        Set-MockAdapterStatus -Name Ethernet2 -Status Disconnected
        (Get-NetLbfoTeam -Name Team1).Status
    """)
    assert out == "Down"


def test_add_and_remove_team_member():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        Add-NetLbfoTeamMember -Name Ethernet3 -Team Team1 | Out-Null
        $afterAdd = (Get-NetLbfoTeam -Name Team1).Members.Count
        Remove-NetLbfoTeamMember -Name Ethernet3 -Team Team1 | Out-Null
        $afterRemove = (Get-NetLbfoTeam -Name Team1).Members.Count
        "$afterAdd|$afterRemove"
    """)
    assert out == "3|2"


def test_remove_netlbfoteam_frees_adapters():
    out = _run_ps(f"""
        Import-Module {MODULE_PATH}
        New-NetLbfoTeam -Name Team1 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        Remove-NetLbfoTeam -Name Team1 -Confirm:$false
        $teamCount = (Get-NetLbfoTeam).Count
        # 削除後は同じアダプタで新チームを作れる(TeamMemberフラグが解除されている)こと
        New-NetLbfoTeam -Name Team2 -TeamMembers Ethernet1,Ethernet2 | Out-Null
        $ok = (Get-NetLbfoTeam -Name Team2) -ne $null
        "$teamCount|$ok"
    """)
    assert out == "0|True"
