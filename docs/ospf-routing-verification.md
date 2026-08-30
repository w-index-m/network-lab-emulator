# OSPF ルーティング配信後の経路確認ガイド

このドキュメントは、OSPFで配信したルーティング情報を、netmiko/Paramikoを使用して検証する方法を説明します。

---

## 概要

### テストシナリオ

OSPFを複数デバイスで設定し、ルーティング情報が正しく配信・学習されたことを確認します：

```
Catalyst1 (10.0.1.0/24)
    ↓ OSPF接続
Cisco ISR2 (10.0.1.0/24, 10.0.2.0/24)
    ↓ OSPF接続
Si-R3 (10.0.2.0/24)
```

### 各デバイスが学習する経路

- **Catalyst**: Cisco ISRとSi-Rのネットワークを学習 (O IA)
- **Cisco ISR**: Catalystとか-RのネットワークとSi-Rへの直接接続を保有
- **Si-R**: Catalystとネイバーデバイスのネットワークを学習

---

## ツール説明

### `test_ospf_routing_verification.py`

**場所**: `tools/test_ospf_routing_verification.py`

3つのテストモード を提供：

| モード | 説明 | 対象デバイス | 依存ライブラリ |
|--------|------|-----------|-------------|
| `--emulator` | エミュレーター内でのテスト | Catalyst/Cisco/Si-R (仮想) | なし |
| `--real-catalyst` | 実機Catalyst経由でNetmiko検証 | 実機 Catalyst | netmiko |
| `--real-sir` | 実機Si-RをParamiko経由で検証 | 実機 Si-R | paramiko |

---

## 1. エミュレーター内での OSPF ルーティング検証

### 実行手順

**ターミナル1: エミュレーターサーバー起動**
```bash
cd /home/user/network-lab-emulator
python app.py

# 出力例:
# INFO:     Uvicorn running on http://0.0.0.0:8000
```

**ターミナル2: テスト実行**
```bash
python tools/test_ospf_routing_verification.py --emulator
```

### 期待される出力

```
======================================================================
🧪 OSPF ルーティング配信後の経路確認テスト
======================================================================

======================================================================
🧪 エミュレーター OSPF トポロジー検証
======================================================================

======================================================================
📍 OSPF トポロジーセットアップ
======================================================================
  ✅ デバイス登録: ospf-cat1
  ✅ デバイス登録: ospf-cisco2
  ✅ デバイス登録: ospf-sir3
  ✅ リンク作成: ospf-cat1 ↔ ospf-cisco2
  ✅ リンク作成: ospf-cisco2 ↔ ospf-sir3

======================================================================
📍 Catalyst OSPF 設定
======================================================================
  ✅ Catalyst OSPF 設定完了

======================================================================
📍 Cisco ISR OSPF 設定
======================================================================
  ✅ Cisco ISR OSPF 設定完了

======================================================================
📍 Si-R OSPF 設定
======================================================================
  ✅ Si-R OSPF 設定完了

======================================================================
📍 OSPF 隣接確認（エミュレーター内）
======================================================================

  📍 ospf-cat1:
      Neighbor ID     Pri   State           Dead Time   Address
      10.0.2.1          1   FULL/BDR        00:00:38    10.0.1.2

  📍 ospf-cisco2:
      Neighbor ID     Pri   State           Dead Time   Address
      10.0.1.1          1   FULL/DR         00:00:37    10.0.1.1
      10.0.2.2          1   FULL/DR         00:00:39    10.0.2.2

  📍 ospf-sir3:
      Neighbor ID     Pri   State           Dead Time   Address
      10.0.2.1          1   FULL/DR         00:00:38    10.0.2.1

======================================================================
📍 ルーティングテーブル確認（エミュレーター）
======================================================================

  📍 Catalyst (ospf-cat1):
      ✅ OSPF 経路数: 2
      O IA 10.0.2.0/24 [110/200] via 10.0.1.2, 00:00:15, Ethernet0/0
      O IA 192.168.102.0/24 [110/300] via 10.0.1.2, 00:00:12, Ethernet0/0

  📍 Cisco ISR (ospf-cisco2):
      ✅ OSPF 経路数: 2
      O 192.168.101.0/24 [110/100] via 10.0.1.1, 00:00:18, Ethernet0/0
      O 192.168.103.0/24 [110/100] via 10.0.2.2, 00:00:16, Ethernet0/1

  📍 Si-R (ospf-sir3):
      ✅ OSPF 経路数: 2
      O 10.0.1.0/24 [110/100] via 10.0.2.1, 00:00:20, lan0
      O 192.168.101.0/24 [110/200] via 10.0.2.1, 00:00:17, lan0

📊 エミュレーター結果: 3/3 成功
```

### テスト項目

1. **デバイス登録** — 3台のデバイス（Catalyst・Cisco・Si-R）を作成
2. **OSPF設定投入** — 各デバイスにOSPFプロセス1を設定
3. **隣接確認** — OSPF隣接が FULL 状態に達したことを確認
4. **ルーティング確認** — 各デバイスが他のネットワークのOSPF経路を学習したことを確認

---

## 2. 実機 Catalyst への Netmiko 経由のルーティング確認

### セットアップ

```bash
# netmiko インストール
pip install netmiko

# 実機 Catalyst への SSH 接続確認
ssh admin@192.168.1.100
```

### 実行

**環境変数で接続情報を指定**
```bash
export CATALYST_HOST=192.168.1.100
export CATALYST_USER=admin
export CATALYST_PASS=admin

python tools/test_ospf_routing_verification.py --real-catalyst
```

**または直接コマンドで実行（実装例）**
```python
from tools.test_ospf_routing_verification import NetmikoOSPFVerifier

verifier = NetmikoOSPFVerifier(
    host='192.168.1.100',
    username='admin',
    password='admin',
    device_type='cisco_ios'
)

verifier.verify_ospf_routing()
```

### 期待される出力

```
======================================================================
📍 OSPF ルーティング確認（Netmiko）
======================================================================

  [1] OSPF プロセス確認:
      Routing Process "ospf 1" with ID 192.168.1.100
       Start time: 00:05:23.456, Time elapsed: 00:15:43.212
       Supports only single TOS (TOS 0) routes
       Supports opaque LSA
       Supports Traffic Engineering (TE) with MPLS for TE
       Supports RSVP-TE
       Restart enabled
       Initial sync with peers: 0%
       strictest RTO interval: 34000 msecs; RTO is disabled
       Number of incomingcurrent DD exchanges running = 0
       Last redo with redo index 0
       Last redo generation number with redo index 0
       Initial number of multiple incomingcurrent DD exchanges = 0

  [2] OSPF 隣接確認:
      Neighbor ID     Pri   State           Dead Time   Address
      10.0.2.1          1   FULL/BDR        00:00:35    10.0.1.2
      10.0.3.1          1   FULL/DR         00:00:39    10.0.1.3

  [3] ルーティングテーブル（OSPF 経路）:
      O    10.0.2.0/24 [110/200] via 10.0.1.2, 00:00:14, GigabitEthernet0/0/0
      O IA 10.0.3.0/24 [110/300] via 10.0.1.2, 00:00:12, GigabitEthernet0/0/0
      O    192.168.102.0/24 [110/150] via 10.0.1.2, 00:00:10, GigabitEthernet0/0/0

      ✅ ルーティングテーブル取得成功

  [4] 全ルーティングテーブル:
      ✅ 合計経路数: 推定 12 経路
      Codes: C - connected, S - static, R - RIP, M - mobile, B - BGP
             D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
             N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
             E1 - OSPF external type 1, E2 - OSPF external type 2
             i - IS-IS, su - IS-IS summary, L1 - IS-IS level-1, L2 - IS-IS level-2
             ia - IS-IS inter area, * - candidate default, U - per-user static route
             o - ODR, P - periodic downloaded static route, H - nssa-external
             l - LISP, a - application route
             + - replicated route, % - next hop override, p - overrides of connected

      Gateway of last resort is 0.0.0.0 to network 0.0.0.0

      C    10.0.1.0/24 is directly connected, GigabitEthernet1/0/1
      O    10.0.2.0/24 [110/200] via 10.0.1.2, 00:00:14, GigabitEthernet1/0/1
      O IA 10.0.3.0/24 [110/300] via 10.0.1.2, 00:00:12, GigabitEthernet1/0/1
      S*   0.0.0.0/0 [1/0] via 192.168.1.1
      C    192.168.1.0/24 is directly connected, GigabitEthernet0/0
      O    192.168.102.0/24 [110/150] via 10.0.1.2, 00:00:10, GigabitEthernet1/0/1
      C    192.168.101.0/24 is directly connected, Vlan100
```

### 検証内容

1. **OSPF プロセス確認** — OSPF が稼働しているか確認
2. **OSPF 隣接確認** — ネイバーが FULL 状態か確認
3. **OSPF 経路確認** — `show ip route ospf` で学習した経路をフィルタ
4. **全ルーティングテーブル** — 全経路をスナップショット

---

## 3. 実機 Si-R への Paramiko 経由のルーティング確認

### Si-R 対応状況

| 項目 | 対応 | 説明 |
|------|------|------|
| **SSH接続** | ✅ 対応 | Paramiko で接続可能 |
| **CLI 実行** | ✅ 対応 | 標準的なSSH/CLIで実行可能 |
| **netmiko** | ❌ 未対応 | Si-R の device driver がない |
| **Paramiko** | ✅ 対応 | 汎用SSHクライアント として利用可能 |

### セットアップ

```bash
# paramiko インストール
pip install paramiko

# Si-R へのSSH接続確認
ssh admin@192.168.1.50
```

### 実行

**環境変数で接続情報を指定**
```bash
export SIR_HOST=192.168.1.50
export SIR_USER=admin
export SIR_PASS=admin

python tools/test_ospf_routing_verification.py --real-sir
```

### 期待される出力

```
======================================================================
📍 Si-R ルーティング確認（Paramiko）
======================================================================

  [📍] ルーティングテーブル:
      Routing table
      Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP
             i - ISIS

      C  10.0.2.0/24    via                    lan0        Metric   0
      O  10.0.1.0/24    via 10.0.2.1           lan0        Metric   100
      O  192.168.101.0/24 via 10.0.2.1         lan0        Metric   200
      O  192.168.102.0/24 via 10.0.2.1         lan0        Metric   150

      ✅ ルーティングテーブル 取得成功

  [📍] OSPF プロセス:
      Router ospf 1
        Router ID: 10.0.2.2
        Area 0:
          Network: 10.0.2.0/24
          Network: 192.168.103.0/24

      ✅ OSPF プロセス 取得成功

  [📍] OSPF 隣接:
      Process 1, Router ID 10.0.2.2
        Neighbor: 10.0.2.1
          State: Full
          Interface: lan0
          Last Hello: 8 seconds ago

      ✅ OSPF 隣接 取得成功
```

### Si-R での確認コマンド

Si-R の CLI コマンド体系は Cisco と異なります：

```
# ルーティングテーブル表示
show ip route

# OSPF プロセス確認
show ip ospf

# OSPF 隣接確認
show ip ospf neighbor

# 詳細設定確認
show running-config

# ルーターID確認
show ip ospf brief
```

---

## 4. Si-R での Paramiko 利用の詳細

### Paramiko による Si-R 接続のメリット

1. **netmiko 依存なし** — Si-R 専用ドライバがなくても接続可能
2. **汎用SSHクライアント** — プロトコルレベルでの接続
3. **カスタムコマンド対応** — Si-R 独自のコマンドに対応可能

### Paramiko でのコマンド実行例

```python
import paramiko

# SSH 接続
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.1.50', username='admin', password='admin')

# CLI コマンド実行
stdin, stdout, stderr = ssh.exec_command('show ip route')
output = stdout.read().decode()
print(output)

# 設定コマンド投入（注意: 実際のデバイスに影響）
commands = [
    'configure',
    'router ospf 1',
    'network 192.168.200.0 0.0.0.255 area 0',
    'exit',
    'save'
]

for cmd in commands:
    ssh.exec_command(cmd)
    time.sleep(0.5)

ssh.close()
```

### 制限事項

1. **バナー処理** — 接続時のバナーメッセージの処理が必要な場合あり
2. **プロンプト検出** — クライアント側でプロンプトを認識する必要がある
3. **コマンド応答** — CLI 上のプロンプト入力に対する応答が必要な場合あり

対応例：
```python
# バナーをスキップ
ssh.get_transport().set_security_options(
    paramiko.py3compat.decodebytes(b'...')  # ホスト鍵
)

# タイムアウト設定
ssh.get_transport().set_keepalive(30)
```

---

## トラブルシューティング

### Netmiko 接続失敗（Catalyst）

**症状**: `NetmikoTimeoutException`

**対応**:
```bash
# 1. SSH 直接接続確認
ssh admin@192.168.1.100

# 2. タイムアウト時間延長
python tools/test_ospf_routing_verification.py --real-catalyst
# 内部で timeout=30 を使用（必要に応じて修正）

# 3. ログ出力で詳細確認
python -c "
from tools.test_ospf_routing_verification import NetmikoOSPFVerifier
v = NetmikoOSPFVerifier('192.168.1.100', 'admin', 'admin')
v.verify_ospf_routing()
"
```

### Paramiko 接続失敗（Si-R）

**症状**: `SSHException` または `AuthenticationException`

**対応**:
```bash
# 1. SSH キー認証確認
ssh -i ~/.ssh/id_rsa admin@192.168.1.50

# 2. パスワード認証確認
ssh -o PreferredAuthentications=password admin@192.168.1.50

# 3. ホスト鍵の受け入れ確認
ssh-keyscan -H 192.168.1.50 >> ~/.ssh/known_hosts
```

### ルーティング学習がない

**症状**: `show ip route ospf` で経路が表示されない

**対応**:
```bash
# 1. OSPF プロセス確認
show ip ospf

# 2. OSPF 隣接確認
show ip ospf neighbor

# 3. ネットワーク設定確認
show running-config | include network

# 4. インターフェース状態確認
show interfaces | include line protocol is up

# 5. デバッグログ確認（テスト環境のみ）
debug ip ospf events
```

---

## まとめ

| 方法 | 実機必要 | ツール | 対応デバイス | 用途 |
|------|--------|-------|----------|------|
| **エミュレーター** | ❌ | HTTP API | 全対応 | 開発・学習 |
| **Netmiko** | ✅ | SSH | Catalyst / Cisco / Nexus / ASA | 自動化・検証 |
| **Paramiko** | ✅ | SSH | Si-R / SR-S / その他 | カスタム統合 |

---

## 参考資料

- **Netmiko**: https://github.com/ktbyers/netmiko
- **Paramiko**: https://github.com/paramiko/paramiko
- **Cisco IOS コマンド**: https://www.cisco.com/c/en/us/support/index.html
- **富士通 Si-R ドキュメント**: 機器付属の管理者マニュアル参照
