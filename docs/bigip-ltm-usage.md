# F5 BIG-IP LTM (Local Traffic Management) 完全ガイド

このドキュメントは、Network Lab Emulator で **F5 BIG-IP** ロードバランサーを使用・検証する方法を説明します。

LTM は VIP（Virtual IP）への着信トラフィックを複数のバックエンド**メンバー**（実サーバ）に分散させます。

---

## 📋 概要

### BIG-IP ロードバランシングの流れ

```
┌────────────┐
│   Client   │
│ 203.0.113.1:12345
└──────┬─────┘
       │
       ▼ (192.0.2.10:80 に接続)
┌──────────────────────────┐
│  F5 BIG-IP LTM           │
│  Virtual Server (VIP)    │
│  192.0.2.10:80           │
│  ├─ Pool: web_pool       │
│  │  ├─ Member: 10.0.0.1:80
│  │  ├─ Member: 10.0.0.2:80
│  │  └─ Member: 10.0.0.3:80
│  │                  ▼
│  └─ Load-balance (RR)
└──────────────────────────┘
       │   │   │
       ▼   ▼   ▼
 ┌──────┐┌──────┐┌──────┐
 │ Web1 ││ Web2 ││ Web3 │
 └──────┘└──────┘└──────┘
```

### 主要コンポーネント

| コンポーネント | 説明 | 例 |
|---|---|---|
| **Virtual Server (VIP)** | クライアントが接続する仮想IP | 192.0.2.10:80 |
| **Pool** | バックエンドサーバの集合 | web_pool |
| **Pool Member** | 実サーバ (IP:port) | 10.0.0.1:80 |
| **Monitor** | メンバーのヘルスチェック | http / tcp / icmp |
| **Load-Balancing Mode** | 分散方式 | round-robin / least-connections |
| **Node** | サーバのIP定義 | 10.0.0.1 |

---

## 🔧 基本操作

### 1. デバイス登録

エミュレーター内で BIG-IP を追加：

```bash
# HTTPポスト で デバイス追加
curl -X POST http://localhost:8000/api/device \
  -H "Content-Type: application/json" \
  -d '{
    "id": "bigip-1",
    "type": "bigip",
    "hostname": "F5-LTM"
  }'
```

または UI から 「+ F5 BIG-IP」を選択

---

### 2. Pool 作成（メンバー・モニター・分散方式）

```cisco
tmsh create ltm pool web_pool {
    members add { 10.0.0.1:80 10.0.0.2:80 10.0.0.3:80 }
    monitor http
    load-balancing-mode round-robin
}
```

**パラメータ詳細:**

- **members**: IP:port の リストを指定
  ```cisco
  members add { 10.0.0.1:80 10.0.0.2:80 }   # 複数ポート可
  members { 192.168.1.1:443 }                # 追加指定
  ```

- **monitor**: ヘルスチェック方式
  ```cisco
  monitor http          # HTTP GET ヘルスチェック
  monitor tcp           # TCP接続確認のみ
  monitor icmp          # ICMP ping
  monitor none          # チェック無し（常にup）
  ```

- **load-balancing-mode**: 分散方式
  ```cisco
  load-balancing-mode round-robin                 # ラウンドロビン（デフォルト）
  load-balancing-mode least-connections-member    # 接続数が少ないメンバー優先
  load-balancing-mode static-member-order         # 設定順（固定）
  ```

**例: 複数パターン**

```cisco
# パターンA: HTTP LB（最少接続）
create ltm pool api_pool {
    members add { 10.0.0.10:8080 10.0.0.11:8080 }
    monitor http
    load-balancing-mode least-connections-member
}

# パターンB: HTTPS LB（ラウンドロビン）
create ltm pool https_pool {
    members add { 10.0.0.20:443 10.0.0.21:443 }
    monitor tcp
    load-balancing-mode round-robin
}

# パターンC: DB LB（モニタなし、常に up）
create ltm pool db_pool {
    members add { 10.1.0.1:3306 10.1.0.2:3306 }
    monitor none
    load-balancing-mode round-robin
}
```

---

### 3. Virtual Server（VIP）作成

```cisco
tmsh create ltm virtual vs_web {
    destination 192.0.2.10:80
    pool web_pool
    profiles add { http tcp }
}
```

**パラメータ:**

- **destination**: VIP の IP:port（クライアントが接続する先）
- **pool**: トラフィックを転送する Pool
- **profiles**: プロトコルプロファイル（http/tcp/ssl等）

**複数VIP の例:**

```cisco
create ltm virtual vs_api {
    destination 192.0.2.20:8080
    pool api_pool
    profiles add { http tcp }
}

create ltm virtual vs_db {
    destination 192.0.2.30:3306
    pool db_pool
    profiles add { tcp }
}
```

---

### 4. ノード（実サーバIP）の明示的定義

省略可能ですが、複数メンバーで同じIPを異なるポート指定する場合は明示的に定義：

```cisco
create ltm node 10.0.0.1 {
    address 10.0.0.1
}
```

---

### 5. ヘルスモニター（カスタム）作成

デフォルトのモニター以外を使用する場合：

```cisco
create ltm monitor http custom_http {
    # 詳細設定（現在のエミュレーターでは基本的にサポート）
}
```

現在のエミュレーターではモニター設定は保存されますが、自動health判定には使用されません。
メンバー状態の up/down は **手動で切り替え** するか、CLI で明示します。

---

## 📊 状態確認・管理

### Pool 状態確認

```bash
tmsh show ltm pool web_pool
```

**出力例:**

```
Ltm::Pool: web_pool
────────────────────────────────────────
  Status
    Availability : available (green)
    State        : enabled
    Load Balancing Mode : round-robin
    Monitor      : http
    Members      : 3 (up: 3, down: 0)

  Member: 10.0.0.1:80   available (green)
  Member: 10.0.0.2:80   available (green)
  Member: 10.0.0.3:80   available (green)

  Totals
    Clients        : 25
    Connections    : 47
```

### Virtual Server 状態確認

```bash
tmsh show ltm virtual vs_web
```

**出力例:**

```
Ltm::Virtual Server: vs_web
────────────────────────────────────────
  Status
    Availability : available (green)
    State        : enabled
    Destination  : 192.0.2.10:80
    Pool         : web_pool
    Members Up   : 3
    Profiles     : http tcp
```

### 全ノード表示

```bash
tmsh show ltm node
```

```
Ltm::Node List
────────────────────────────────────────
  10.0.0.1      address: 10.0.0.1      state: enabled
  10.0.0.2      address: 10.0.0.2      state: enabled
  10.0.0.3      address: 10.0.0.3      state: enabled
```

### モニター一覧

```bash
tmsh show ltm monitor
```

```
Ltm::Monitor List
────────────────────────────────────────
http      (type: http)
tcp       (type: tcp)
icmp      (type: icmp)
```

---

## 🔄 メンバー管理

### メンバー追加

```cisco
modify ltm pool web_pool members add { 10.0.0.4:80 }
```

### メンバー削除

```cisco
modify ltm pool web_pool members delete { 10.0.0.4:80 }
```

### メンバーの手動 up/down（保守用）

```cisco
# メンバーを下線（切り離し）
modify ltm pool web_pool members modify { 10.0.0.1:80 { state user-down } }
```

**状態コマンド:**

| 状態 | コマンド | 説明 |
|---|---|---|
| Up（利用可能） | `state user-up` | メンバーが利用可能 |
| Down（切り離し） | `state user-down` | 保守/更新時に切り離し |
| Disabled | `session user-disabled` | セッション無効 |

**確認:**

```bash
tmsh show ltm pool web_pool
# Members : 3 (up: 2, down: 1)  ← 1つが down
```

→ VIP へのアクセスは自動的に残り 2 メンバーに振り分け

---

## 📁 設定の保存・確認・削除

### 実行中の全設定を表示

```bash
tmsh show running-config
```

### Pool / Virtual の詳細設定表示

```bash
tmsh list ltm pool web_pool
```

出力例：

```
ltm pool web_pool {
    load-balancing-mode round-robin
    members {
        10.0.0.1:80 {
            address 10.0.0.1
        }
        10.0.0.2:80 {
            address 10.0.0.2
        }
        10.0.0.3:80 {
            address 10.0.0.3
        }
    }
    monitor http
}
```

### 設定の保存

```bash
tmsh save sys config
```

エミュレーターでは設定はメモリに保持されます（デバイス削除時に消去）。

### リソース削除

```cisco
# Virtual Server 削除
delete ltm virtual vs_web

# Pool 削除
delete ltm pool web_pool

# ノード削除
delete ltm node 10.0.0.1

# モニター削除
delete ltm monitor http custom_http
```

**注意**: Pool 削除前に Virtual が参照していないか確認

---

## 🧪 テストシナリオ

### シナリオ1: 基本的なラウンドロビン LB

```cisco
# 3台のウェブサーバにトラフィックを均等分散

create ltm pool web_pool {
    members add { 10.0.0.1:80 10.0.0.2:80 10.0.0.3:80 }
    monitor http
    load-balancing-mode round-robin
}

create ltm virtual vs_web {
    destination 192.0.2.10:80
    pool web_pool
    profiles add { http tcp }
}

# 検証
tmsh show ltm pool web_pool
tmsh show ltm virtual vs_web
```

### シナリオ2: 最少接続分散 + メンバー切り離し

```cisco
create ltm pool api_pool {
    members add { 10.0.0.10:8080 10.0.0.11:8080 10.0.0.12:8080 }
    monitor tcp
    load-balancing-mode least-connections-member
}

create ltm virtual vs_api {
    destination 192.0.2.20:8080
    pool api_pool
    profiles add { http tcp }
}

# サーバ保守: 10.0.0.10 を一時切り離し
modify ltm pool api_pool members modify { 10.0.0.10:8080 { state user-down } }

# 確認（up: 2, down: 1）
tmsh show ltm pool api_pool

# 保守完了: 復旧
modify ltm pool api_pool members modify { 10.0.0.10:8080 { state user-up } }
```

### シナリオ3: 複数プール・複数VIP（フルメッシュ）

```cisco
# Web Tier
create ltm pool web_tier {
    members add { 10.0.1.1:80 10.0.1.2:80 }
    monitor http
    load-balancing-mode round-robin
}

create ltm virtual vs_web {
    destination 192.0.2.1:80
    pool web_tier
    profiles add { http tcp }
}

# App Tier
create ltm pool app_tier {
    members add { 10.0.2.1:8080 10.0.2.2:8080 }
    monitor tcp
    load-balancing-mode least-connections-member
}

create ltm virtual vs_app {
    destination 192.0.2.2:8080
    pool app_tier
    profiles add { http tcp }
}

# DB Tier
create ltm pool db_tier {
    members add { 10.0.3.1:3306 10.0.3.2:3306 }
    monitor tcp
    load-balancing-mode round-robin
}

create ltm virtual vs_db {
    destination 192.0.2.3:3306
    pool db_tier
    profiles add { tcp }
}

# 全設定確認
tmsh show running-config
```

---

## 🔗 Netmiko での制御

Python Netmiko を使用した自動化例：

```python
from netmiko import ConnectHandler

device = {
    'device_type': 'f5_tmsh',
    'host': '192.168.1.100',
    'username': 'admin',
    'password': 'admin'
}

with ConnectHandler(**device) as net_connect:
    # Pool 作成
    commands = [
        'create ltm pool web_pool {',
        '    members add { 10.0.0.1:80 10.0.0.2:80 }',
        '    monitor http',
        '    load-balancing-mode round-robin',
        '}'
    ]
    
    for cmd in commands:
        net_connect.send_command(cmd)
    
    # 確認
    output = net_connect.send_command('show ltm pool web_pool')
    print(output)
```

---

## ⚠️ 制限事項・注意点

### 現在のエミュレーター実装での制限

```
✅ 対応:
  - Pool / Virtual / Node / Monitor 基本操作
  - メンバー状態管理（up/down 手動切り替え）
  - ラウンドロビン / 最少接続分散
  - show / list / delete コマンド
  - tmsh シェル対応

⚠️ 非対応（今後の予定）:
  - 自動ヘルスモニター（メンバーの自動 down 判定）
  - GTM / DNS ロードバランシング
  - iRule（ビジネスロジック）
  - SNAT / NAT の詳細動作
  - SSL/TLS オフロード
  - F5OS (次世代OS) REST API
```

### 実機との違い

| 機能 | エミュレーター | 実機 |
|---|---|---|
| Pool/Virtual CRUD | ✅ | ✅ |
| メンバー状態表示 | 手動設定 | 自動health判定 |
| 接続カウンタ | 簡易版 | 精密測定 |
| パフォーマンス | シミュレーション | 実測値 |
| セッション永続化 | 非対応 | ✅ |

---

## 📚 参考資料

- **F5 BIG-IP TMOS Documentation**: https://support.f5.com/csp/knowledge-center/software/BIG-IP
- **LTM Concepts**: https://www.f5.com/services/resources/whitepapers/ltm-concepts
- **tmsh Guide**: https://support.f5.com/csp/knowledge-center/software/BIG-IP

---

## 🎯 まとめ

F5 BIG-IP LTM でのロードバランシングフロー：

```
1. Pool 作成（メンバー定義 + モニター + 分散方式）
   ↓
2. Virtual Server 作成（VIP + Pool 割り当て）
   ↓
3. 状態確認（show / list）
   ↓
4. 必要に応じてメンバーを up/down 切り替え
   ↓
5. 設定保存（save sys config）
```

このシンプルな構造で複数トラフィックパターンをエミュレーション可能です。
