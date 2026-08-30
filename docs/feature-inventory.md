# 機能インベントリ - 実装済み全機能リスト

このドキュメントは、Network Lab Emulator に実装されているすべてのデバイス、プロトコル、機能を列挙しています。

---

## 📱 対応デバイス

### ネットワークデバイス

| デバイス | タイプ | ベンダー | コマンド体系 | Netmiko | Paramiko | テスト |
|---------|--------|---------|-----------|---------|----------|--------|
| **Si-R G120** | ルータ | 富士通 | Si-R準拠 | ❌ | ✅ | ✅ |
| **SR-S324TR1** | L3スイッチ | 富士通 | SR-S準拠 | ❌ | ✅ | ✅ |
| **Catalyst 9300** | L3スイッチ | Cisco | IOS-XE 17.x | ✅ `cisco_ios` | ✅ | ✅ |
| **Cisco ISR** | ルータ | Cisco | IOS準拠 | ✅ `cisco_ios` | ✅ | ✅ |
| **Nexus 9300** | L3スイッチ | Cisco | NX-OS 10.2 | ✅ `cisco_nxos` | ✅ | ✅ |
| **ASA** | ファイアウォール | Cisco | ASA 9.x準拠 | ✅ `cisco_asa` | ✅ | ✅ |
| **APRESIA Light GM200** | L2/L3スイッチ | APRESIA | ApresiaLight準拠 | ⚠️ | ✅ | ✅ |

### ホスト・エンドデバイス

| デバイス | 機能 | 用途 |
|---------|------|------|
| **PC** | Ping / ICMP / ゲートウェイ設定 | 通信テスト用エンドデバイス |

---

## 🌐 ルーティングプロトコル

### RIP v2

```
✅ 実装済み機能:
  - ネイバー確立・タイムアウト
  - メトリック計算 (ホップ数, max 15)
  - 複数ネイバー対応
  - MD5認証 (キー不一致時の拒否)
  - 周期的Update送信
  - タイムアウト検出・再学習
```

**テスト**: `pytest tests/test_filtering_auth_ecmp.py::TestRipMd5Authentication -v`

---

### OSPF

```
✅ 実装済み機能:
  - DR/BDR選出
  - LSA交換 (Type 1-5)
  - SPF計算 (Dijkstra)
  - Area 0 ベース
  - 複数隣接 (Full状態)
  - 複数経路学習
  
✅ 高度な機能:
  - マルチエリア対応
  - ABR (Area Border Router)
  - Summary LSA (Type 3)
  - Area間経路学習 (O IA)
  - MD5認証
  - Dead Timer (hello timeout)
  - Cost計算

✅ 検証済み:
  - 8台フルメッシュ (56隣接 Full)
  - 10台チェーン (9ホップ伝播)
  - マルチエリア経路集約
```

**テスト**: 
- `pytest tests/test_ospf_multiarea.py -v`
- `python tests/test_extended_topologies.py`

---

### BGP (eBGP)

```
✅ 基本機能:
  - セッション確立
  - eBGP (異AS)
  - 経路広告 (NLRI)
  - 複数AS対応
  - 複数neighbor・複数prefix

✅ 高度な機能:
  - AS-path prepend (経路長伸張)
  - local-preference (ローカル優先度)
  - MED (Multi-Exit Discriminator)
  - route-map filtering
  - Prefix-list filtering (in/out)
  - ge/le レンジ指定
  - MD5認証

✅ 検証済み:
  - 8AS フルメッシュ (28セッション)
  - AS-path prepend による経路制御
  - local-pref による最適パス選択
  - MED による出口選択
  - prefix-list による経路制御 (11パターン)
```

**テスト**:
- `pytest tests/test_bgp_advanced.py -v`
- `pytest tests/test_filtering_auth_ecmp.py::TestBgpPrefixListFilter -v`

---

### Static Route

```
✅ 機能:
  - ip route コマンド
  - AD値 (Administrative Distance)
  - フローティングスタティック
  - マルチプロトコル混在での経路選択
  - ECMP (複数等コスト経路)

✅ 検証済み:
  - Static → OSPF/RIP/BGP のAD比較
  - フェイルオーバー・復旧
  - マルチプロトコル経路選択
```

**テスト**: `pytest tests/test_multivendor_multi_neighbor.py -v`

---

### 経路ジェネレータ

```
✅ 実装方法:
  - Static Route 投入 (ip route ...)
  - redistribute コマンド (static → OSPF/RIP/BGP)
  - メトリック変換

✅ 対応プロトコル:
  - Static → OSPF (外部経路 O E2)
  - Static → RIP (メトリック20)
  - Static → BGP (external)

✅ 検証方法:
  - Netmiko: show ip route (Catalyst)
  - Paramiko: show ip route (Si-R)
  - HTTP API: show ip route (エミュレーター)
```

**テスト**: `python tools/test_etherchannel_stp_cost.py --emulator`

---

## 🔄 スイッチング・冗長化

### VLAN

```
✅ 機能:
  - VLAN 作成・削除
  - VLAN メンバシップ設定
  - Trunk ポート (802.1q)
  - SVI (Switched Virtual Interface)
  - ネイティブVLAN

✅ 対応デバイス:
  - Catalyst / SR-S / Nexus / APRESIA
```

---

### STP / Rapid-PVST+

```
✅ BPDU処理:
  - Root Bridge選出 (BID比較)
  - Port Role決定
    - Root Port (最小コスト)
    - Designated Port
    - Alternate Port (ブロック)
  - ポートコスト計算
  - Port Priority (128, 129, ...)

✅ 機能:
  - PortFast
  - BPDU Guard
  - ポートコスト設定 (spanning-tree cost)
  - Port Priority設定
  - Rapid Convergence

✅ 検証済み:
  - SR-S ↔ Catalyst EtherChannel
  - port-channel のSTPコスト反映
  - ルートのフェイルオーバー
```

**テスト**: `python tools/test_etherchannel_stp_cost.py --emulator`

---

### LACP / EtherChannel

```
✅ 機能:
  - LACP (Link Aggregation Control Protocol)
    - Active / Passive モード
  - Static バンドル (no LACP)
  - channel-group 設定
  - port-channel 作成

✅ 統合機能:
  - port-channel の STP コスト反映
  - port-channel の OSPF/BGP対応
  - Min-links設定

✅ 対応デバイス:
  - Catalyst / SR-S / Nexus

✅ 検証済み:
  - 2ポートバンドル
  - ポートコスト計算
  - STP での単一ポート扱い
```

**テスト**: `python tools/test_etherchannel_stp_cost.py --emulator`

---

### vPC (NX-OS専用)

```
✅ 機能:
  - Primary / Secondary役割
  - Peer-Link (冗長リンク)
  - Keepalive channel
  - VSS (Virtual Switching System)相当
```

**対応**: Nexus のみ

---

### VRRP / HSRP

```
✅ VRRP 機能:
  - Master / Backup遷移
  - Preempt設定
  - Virtual Gateway IP
  - Priority による Master選出

✅ HSRP 機能:
  - Hot Standby ルーター役割
  - Hello/Holdtime
  - Active / Standby状態

✅ 対応デバイス:
  - Catalyst / Cisco ISR / Nexus / Si-R
```

---

## 🔐 フィルタリング・セキュリティ

### ACL (Access Control List)

```
✅ タイプ:
  - 標準ACL (Source IP)
  - 拡張ACL (Source/Dest IP/Port)
  - 命名ACL

✅ 操作:
  - permit / deny
  - ポート範囲
  - プロトコル (TCP/UDP/ICMP)
  - ワイルドカードマスク

✅ 適用:
  - インターフェース (in/out)
  - ルータACL
  - Switchport ACL
```

---

### Prefix-list

```
✅ 機能:
  - permit / deny
  - CIDR表記サポート
  - ge / le レンジ指定

✅ 用途:
  - BGP neighbor に適用
  - Outbound / Inbound フィルタ
  - route-map での参照

✅ 検証済み:
  - BGP outbound フィルタ (拒否経路ブロック)
  - BGP inbound フィルタ (受信拒否)
  - ge/le による/24のみ許可など
  - 11/11 テスト成功
```

**テスト**: `pytest tests/test_filtering_auth_ecmp.py::TestBgpPrefixListFilter -v`

---

### Route-map

```
✅ 機能:
  - Match条件
    - prefix (prefix-list参照)
    - AS-path
    - IP address (ACL参照)
  
  - Set操作
    - as-path prepend (AS-path伸張)
    - local-preference (優先度変更)
    - metric / med設定
```

---

### ICMP

```
✅ 機能:
  - Echo Request (Ping要求)
  - Echo Reply (Ping応答)
  - TTL検証
  - Unreachable通知

✅ 対応デバイス:
  - PC / ルータ / スイッチ
```

---

### IPFilter

```
✅ 機能:
  - IP アドレス範囲フィルタ
  - CIDR表記
```

---

### NAT (Network Address Translation)

```
✅ 機能:
  - Inside / Outside定義
  - Static NAT (1:1 mapping)
  - Dynamic NAT
  - PAT (Port Address Translation)

✅ 対応デバイス:
  - Cisco ISR / ASA
```

---

### Firewall (ASA向け)

```
✅ 機能:
  - ACL ベースのフィルタリング
  - ステートフルファイアウォール (計画中)
```

---

## 🔍 ネットワーク管理・監視

### ARP (Address Resolution Protocol)

```
✅ 機能:
  - ARP テーブル学習
  - ARP 送受信
  - ARP エージング

✅ 出力:
  - show arp
  - show ip arp
```

---

### CEF (Cisco Express Forwarding)

```
✅ 機能:
  - FIB (Forwarding Information Base)
  - 高速パケット転送
  - 分散転送

✅ 対応: Cisco系デバイス
```

---

### DP (Data Plane)

```
✅ 機能:
  - パケット転送エンジン
  - キューイング
  - スケジューリング
```

---

### Syslog

```
✅ 機能:
  - リアルタイムログ転送
  - UDP 514 でのリモート送信
  - 機種別ログフォーマット

✅ 対応ログ:
  - OSPF 隣接変化
  - BGP セッション状態
  - STP トポロジー変化
  - インターフェース状態
  - ルーティング変化
```

---

### SNMP

```
✅ 機能:
  - Community設定
  - Trap送信
  - デバイス情報取得
```

---

### NTP

```
✅ 機能:
  - サーバ同期シミュレーション
  - タイムスタンプ
```

---

### Genie

```
✅ 機能:
  - 構造化データ解析
  - Cisco デバイス連携 (計画中)
```

---

## 📊 テスト・検証エンジン

### RIB (Routing Information Base)

```
✅ 機能:
  - 経路集約
  - AD値ベースの経路選択
  - ECMP (Equal-Cost Multi-Path) 集約
  - 次ホップ決定

✅ ECMP機能:
  - 複数等コスト経路の集約
  - 最大4パス (Ciscoデフォルト相当)
  - ロードバランシング

✅ 検証済み:
  - Static ECMP (2-4パス)
  - OSPF ECMP
  - AD違いの経路除外
```

**テスト**: `pytest tests/test_filtering_auth_ecmp.py::TestEcmp -v`

---

### redistribute

```
✅ 機能:
  - プロトコル間の経路転換
  - メトリック変換
  - ルート再配信

✅ 対応パターン:
  - Static → OSPF/RIP/BGP
  - RIP ↔ OSPF
  - OSPF ↔ BGP
  - BGP ↔ Static
```

---

### Filter Engine

```
✅ 機能:
  - Prefix-list管理
  - Access-list管理
  - Route-map管理
  - distribute-list (計画中)

✅ 処理:
  - BGP 隣接フィルタ
  - OSPF 経路フィルタ
  - RIP 経路フィルタ
```

---

## 🧪 自動テストスイート

```
✅ テストスイート:
  - pytest ベース (Python)
  - スタンドアロンスクリプト

✅ テストカテゴリ:
  - プロトコル機能テスト (RIP/OSPF/BGP)
  - マルチベンダー相互接続テスト
  - マルチプロトコル混在テスト
  - 大規模トポロジテスト (8-10台)
  - 高度な機能テスト (prepend/MED/local-pref)
  - 認証・フィルタテスト
  - ECMP テスト

✅ テスト数:
  - 100+ テストケース
  - 成功率: 100% (実装済み機能)
  - マルチベンダー検証

✅ テストファイル:
  - test_protocols.py
  - test_device_os.py
  - test_multivendor_neighbors.py
  - test_multivendor_multi_neighbor.py
  - test_extended_topologies.py
  - test_bgp_advanced.py
  - test_ospf_multiarea.py
  - test_filtering_auth_ecmp.py
  - test_netmiko_catalyst.py
  - test_ospf_routing_verification.py
  - test_etherchannel_stp_cost.py
```

---

## 🔌 ツール・統合機能

### Netmiko 統合

```
✅ 対応デバイス:
  - Catalyst (cisco_ios)
  - Cisco ISR (cisco_ios)
  - Nexus (cisco_nxos)
  - ASA (cisco_asa)

✅ 機能:
  - SSH接続
  - CLI コマンド実行
  - 設定投入 (send_config_set)
  - 状態確認 (show コマンド)

✅ テストツール:
  - tools/test_netmiko_integration.py (実機)
  - tools/test_netmiko_catalyst.py (pytest)
  - tools/test_ospf_routing_verification.py (OSPF検証)
```

---

### Paramiko 統合

```
✅ 対応デバイス:
  - Si-R (netmiko未対応を補完)
  - SR-S (netmiko未対応を補完)
  - その他SSH対応デバイス

✅ 機能:
  - SSH接続 (鍵認証・パスワード認証)
  - CLI コマンド実行
  - ルーティングテーブル取得
  - 設定確認

✅ テストツール:
  - tools/test_ospf_routing_verification.py (--real-sir)
  - tools/test_etherchannel_stp_cost.py (--sir-routes)
```

---

### EVE-NG 連携

```
✅ 機能:
  - 構成のエクスポート
  - 実機へのデプロイ (netmiko経由)
  - リアルタイム検証

✅ ツール:
  - tools/eveng_deploy.py
    - export: 構成ファイル生成
    - deploy: 実機へのデプロイ
    - verify: show コマンド検証
```

---

### HTTP API

```
✅ エンドポイント:
  - /api/device (デバイス登録・削除)
  - /api/cli (CLIコマンド実行)
  - /api/link (リンク作成・削除)
  - /api/export (構成エクスポート)
  - /api/status (ステータス確認)

✅ 機能:
  - RESTful インターフェース
  - JSON リクエスト/レスポンス
  - リアルタイムコマンド実行
```

---

### WebSocket

```
✅ 機能:
  - リアルタイムプロトコルシミュレーション
  - イベント通知
  - ライブログ配信
```

---

## 📈 まとめ表

| カテゴリ | 実装数 | テスト | 備考 |
|---------|------|------|------|
| **デバイス** | 8機種 | ✅ | Si-R/SR-S/Catalyst/Cisco/Nexus/ASA/APRESIA/PC |
| **ルーティング** | 4プロトコル | ✅ | RIP/OSPF/BGP/Static + redistribute |
| **スイッチング** | 6機能 | ✅ | VLAN/STP/LACP/vPC/VRRP/HSRP |
| **フィルタ/セキュリティ** | 7機能 | ✅ | ACL/Prefix-list/Route-map/ICMP/IPFilter/NAT/Firewall |
| **管理・監視** | 6機能 | ✅ | ARP/CEF/DP/Syslog/SNMP/NTP |
| **テストツール** | 11スイート | ✅ | 100+ テストケース |
| **統合ツール** | 4種類 | ✅ | Netmiko/Paramiko/EVE-NG/HTTP API |

---

## 🚀 今後の予定

### 計画中の機能

```
⬜ HSRP の詳細実装
⬜ distribute-list
⬜ BGP community
⬜ OSPF NSSA
⬜ QoS (Queuing/Shaping)
⬜ マルチキャスト
⬜ IPv6
```

### パフォーマンス最適化

```
⬜ 大規模 AS/prefix スケール テスト
⬜ RIPメトリック非対称性の改善
⬜ シミュレーション値の最適化
```

---

## 📚 参考資料

- **Netmiko**: https://github.com/ktbyers/netmiko
- **Paramiko**: https://github.com/paramiko/paramiko
- **Cisco**: https://www.cisco.com/
- **富士通**: https://www.fujitsu.com/jp/

