# Catalyst への RIP 経路注入テスト（route_injector + 実機検証）

本ドキュメントは、**Windows PC 上の route_injector ツール**を使って、**実機 Catalyst 機器へ RIP 経路を注入・検証する**ための手順書です。

---

## 前置き

- **route_injector 所在**: `tools/route_injector/network_route_injector.py`（Tkinter GUI）
- **動作環境**: Windows のみ（管理者権限不要、scapy/Npcap 不要 for RIP）
- **検証方法**: 
  1. Catalyst 側に RIP を設定
  2. Windows PC から route_injector を起動
  3. Catalyst の `show ip route rip` で経路確認

---

## ネットワーク構成図

```
【EVE-NG 実機環境の例】
┌─────────────────────────────┐
│  EVE-NG ホスト上            │
│  ┌──────────────────────┐   │
│  │ Catalyst 3650        │   │
│  │ (Dist-SW)            │   │
│  │ IP: 10.0.0.254/24    │   │
│  │ (Vlan 10 など)       │   │
│  └────────────┬─────────┘   │
│               │ Cloud/pnet   │
│               │ ブリッジ     │
│               │              │
└───────────────┼──────────────┘
                │
        【物理ネットワーク】
                │
    ┌───────────┴──────────┐
    │                      │
┌───┴─────────────┐  ┌────┴──────────┐
│   Windows PC    │  │  (他のNIC)    │
│  route_injector │  │               │
│  10.0.0.100/24  │  │               │
└─────────────────┘  └───────────────┘
```

**重要**: PC と Catalyst は**同一 L2 セグメント**に接続必須。  
EVE-NG 使用時は `Cloud/pnet` で PC の NIC をエミュレーション・ブリッジに接続。

---

## ステップ 1: Catalyst 側の設定

### A) RIP 最小構成

```cli
enable
configure terminal

! RIP プロセス開始
router rip
 version 2
 network 10.0.0.0          ! ツールと同じセグメント（10.0.0.0/24）を指定
 exit

! インターフェース確認（既にあれば OK）
interface Vlan10
 ip address 10.0.0.254 255.255.255.0
 no shutdown
 exit

exit
```

**確認コマンド**:
```
show ip rip
show ip protocols
show ip route rip
```

期待出力:
```
Routing Protocol is "rip"
  ... RIP が UP している
```

---

### B) テスト用 VLAN の作成（例）

すでに `Vlan10` がある場合はスキップ。ない場合:

```cli
configure terminal
vlan 10
 name RIP-Test
 exit

interface Vlan10
 ip address 10.0.0.254 255.255.255.0
 no shutdown
 exit
```

---

## ステップ 2: Windows PC での route_injector 起動

### 前提条件

- Windows 7 以上
- Python 3.7 以上
- `pip install scapy` **は不要**（RIP タブでは不要）
- Tkinter は Python に同梱

### 起動コマンド

```bash
cd tools/route_injector
python network_route_injector.py
```

GUI が起動します。

---

## ステップ 3: route_injector [RIP] タブで経路を注入

### 3-1. タブ選択

GUI で **[RIP]** タブをクリック。

### 3-2. パラメータ入力

| 項目 | 入力値 | 説明 |
|------|--------|------|
| **Version** | `2` | RIPv2 |
| **Send to (宛先)** | `224.0.0.9` | RIPv2 マルチキャスト（推奨）<br>or Catalyst IP `10.0.0.254` |
| **Bind IP (送信元)** | `10.0.0.100` | PC のセグメント内の空き IP<br>（Catalyst と同じ 10.0.0.0/24）<br>※ Catalyst IP と重複しないこと |
| **TTL** | `1` | 同一セグメント内なら 1 で OK |

**例**:
```
Version: 2
宛先: 224.0.0.9
送信元IP: 10.0.0.100
TTL: 1
```

### 3-3. 経路を追加

**[経路追加]** ボタン → ダイアログで以下を入力:

| 項目 | 入力値 | 説明 |
|------|--------|------|
| **Network** | `172.16.50.0` | テスト用経路（任意） |
| **Netmask** | `255.255.255.0` | /24 |
| **Next Hop** | `10.0.0.100` | 送信元IP と同じ |
| **Metric** | `1` | ホップ数 |
| **Tag** | `0` | RIPv2 tags（通常は 0）|

例：複数経路を追加
```
1番目: 172.16.50.0/24 nexthop=10.0.0.100 metric=1
2番目: 192.0.2.0/24   nexthop=10.0.0.100 metric=2
3番目: 203.0.113.0/24 nexthop=10.0.0.100 metric=3
```

### 3-4. 送信

**[送信]** ボタンをクリック → RIP Update パケットが Catalyst へ送信されます。

---

## ステップ 4: Catalyst 側で検証

### 方法 A: CLI で直接確認（リアルタイム）

Catalyst の CLI で:

```
show ip route rip
```

期待出力例:
```
R       172.16.50.0 [120/1] via 10.0.0.100, 00:01:23, Vlan10
R       192.0.2.0 [120/2] via 10.0.0.100, 00:02:15, Vlan10
R       203.0.113.0 [120/3] via 10.0.0.100, 00:03:04, Vlan10
```

- `[120/x]` = RIP の AD/metric
- ルート削除 = route timeout/garbage collection（デフォルト 180s + 120s）

### 方法 B: eveng_deploy.py で自動検証

1. EVE-NG から export:
```bash
python tools/eveng_deploy.py export --api http://<EVE-NGホスト>:api_port --out ./eveng_out
```

2. inventory を編集（host/user/pass を実機に合わせる）

3. 検証スクリプト作成 (`checks.rip.json`):
```json
{
  "catalyst": [
    {
      "cmd": "show ip route rip",
      "expect": "172.16.50.0"
    },
    {
      "cmd": "show ip route rip",
      "expect": "192.0.2.0"
    }
  ]
}
```

4. 実行:
```bash
python tools/eveng_deploy.py verify \
  --inventory eveng_out/inventory.json \
  --checks checks.rip.json
```

---

## トラブルシューティング

### 問題 1: 「Cannot assign requested address」エラー

**原因**: Bind IP が PC に存在しない。

**解決**:
- PC の NIC IP を確認: `ipconfig` (Windows)
- route_injector の **[Bind IP]** を PC の実際の NIC IP に変更

### 問題 2: Catalyst が経路を学習しない

**チェック項目**:
1. **Catalyst の RIP が有効か**
   ```
   show ip rip
   show ip protocols | i RIP
   ```
   → "RIP is disabled" となっていないか？

2. **セグメント同一か**
   - `show ip int brief` で Vlan10 が 10.0.0.254/24 か確認
   - PC の IP が 10.0.0.0/24 範囲内か確認

3. **パケットが届いているか**
   - route_injector で **[詳細ログ]** を有効化
   - Catalyst で `debug ip rip` を実行（リアルタイム）

4. **ファイアウォール**
   - Windows Firewall が UDP 520 を許可しているか確認

### 問題 3: 経路がすぐ消える

**原因**: RIP timeout (デフォルト 180s) + garbage collection (120s) = ~5分で削除

**確認コマンド**:
```
show ip protocols
  Timers: Update 30s Timeout 180s Garbage collection 120s
```

**対策**: route_injector で定期的に Update を送信（例: 15秒ごと）

---

## 実際の使用例（複合シナリオ）

### シナリオ: AWS Direct Connect 冗長構成テスト

**構成**:
- Catalyst: ローカルルータ（AS 65000）
- route_injector（AS 65100）: 「AWS 側」を模擬

**Step 1: Catalyst で RIP を設定**
```
router rip
 version 2
 network 10.0.0.0
```

**Step 2: route_injector で AWS 側の経路を注入**
- 注入経路: `10.1.0.0/16`, `172.31.0.0/16` (AWS デフォルト CIDR)
- Nexthop: `10.0.0.100`
- Metric: 優先度を変動させてテスト（1 = 優先、3 = バックアップ）

**Step 3: Catalyst 側でフェイルオーバーをテスト**
- Primary link: route_injector で metric=1 → Catalyst は metric 1 の経路を選択
- Primary link 障害: route_injector の metric を 3 に変更 → Catalyst が自動切り替え
- 復旧: metric を 1 に戻す

---

## 次のステップ

- **OSPF テスト**: `[OSPF P2P]` / `[OSPF Broadcast]` タブ を使用  
  （scapy + Npcap + 管理者権限が必要）

- **BGP テスト**: `[BGP]` タブでネイバー確立・経路広告テスト

- **FlowSpec テスト**: BGP の `[FlowSpec]` 機能で DDoS 対策経路注入シミュレーション

---

## 参考資料

- `tools/route_injector/README.md` … ツール全体の解説
- `docs/config-parameters.md` … 各プロトコル対応状況一覧
- `tools/eveng_deploy.py` … 実機検証の自動化

