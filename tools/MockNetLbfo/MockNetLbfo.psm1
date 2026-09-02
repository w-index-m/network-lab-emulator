# MockNetLbfo.psm1
#
# Windows Server の NIC Teaming (LBFO) コマンドレットを模したモジュール。
# 実際の NetLbfo モジュールは Windows専用で Linux 版 PowerShell には
# 存在しないため、このラボの他機種（Cisco/VyOS等）と同じ「実コマンド
# 構文そのままで挙動を模擬する」アプローチで、コマンドレットの使い方
# そのものを練習できるようにしたもの。
#
# 対応コマンドレット:
#   Get-NetAdapter, New-NetLbfoTeam, Get-NetLbfoTeam,
#   Get-NetLbfoTeamMember, Add-NetLbfoTeamMember,
#   Remove-NetLbfoTeamMember, Set-NetLbfoTeam, Remove-NetLbfoTeam
#
# 実際のWindows Serverで動く構文をそのまま使えるが、これはシミュレーター
# であり実ネットワークには一切影響しない。

# ── 内部状態（モジュールスコープ） ──────────────────────
$script:Adapters = @(
    [PSCustomObject]@{ Name = 'Ethernet1'; InterfaceDescription = 'Intel(R) I350 Gigabit Network Connection'; Status = 'Up'; MacAddress = '00-15-5D-01-01-01'; LinkSpeed = '1 Gbps'; TeamMember = $false }
    [PSCustomObject]@{ Name = 'Ethernet2'; InterfaceDescription = 'Intel(R) I350 Gigabit Network Connection #2'; Status = 'Up'; MacAddress = '00-15-5D-01-01-02'; LinkSpeed = '1 Gbps'; TeamMember = $false }
    [PSCustomObject]@{ Name = 'Ethernet3'; InterfaceDescription = 'Intel(R) I350 Gigabit Network Connection #3'; Status = 'Up'; MacAddress = '00-15-5D-01-01-03'; LinkSpeed = '1 Gbps'; TeamMember = $false }
    [PSCustomObject]@{ Name = 'Ethernet4'; InterfaceDescription = 'Intel(R) I350 Gigabit Network Connection #4'; Status = 'Disconnected'; MacAddress = '00-15-5D-01-01-04'; LinkSpeed = '0 bps'; TeamMember = $false }
)

$script:Teams = @{}

function Get-NetAdapter {
    [CmdletBinding()]
    param(
        [Parameter(Position=0)][string]$Name
    )
    if ($Name) {
        $a = $script:Adapters | Where-Object { $_.Name -eq $Name }
        if (-not $a) { Write-Error "Get-NetAdapter : No matching MSFT_NetAdapter objects found by CIM query for instances of the root/StandardCimv2 class." }
        return $a
    }
    return $script:Adapters
}

function New-NetLbfoTeam {
    [CmdletBinding(SupportsShouldProcess=$true)]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name,
        [Parameter(Mandatory=$true)][string[]]$TeamMembers,
        [ValidateSet('SwitchIndependent','LACP','Static')][string]$TeamingMode = 'SwitchIndependent',
        [ValidateSet('Dynamic','HyperVPort','IPAddresses','MacAddresses','TransportPorts')][string]$LoadBalancingAlgorithm = 'Dynamic'
    )
    if ($script:Teams.ContainsKey($Name)) {
        Write-Error "New-NetLbfoTeam : A team with the name '$Name' already exists."
        return
    }
    foreach ($m in $TeamMembers) {
        $adapter = $script:Adapters | Where-Object { $_.Name -eq $m }
        if (-not $adapter) {
            Write-Error "New-NetLbfoTeam : Network adapter '$m' was not found."
            return
        }
        if ($adapter.TeamMember) {
            Write-Error "New-NetLbfoTeam : Network adapter '$m' is already a member of another team."
            return
        }
    }
    foreach ($m in $TeamMembers) {
        ($script:Adapters | Where-Object { $_.Name -eq $m }).TeamMember = $true
    }
    $primaryUp = ($script:Adapters | Where-Object { $_.Name -in $TeamMembers -and $_.Status -eq 'Up' }).Count -gt 0
    $script:Teams[$Name] = [PSCustomObject]@{
        Name                    = $Name
        Members                 = $TeamMembers
        TeamingMode             = $TeamingMode
        LoadBalancingAlgorithm  = $LoadBalancingAlgorithm
        Status                  = if ($primaryUp) { 'Up' } else { 'Degraded' }
    }
    Write-Host "New team '$Name' created with members: $($TeamMembers -join ', ') (Mode=$TeamingMode, LB=$LoadBalancingAlgorithm)"
    return $script:Teams[$Name]
}

function Get-NetLbfoTeam {
    [CmdletBinding()]
    param(
        [Parameter(Position=0)][string]$Name
    )
    if ($Name) {
        if (-not $script:Teams.ContainsKey($Name)) {
            Write-Error "Get-NetLbfoTeam : No MSFT_NetLbfoTeam objects found with property 'Name' equal to '$Name'."
            return
        }
        return $script:Teams[$Name]
    }
    return $script:Teams.Values
}

function Get-NetLbfoTeamMember {
    [CmdletBinding()]
    param(
        [Parameter(Position=0)][string]$Team
    )
    $members = @()
    foreach ($t in $script:Teams.Values) {
        if ($Team -and $t.Name -ne $Team) { continue }
        foreach ($m in $t.Members) {
            $adapter = $script:Adapters | Where-Object { $_.Name -eq $m }
            $members += [PSCustomObject]@{
                Name              = $m
                Team              = $t.Name
                AdministrativeMode = 'Active'
                OperationalStatus = $adapter.Status
                TransmitLinkSpeed = $adapter.LinkSpeed
                ReceiveLinkSpeed  = $adapter.LinkSpeed
                FailureReason     = if ($adapter.Status -eq 'Up') { 'NoFailure' } else { 'AdapterDisconnected' }
            }
        }
    }
    return $members
}

function Add-NetLbfoTeamMember {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Team
    )
    if (-not $script:Teams.ContainsKey($Team)) {
        Write-Error "Add-NetLbfoTeamMember : No MSFT_NetLbfoTeam objects found with property 'Name' equal to '$Team'."
        return
    }
    $adapter = $script:Adapters | Where-Object { $_.Name -eq $Name }
    if (-not $adapter) {
        Write-Error "Add-NetLbfoTeamMember : Network adapter '$Name' was not found."
        return
    }
    if ($adapter.TeamMember) {
        Write-Error "Add-NetLbfoTeamMember : Network adapter '$Name' is already a member of a team."
        return
    }
    $adapter.TeamMember = $true
    $script:Teams[$Team].Members += $Name
    Write-Host "Added '$Name' to team '$Team'."
}

function Remove-NetLbfoTeamMember {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Team
    )
    if (-not $script:Teams.ContainsKey($Team)) {
        Write-Error "Remove-NetLbfoTeamMember : No MSFT_NetLbfoTeam objects found with property 'Name' equal to '$Team'."
        return
    }
    $t = $script:Teams[$Team]
    if ($t.Members.Count -le 1) {
        Write-Error "Remove-NetLbfoTeamMember : Cannot remove the last member of a team. Use Remove-NetLbfoTeam instead."
        return
    }
    $t.Members = $t.Members | Where-Object { $_ -ne $Name }
    ($script:Adapters | Where-Object { $_.Name -eq $Name }).TeamMember = $false
    Write-Host "Removed '$Name' from team '$Team'."
}

function Set-NetLbfoTeam {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name,
        [ValidateSet('SwitchIndependent','LACP','Static')][string]$TeamingMode,
        [ValidateSet('Dynamic','HyperVPort','IPAddresses','MacAddresses','TransportPorts')][string]$LoadBalancingAlgorithm
    )
    if (-not $script:Teams.ContainsKey($Name)) {
        Write-Error "Set-NetLbfoTeam : No MSFT_NetLbfoTeam objects found with property 'Name' equal to '$Name'."
        return
    }
    if ($TeamingMode) { $script:Teams[$Name].TeamingMode = $TeamingMode }
    if ($LoadBalancingAlgorithm) { $script:Teams[$Name].LoadBalancingAlgorithm = $LoadBalancingAlgorithm }
    Write-Host "Team '$Name' updated: Mode=$($script:Teams[$Name].TeamingMode), LB=$($script:Teams[$Name].LoadBalancingAlgorithm)"
    return $script:Teams[$Name]
}

function Remove-NetLbfoTeam {
    [CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='High')]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name
    )
    if (-not $script:Teams.ContainsKey($Name)) {
        Write-Error "Remove-NetLbfoTeam : No MSFT_NetLbfoTeam objects found with property 'Name' equal to '$Name'."
        return
    }
    foreach ($m in $script:Teams[$Name].Members) {
        ($script:Adapters | Where-Object { $_.Name -eq $m }).TeamMember = $false
    }
    $script:Teams.Remove($Name)
    Write-Host "Team '$Name' removed."
}

# ── 障害シミュレーション（このモックだけの拡張コマンド） ──────
function Set-MockAdapterStatus {
    <#
    .SYNOPSIS
        テスト用: 指定したアダプタのリンク状態を強制的に変更する。
        実際のNIC切断/復旧をシミュレートし、チームのフェイルオーバー
        挙動（Get-NetLbfoTeamMemberのOperationalStatus/FailureReason
        への反映）を確認するために使う。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true, Position=0)][string]$Name,
        [Parameter(Mandatory=$true)][ValidateSet('Up','Disconnected')][string]$Status
    )
    $adapter = $script:Adapters | Where-Object { $_.Name -eq $Name }
    if (-not $adapter) {
        Write-Error "Set-MockAdapterStatus : Network adapter '$Name' was not found."
        return
    }
    $adapter.Status = $Status
    $adapter.LinkSpeed = if ($Status -eq 'Up') { '1 Gbps' } else { '0 bps' }
    # このアダプタが所属するチームのStatusも再評価
    foreach ($t in $script:Teams.Values) {
        if ($t.Members -contains $Name) {
            $anyUp = ($script:Adapters | Where-Object { $_.Name -in $t.Members -and $_.Status -eq 'Up' }).Count -gt 0
            $allUp = ($script:Adapters | Where-Object { $_.Name -in $t.Members -and $_.Status -eq 'Up' }).Count -eq $t.Members.Count
            $t.Status = if ($allUp) { 'Up' } elseif ($anyUp) { 'Degraded' } else { 'Down' }
        }
    }
    Write-Host "Adapter '$Name' status set to '$Status'."
}

Export-ModuleMember -Function `
    Get-NetAdapter, New-NetLbfoTeam, Get-NetLbfoTeam, Get-NetLbfoTeamMember, `
    Add-NetLbfoTeamMember, Remove-NetLbfoTeamMember, Set-NetLbfoTeam, `
    Remove-NetLbfoTeam, Set-MockAdapterStatus
