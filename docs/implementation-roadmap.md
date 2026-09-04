# 実装ロードマップ - 機能拡張タスク

このドキュメントは、Network Lab Emulator で現在**部分実装**または**未実装**の機能を列挙し、優先度順に拡張計画を示します。

---

## 🎯 優先度別実装タスク

### Priority 1: 高影響度・高頻出（すぐに実装推奨）

#### 1-1. BGP Community 属性
**現状**: 未実装  
**影響度**: 中～高（BGP ポリシー操作で必須）  
**難度**: 中

```python
# 実装内容:
# - BGP route に community 属性追加
# - route-map で set/match community
# - show ip bgp で community 表示
# - send-community neighbor コマンド対応

# CLI例:
# route-map SET_COMMUNITY permit 10
#  set community 65000:100
# neighbor 10.0.0.2 route-map SET_COMMUNITY out

# 期待出力:
# show ip bgp 192.168.1.0
#   Network        Next Hop    Metric LocPrf Weight Path Communities
#   192.168.1.0    10.0.0.2         0      90       0 65001 65000:100
```

**対応ファイル**:
- `engine/rules.py`: route-map SET community コマンド
- `engine/protocols.py`: BgpEngine に community フィールド追加

**テスト例**:
```python
def test_bgp_community():
    # Cisco が community:65000:100 を付与して広告
    # ISR が受け取って community 表示
    # route-map で community フィルタ
```

---

#### 1-2. distribute-list コマンド実装
**現状**: エンジン層には存在（protocols.py）だが CLI がない  
**影響度**: 中（RIP/OSPF フィルタリングに必須）  
**難度**: 低～中

```cisco
# CLI を追加:
router ospf 1
 distribute-list 1 in GigabitEthernet1/0/1
 distribute-list prefix-list PL_FILTER out

# または RIP:
router rip
 distribute-list 2 out GigabitEthernet1/0/1
```

**対応ファイル**:
- `engine/rules.py`: distribute-list コマンドハンドラ追加
- `engine/protocols.py`: 既存フィルタ層を活用

**テスト例**:
```python
def test_distribute_list_ospf():
    # Si-R で distribute-list in を設定
    # Catalyst からの特定経路が学習されないことを確認
```

---

#### 1-3. OSPF NSSA (Not-So-Stubby Area)
**現状**: 未実装（Stub Area は実装）  
**影響度**: 中（マルチエリア OSPF で高度な集約に必須）  
**難度**: 中～高

```cisco
area 2 nssa
area 2 nssa default-information-originate
```

**対応ファイル**:
- `engine/protocols.py`: OspfEngine に NSSA ロジック追加
- LSA Type 7 (NSSA External) 生成・変換

**テスト例**:
```python
def test_ospf_nssa():
    # Area 1 (NSSA) に ASA が static route を注入
    # ABR が Type 7 → Type 5 に変換
    # Area 0 で E1/E2 経路として学習
```

---

#### 1-4. Big-IP LTM テストツール実装
**現状**: 機能実装済み ✅ だが、テストツール `tools/test_bigip_ltm.py` が未実装  
**影響度**: 低～中（検証用）  
**難度**: 低

```bash
# 実装後の使用:
python tools/test_bigip_ltm.py --emulator
# 出力: Pool 作成・削除、メンバー管理、VIP 設定、状態確認テスト
```

**対応ファイル**:
- `tools/test_bigip_ltm.py` (新規作成)

**テスト内容**:
- Pool CRUD 操作
- Virtual Server 設定
- メンバー状態管理（up/down）
- 負荷分散方式の確認
- 複数 Pool・VIP の共存

---

### Priority 2: 中程度の影響度（2-3週間後推奨）

#### 2-1. HSRP コマンドの完全実装
**現状**: 基本実装あり（show standby）だが、CLI 設定コマンド不完全  
**影響度**: 中（冗長化検証で必須）  
**難度**: 中

```cisco
# 現在未対応:
interface GigabitEthernet1/0/1
 standby 1 ip 10.0.0.10
 standby 1 priority 120
 standby 1 preempt
 standby 1 hello 3
 standby 1 hold 10
 standby 1 authentication md5 key-chain HSRP-KEY
```

**対応ファイル**:
- `engine/rules.py`: standby コマンドハンドラ完成
- `engine/protocols.py`: HSRP タイマー・認証・優先度反映

---

#### 2-2. IPv6 ルーティング（基本）
**現状**: 未実装  
**影響度**: 中（IPv6 時代対応）  
**難度**: 中～高

```cisco
# IPv6 設定例:
interface GigabitEthernet1/0/1
 ipv6 address 2001:db8::1/64
 ipv6 enable

router ospfv3 1
 router-id 1.1.1.1
 interface GigabitEthernet1/0/1 area 0
```

**対応ファイル**:
- `engine/rules.py`: ipv6 address / ipv6 enable
- `engine/protocols.py`: OSPFv3, RIPng, BGP IPv6 AFI

---

#### 2-3. QoS (Quality of Service)
**現状**: 未実装  
**影響度**: 低～中（高度なネットワーク検証向け）  
**難度**: 高

```cisco
class-map VOICE
 match ip dscp ef

policy-map QOS_POLICY
 class VOICE
  priority 1000

interface GigabitEthernet1/0/1
 service-policy output QOS_POLICY
```

---

### Priority 3: 低影響度・将来向け（保留中）

#### 3-1. Stateful Firewall (ASA)
- Connection tracking
- Session timeout

#### 3-2. マルチキャスト
- IGMP
- PIM-SM / PIM-DM

#### 3-3. VPN
- IPSec IKEv2
- GRE Tunnel

---

## 🔄 対応機能別の組み合わせギャップ

### Gap A: BGP + distribute-list
**現象**: BGP で distribute-list が効かない  
**原因**: distribute-list CLI 未実装  
**解決**: Priority 1-2 の実装で解決

```
Before: BGP が全経路を配信 → distribute-list で絞りたいが設定不可
After:  distribute-list PL_FILTER in Gi1/0/1 で設定可能
```

---

### Gap B: OSPF Multiarea + NSSA
**現象**: NSSA Area では Type 7 LSA が Type 5 に変換されない  
**原因**: NSSA ロジック未実装  
**解決**: Priority 1-3 の実装で解決

```
Before: Area 1 NSSA での Type 7 LSA が ABR で Type 5 に変わらない
After:  正しく変換され、Area 0 で E2 経路として学習
```

---

### Gap C: EtherChannel + VRRP/HSRP
**現象**: port-channel に対して standby を設定しても反映されない可能性  
**原因**: port-channel と HSRP の統合が不完全  
**解決**: Priority 2-1 の実装で解決

```
Before: interface port-channel 1
         standby 1 ip 10.0.0.10
         ↑ 設定されるが動作未検証
After:  HSRP が正しく動作、フェイルオーバー確認可能
```

---

### Gap D: Big-IP + Netmiko 統合テスト
**現象**: Big-IP を Netmiko で制御したいが、テストツールがない  
**原因**: tools/test_bigip_ltm.py 未実装  
**解決**: Priority 1-4 の実装で解決

```
Before: Big-IP は動作するが、自動テストできない
After:  pytest で Big-IP LTM 全機能をテスト可能
```

---

## 📋 実装優先度サマリー

| # | 機能 | 難度 | 影響度 | 推奨時期 | ファイル |
|---|------|------|--------|---------|----------|
| 1-1 | BGP Community | 中 | 高 | 即 | protocols.py / rules.py |
| 1-2 | distribute-list CLI | 低 | 中 | 即 | rules.py |
| 1-3 | OSPF NSSA | 高 | 中 | 1週間 | protocols.py |
| 1-4 | Big-IP Test Tool | 低 | 低 | 即 | tools/test_bigip_ltm.py |
| 2-1 | HSRP 完全実装 | 中 | 中 | 2週間 | rules.py / protocols.py |
| 2-2 | IPv6 基本 | 高 | 中 | 3週間 | rules.py / protocols.py |
| 2-3 | QoS | 高 | 低 | 1ヶ月 | rules.py / protocols.py |

---

## 🛠️ 実装開始までの準備

各機能のテストを先に書く（テスト駆動開発）:

```bash
# Priority 1-1: BGP Community テスト
pytest tests/test_bgp_community.py -v

# Priority 1-2: distribute-list テスト
pytest tests/test_distribute_list.py -v

# Priority 1-3: OSPF NSSA テスト
pytest tests/test_ospf_nssa.py -v

# Priority 1-4: Big-IP テストツール
pytest tools/test_bigip_ltm.py --emulator -v
```

---

## 📊 期待される効果

実装後のテストカバレッジ:

```
現在:  100/150 テストケース (66%)
    ✅ 基本機能
    ✅ マルチプロトコル
    ✅ 冗長構成
    ❌ BGP Community / distribute-list
    ❌ OSPF NSSA
    ❌ Big-IP 自動テスト

実装後: 130/150 テストケース (87%)
    ✅ + BGP Community
    ✅ + distribute-list フィルタ
    ✅ + OSPF NSSA
    ✅ + Big-IP LTM テスト
```

---

## 🚀 推奨実装順序

```
Week 1:
  [ ] 1-1: BGP Community 属性追加
  [ ] 1-2: distribute-list CLI 実装
  [ ] 1-4: Big-IP テストツール作成

Week 2:
  [ ] 1-3: OSPF NSSA 実装開始
  [ ] テスト追加

Week 3:
  [ ] 2-1: HSRP 完全実装
  [ ] リグレッション確認

Week 4:
  [ ] 2-2: IPv6 基本実装検討
  [ ] ドキュメント更新
```
