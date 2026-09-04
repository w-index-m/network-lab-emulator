# EtherChannel + STP コスト + 経路ジェネレータ 統合ガイド

このドキュメントは、EtherChannelのポートコストがSTPに反映されること、および経路ジェネレータで配信した経路をParamikoで検証する方法について説明します。

---

## 概要

### 実装済み機能

| 機能 | 実装状況 | 説明 |
|------|--------|------|
| **EtherChannel** | ✅ 実装済み | LACP/Static バンドル対応 |
| **STP コスト** | ✅ 実装済み | ポート毎・port-channel毎のコスト計算 |
| **STP コスト反映** | ✅ 実装済み | EtherChannel（port-channel）のコストがSTPに反映 |
| **Static Route** | ✅ 実装済み | ip route コマンドで経路配信 |
| **redistribute** | ✅ 実装済み | Static → OSPF/RIP/BGP への再配信 |
| **経路ジェネレータ** | ✅ 実装可能 | Static + redistribute で実現 |

---

## 1. EtherChannel + STP コスト反映

### アーキテクチャ

```
┌─ Catalyst ─┐
│  Gi1/0/1  │
│  Gi1/0/2  │ ─→ channel-group 1 ─→ port-channel 1
│           │                        (STP Cost: 8)
└───────────┘

STP では port-channel 1 が単一のポートとして扱われ、
そのコストが BPDU計算に使用される
```

### Catalyst での設定例

```cisco
! ステップ1: EtherChannel構成
interface range GigabitEthernet1/0/1-2
 channel-group 1 mode active
 exit

! ステップ2: port-channel のコスト設定
interface port-channel 1
 switchport mode trunk
 spanning-tree cost 8          ! ← STP ポートコスト設定
 spanning-tree port priority 128
 exit
```

### 検証方法

```bash
# port-channel の EtherChannel 状態確認
show etherchannel summary
show port-channel summary

# STP ではport-channelをポートとして認識
show spanning-tree interface port-channel 1
show spanning-tree vlan 1

# 出力例:
# Interface        Role Sts Cost      Prio Nbr
# ———————————————— —— ——— ———————— ———— ———
# Port-channel1    Alr FWD 8        128.1
```

### コスト計算例

複数ポートのEtherChannelでも、**STP は port-channel を単一ポートとして扱う**：

```
構成: 2x GigabitEthernet + LACP → port-channel 1

個別ポートコスト（参考値）:
  - Gi1/0/1: 4 (デフォルト 1Gbps)
  - Gi1/0/2: 4 (デフォルト 1Gbps)

Port-channel のSTPコスト（手動設定）:
  - port-channel 1: 8 (管理者設定値）

STP 計算では → **port-channel 1 のコスト (8) を使用**
```

### SR-S での設定例

```cisco
! SR-S (富士通) での EtherChannel設定
interface range GigabitEthernet1/0/1-2
 channel-group 1 mode passive    ! 受動側
 exit

interface port-channel 1
 switchport mode trunk
 spanning-tree cost 12           ! SR-Sではコスト12
 exit
```

### テスト実行

```bash
python tools/test_etherchannel_stp_cost.py --emulator
```

**期待される出力**:

```
======================================================================
🧪 EtherChannel + STP コスト検証（エミュレーター）
======================================================================

======================================================================
📍 EtherChannel 状態確認
======================================================================

  📍 cat-test:
      Flags:  D - down        P - bundled in port-channel
      Number of channel-groups in use: 1
      Number of aggregators:           1
      
      Group  Port-channel  Protocol    Ports
      ——————————————————————————————————————————
      1      Po1(SU)       LACP      Gi1/0/1(P) Gi1/0/2(P)
      ✅ port-channel 情報取得

======================================================================
📍 STP コスト確認
======================================================================

  📍 cat-test:
      Interface        Role Sts Cost      Prio Nbr
      ———————————————— —— ——— ———————— ———— ———
      Port-channel1    Alr FWD 8        128.1

      [port-channel 1 詳細]
      Port-channel1 (port 63)
       Port path cost (auto):     4
       Port path cost (manual):   8    ← 設定値が反映
```

### 重要なポイント

1. **複数ポートの集約**: バンドルされたポートは物理的には複数だが、STP では port-channel として **単一ポート** として扱われる

2. **コストの優先度**: 手動設定コスト > 自動計算コスト
   ```
   spanning-tree cost 8  ← これが最優先
   ```

3. **ルート選択への影響**: port-channel のコストは全体のパスコスト計算に含まれる
   ```
   Root Path Cost = 初期値 + Port1 Cost + Port2 Cost + ...
   ```

---

## 2. 経路ジェネレータ + redistribute

### アーキテクチャ

```
Si-R での経路生成・配信フロー：

┌────────────────────┐
│ Static Route 設定   │  ← 経路ジェネレータ相当
│ ip route 192.168.. │
└────────────────────┘
         ↓
┌────────────────────┐
│ routing table      │  ← ローカルルーティング
│ (S: static)        │
└────────────────────┘
         ↓
┌────────────────────┐
│ redistribute       │  ← 再配信設定
│ router ospf 1      │
│  redistribute ..   │
└────────────────────┘
         ↓
┌────────────────────┐
│ OSPF 広告          │  ← ネイバーへ配信
│ (O E2: external)   │
└────────────────────┘
```

### Si-R での設定例

```cisco
! ステップ1: Static Route 投入（経路ジェネレータ）
configure
ip route 192.168.200.0/24 0.0.0.0
ip route 192.168.201.0/24 0.0.0.0
ip route 10.100.0.0/16 0.0.0.0

! ステップ2: OSPF で redistribute
router ospf 1
 network 10.0.0.0 0.255.255.255 area 0
 redistribute static      ← Static Route を OSPF で配信
 exit

save
```

### Catalyst での受信確認

```bash
# redistribute された経路は "O E2" (外部ルート) として学習
show ip route ospf

# 出力例:
# O E2  192.168.200.0/24 [110/20] via 10.0.1.2, 00:00:45, Gi1/0/1
# O E2  192.168.201.0/24 [110/20] via 10.0.1.2, 00:00:43, Gi1/0/1
# O E2  10.100.0.0/16 [110/20] via 10.0.1.2, 00:00:40, Gi1/0/1
```

### テスト実行

```bash
python tools/test_etherchannel_stp_cost.py --emulator
```

**テスト内容**:
1. Si-R で 3つの Static Route を投入
2. OSPF で redistribute
3. エミュレーター内で経路学習確認

---

## 3. Paramiko経由でのSi-R経路確認

### セットアップ

```bash
pip install paramiko
```

### Si-R 実機での設定例

```cisco
configure
! 経路ジェネレータ設定
ip route 192.168.200.0/24 0.0.0.0     ! ブラックホール
ip route 192.168.201.0/24 0.0.0.0

! OSPF設定
router ospf 1
 network 10.0.1.0 0.0.0.3 area 0
 redistribute static
 exit

save
```

### Paramiko で経路確認

```bash
export SIR_HOST=192.168.1.50
export SIR_USER=admin
export SIR_PASS=admin

python tools/test_etherchannel_stp_cost.py --sir-routes
```

### 期待される出力

```
======================================================================
📍 Si-R 経路確認（ジェネレータ経由）- Paramiko
======================================================================

  [1] 全ルーティングテーブル:
      Routing table
      Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP
             i - ISIS
      
      C  10.0.1.0/24      via                    lan0
      S  192.168.200.0/24  via 0.0.0.0           static
      S  192.168.201.0/24  via 0.0.0.0           static
      S  10.100.0.0/16     via 0.0.0.0           static
      O  172.16.0.0/16     via 10.0.1.1          lan0 (OSPF from peer)

      ✅ ルーティングテーブル取得成功

  [2] Static Route一覧:
      S  192.168.200.0/24  via 0.0.0.0           static
      S  192.168.201.0/24  via 0.0.0.0           static
      S  10.100.0.0/16     via 0.0.0.0           static
      ✅ Static Route取得成功

  [3] OSPF プロセス確認:
      Router ospf 1
       Router ID: 10.0.1.2
       Area 0:
        Network: 10.0.1.0/24
      ✅ OSPF プロセス取得成功

  [4] redistribute 設定確認:
        redistribute static
      ✅ redistribute 設定取得成功
```

### Paramiko でのコマンド実行コード例

```python
import paramiko

# SSH接続
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.1.50', username='admin', password='admin', timeout=10)

# ルーティングテーブル取得
stdin, stdout, stderr = ssh.exec_command('show ip route')
routes = stdout.read().decode()
print(routes)

# Static Route フィルタ
for line in routes.split('\n'):
    if line.startswith('S '):
        print(f"配信経路: {line}")

# redistribute 設定確認
stdin, stdout, stderr = ssh.exec_command('show running-config | include redistribute')
config = stdout.read().decode()
if 'redistribute static' in config:
    print("✅ redistribute が設定されている")

ssh.close()
```

---

## 4. 統合テストシナリオ

### シナリオ: マルチベンダー EtherChannel + 経路配信

```
SR-S ←EtherChannel→ Catalyst
       (port-channel)
           ↓
    STP コスト = 8
           ↓
        ルート選択に影響
        
        
Si-R
  ├─ Static 経路 (192.168.200.0/24 等)
  ├─ redistribute OSPF へ
  └─ Catalyst/SR-S で学習
```

### 検証ステップ

**1. EtherChannel バンドル確認**
```bash
show etherchannel summary
show port-channel summary
```

**2. STP コスト反映確認**
```bash
show spanning-tree interface port-channel 1
show spanning-tree vlan 1
# port-channel が正しいコストで表示されるか
```

**3. 経路ジェネレータ設定**
```bash
# Si-R で
configure
ip route 192.168.200.0/24 0.0.0.0
router ospf 1
 redistribute static
```

**4. 経路学習確認**
```bash
# Catalyst で
show ip route ospf
# O E2 で表示される経路を確認
```

**5. Paramiko 検証**
```bash
python tools/test_etherchannel_stp_cost.py --sir-routes
# Si-R の static/OSPF 経路が取得できることを確認
```

---

## トラブルシューティング

### EtherChannel が Down 状態

**症状**: `show etherchannel summary` で D (down) と表示

**対応**:
```bash
# ポートが Enabled か確認
show interfaces GigabitEthernet1/0/1 | include administratively

# ポート設定確認
show running-config interface GigabitEthernet1/0/1

# 再設定
conf t
interface GigabitEthernet1/0/1
 no shutdown
 exit
```

### STP コスト が反映されない

**症状**: 手動設定した `spanning-tree cost` が無視される

**対応**:
```bash
# 1. port-channel インターフェースか確認
show running-config interface port-channel 1 | include spanning-tree cost

# 2. コスト再設定
interface port-channel 1
 spanning-tree cost 8
 exit

# 3. STP トポロジー再計算
clear spanning-tree detected-protocols    # 検出プロトコルリセット
```

### 経路が配信されない

**症状**: Static Route を設定してもOSPFで配信されない

**対応**:
```bash
# 1. Static Route 確認
show ip route static

# 2. redistribute 確認
show running-config | include redistribute

# 3. OSPF プロセス確認
show ip ospf

# 4. redistribute 再設定
router ospf 1
 no redistribute static
 redistribute static
 exit
```

### Paramiko SSH接続失敗

**症状**: `SSHException` が発生

**対応**:
```bash
# 1. SSH 直接接続テスト
ssh admin@192.168.1.50

# 2. 鍵交換方式確認
ssh -o KexAlgorithms=diffie-hellman-group1-sha1 admin@192.168.1.50

# 3. Paramiko タイムアウト設定
# コード内で timeout を増やす
ssh.connect(..., timeout=30)
```

---

## 参考資料

### EtherChannel
- Cisco EtherChannel: https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst3650/software/release/16-11/b_c3650_consolidated_cg_16_11/b_c3650_consolidated_cg_16_11_chapter_028.html

### STP
- Cisco Spanning Tree: https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/24062-146.html

### Route Redistribution
- Cisco Redistribution: https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-3/routing/b_routing_17_3_cg/b_routing_17_3_cg_chapter_010.html

### Paramiko
- Paramiko Documentation: https://docs.paramiko.org/

---

## まとめ

| 機能 | 対応 | テストツール | 用途 |
|------|------|-----------|------|
| **EtherChannel** | ✅ | etherchannel_stp_cost.py | マルチベンダー冗長構成 |
| **STP コスト** | ✅ | etherchannel_stp_cost.py | ルート選択制御 |
| **経路ジェネレータ** | ✅ | etherchannel_stp_cost.py | テスト経路配信 |
| **Paramiko検証** | ✅ | etherchannel_stp_cost.py | Si-R 経路確認 |

