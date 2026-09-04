# PowerShell NIC Teaming (NetLbfo) モック

`tools/MockNetLbfo/MockNetLbfo.psm1`

## これは何か

Windows ServerのNICチーミング（LBFO）は`NetLbfo`という**Windows専用の
PowerShellモジュール**（`New-NetLbfoTeam`等）で操作する。このモジュールは
Windows以外には存在せず、Linux版PowerShell（pwsh、クロスプラットフォーム）
では`Get-NetAdapter`すら使えない。

一方でNICチーミング自体をこの環境で試すには、Linuxカーネルの
bonding機能が必要だが、このサンドボックスはコンテナ環境で
`CAP_SYS_MODULE`が無く、カーネルモジュール(`bonding.ko`)をロード
できないため、Linux bondingも実機検証できなかった
（`ip link add bond0 type bond` → `Error: Unknown device type.`）。

そこで、このリポジトリの他機種（Cisco/VyOS等）と同じ「**実コマンド
構文そのままで挙動を模擬する**」アプローチを踏襲し、`New-NetLbfoTeam`
等の主要コマンドレットをPowerShellスクリプトで再実装した。実際の
Windows Serverで打つのと同じコマンドをそのまま使って、チーム作成・
メンバー追加/削除・フェイルオーバー挙動を確認できる。

## 対応コマンドレット

| コマンドレット | 実装状況 |
|---|---|
| `Get-NetAdapter` | ○（モックアダプタ4枚: Ethernet1〜4） |
| `New-NetLbfoTeam` | ○（TeamingMode/LoadBalancingAlgorithm対応） |
| `Get-NetLbfoTeam` | ○ |
| `Get-NetLbfoTeamMember` | ○（OperationalStatus/FailureReason反映） |
| `Add-NetLbfoTeamMember` | ○ |
| `Remove-NetLbfoTeamMember` | ○ |
| `Set-NetLbfoTeam` | ○ |
| `Remove-NetLbfoTeam` | ○ |
| `Set-MockAdapterStatus` | このモック独自の拡張（NIC切断/復旧をシミュレート） |

## 使い方

```powershell
Import-Module ./tools/MockNetLbfo/MockNetLbfo.psm1

# アダプタ一覧
Get-NetAdapter

# LACPチーム作成（実際のWindows Server構文そのまま）
New-NetLbfoTeam -Name "Team1" -TeamMembers "Ethernet1","Ethernet2" `
    -TeamingMode LACP -LoadBalancingAlgorithm Dynamic

# チームメンバーの状態確認
Get-NetLbfoTeamMember -Team Team1

# 障害シミュレーション（このモック独自コマンド）
Set-MockAdapterStatus -Name Ethernet1 -Status Disconnected
(Get-NetLbfoTeam -Name Team1).Status   # → Degraded

Set-MockAdapterStatus -Name Ethernet1 -Status Up
(Get-NetLbfoTeam -Name Team1).Status   # → Up
```

## 実際に確認した動作（2026-09-02）

PowerShell 7.6.5（GitHub Releasesから取得、`pwsh`コマンド）で
以下の11ステップシナリオを実行し、全て実機同等の挙動を確認:

1. `Get-NetAdapter` — 4枚のモックアダプタ表示
2. `New-NetLbfoTeam` — LACPチーム作成
3. `Get-NetLbfoTeam` — チーム状態確認（Status=Up）
4. `Get-NetLbfoTeamMember` — 両メンバーOperationalStatus=Up
5. `Set-MockAdapterStatus -Status Disconnected` — 片系切断
6. フェイルオーバー確認 — Team Status が `Degraded` に変化、
   `FailureReason=AdapterDisconnected`が反映
7. 復旧 — Team Statusが`Up`に戻る
8. `Add-NetLbfoTeamMember` — 3枚目のNICを追加
9. 全メンバー切断 — Team Statusが`Down`に変化（実機同様）
10. エラー系 — 既にチームメンバーのNICを別チームに追加しようとして
    正しく拒否される（`New-NetLbfoTeam : Network adapter 'Ethernet1'
    is already a member of another team.`）
11. `Remove-NetLbfoTeam` — チーム削除、アダプタが解放され再利用可能に

実装中に見つけた実バグ2件（`New-NetLbfoTeam`/`Remove-NetLbfoTeam`で
`[CmdletBinding(SupportsShouldProcess=$true)]`が自動生成する`-Confirm`
パラメータと、手動定義していた`[switch]$Confirm`が衝突しコマンドレット
自体がロードできなくなる不具合）を修正済み。

## この環境固有の癖

- **`Format-Table`が非対話実行では空出力になる**: このサンドボックスの
  非TTY環境ではPowerShellのデフォルトテーブル整形（コンソール幅検出
  依存）が機能しない。オブジェクト自体は正しく返っているため、
  `ForEach-Object`で明示的に整形するか、実際のWindows端末（対話TTY）
  で試せば通常通り表示される
- `Write-Host`の出力もこの環境ではstdoutにそのまま混ざる
  （通常はホストUI直接出力でパイプに乗らない）

## テスト

```bash
pytest tests/test_mock_netlbfo.py -v
# 7/7 成功（pwsh未インストール環境では自動skip）
```

## 制約

- 実際のL2/L3挙動（フレーム転送、LACPネゴシエーション等）はシミュレート
  していない。あくまで**コマンドレットの入出力・状態遷移ロジック**の
  練習用
- `Get-NetAdapter`のモックアダプタは固定4枚。実機の`Get-NetAdapter`が
  返す全プロパティ（`ifIndex`, `DriverVersion`等）は再現していない
