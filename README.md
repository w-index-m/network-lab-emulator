# Network Lab Emulator

マルチベンダー対応のネットワーク機器CLIエミュレーター。  
ブラウザ上で実機に近いCLI操作・ルーティングプロトコルのシミュレーションができます。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## 対応機種

| 機種 | コマンド体系 | 主な実装機能 |
|------|------------|------------|
| **富士通 Si-R G120** | Si-R Gシリーズ準拠 | RIP / OSPF / VRRP / BGP |
| **富士通 SR-S324TR1** | SR-Sシリーズ準拠 | VLAN / LACP / STP |
| **Cisco Catalyst 9300** | IOS-XE 17.x準拠 | OSPF / BGP / HSRP / STP / EtherChannel |
| **Cisco Nexus 9300** | NX-OS 10.2準拠 | OSPF / BGP / vPC / VRRP / LACP |
| **APRESIA ApresiaLight GM200** | ApresiaLight準拠 | VLAN / STP / LACP |

---

## 実装済み機能

### プロトコル
- **RIP v2** — ネイバー確立・経路学習・タイムアウト・メトリック
- **OSPF** — DR/BDR選出・LSA交換・SPF計算・Area 0
- **BGP (eBGP)** — セッション確立・経路広告・AS間ルーティング
- **スタティックルート** — AD値比較・フローティングスタティック
- **VRRP / HSRP** — Master/Backup遷移・preempt
- **STP / Rapid-PVST+** — Root Bridge選出・PortFast・BPDU Guard
- **LACP / EtherChannel** — バンドル・min-links
- **vPC (NX-OS)** — Primary/Secondary・Peer-Link・Keepalive

### CLI
- **Tab補完** — 実機準拠の前方一致補完
- **? ヘルプ** — モード別コマンド一覧表示
- **短縮コマンド** — `sh ip os ne` → `show ip ospf neighbor` 等
- **複数行一括投入** — クリップボードから設定ブロックをペースト（⎘ボタン）
- **エラーメッセージ** — ベンダー別 (`% Invalid input detected at '^' marker.` 等)

### ログ・監視
- **装置内ログ** — `show logging` / `show logging syslog` (機種別形式)
- **syslog転送** — UDP 514 へリアルタイム転送
- **SNMP** — コミュニティ設定・トラップ送信
- **NTP** — サーバ同期シミュレーション

---

## セットアップ

### 動作環境
- Python 3.10 以上
- RAM: 1GB 以上
- OS: Windows 10/11 / macOS / Linux

### インストール

```bash
# リポジトリをクローン
git clone https://github.com/your-username/network-lab-emulator.git
cd network-lab-emulator

# パッケージインストール
pip install -r requirements.txt

# 起動
python app.py
```

ブラウザで http://localhost:8000 を開く。

### Windows の場合

`start.bat` をダブルクリックするだけで起動します。  
Python が未インストールの場合はインストール手順を案内します。

---

## 使い方

### 基本操作

1. 画面上部の **ランチャー** から機種を選択してターミナルを開く
2. CLIでコマンドを入力（Tab補完・?ヘルプ対応）
3. 複数行コンフィグは **⎘ボタン**（貼り付け）で一括投入

### ラボ構成例（マルチベンダー接続）

```
APRESIA ─── Si-R G120 ─── Catalyst 9300 ─── Nexus 9300
10.0.12.0/30  10.0.23.0/30       10.0.34.0/30
     RIPv2          OSPF Area 0          eBGP
                              AS65001 ↔ AS65002
```

#### Si-R 設定例

```
configure
hostname Router-A
lan 0 ip address 10.0.23.1/30
router ospf 1
 network 10.0.23.0 0.0.0.3 area 0
ip rip use use
ip rip network 10.0.12.0/30
syslog host 192.168.1.100
save
```

#### Catalyst 設定例

```
conf t
hostname Cat-SW1
interface GigabitEthernet1/0/1
 ip address 10.0.23.2 255.255.255.252
 no shutdown
router ospf 1
 network 10.0.23.0 0.0.0.3 area 0
router bgp 65001
 neighbor 10.0.34.2 remote-as 65002
end
write memory
```

#### NX-OS 設定例

```
feature ospf
feature bgp
feature vpc
conf t
hostname Nexus-A
interface Ethernet1/1
 ip address 10.0.34.2/30
 no shutdown
router ospf 1
 network 10.0.34.0 0.0.0.3 area 0
router bgp 65002
 neighbor 10.0.34.1 remote-as 65001
end
copy running-config startup-config
```

### 確認コマンド例

```
# ルーティングテーブル確認
show ip route

# OSPFネイバー確認
show ip ospf neighbor

# BGPセッション確認
show ip bgp summary

# syslogバッファ確認
show logging            # Catalyst / NX-OS
show logging syslog     # Si-R

# vPC状態確認（NX-OS）
show vpc
show vpc brief
```

---

## ファイル構成

```
network-lab-emulator/
├── app.py                  # FastAPI サーバー（メインエントリー）
├── engine/
│   ├── protocols.py        # プロトコルエンジン（RIP/OSPF/BGP/STP/vPC等）
│   ├── rules.py            # CLIルールエンジン（コマンド処理・補完）
│   └── syslog_sender.py    # syslog / SNMP / NTP 送信
├── static/
│   ├── index.html          # メインUI
│   ├── lab_rip.html        # RIPラボ
│   ├── lab_ospf.html       # OSPFラボ
│   └── lab_bgp.html        # BGPラボ
├── tests/
│   ├── test_protocols.py   # プロトコルテスト
│   └── test_device_os.py   # 機種別CLIテスト
├── verify_all.py           # 全機能確認スクリプト（103項目）
├── lab_multivendor.py      # マルチベンダーラボ検証
├── demo_rip.py             # RIPデモスクリプト
├── demo_ospf.py            # OSPFデモスクリプト
├── demo_bgp.py             # BGPデモスクリプト
├── requirements.txt        # 依存パッケージ
├── start.bat               # Windows用起動スクリプト
└── .env.example            # 環境変数サンプル
```

---

## テスト実行

```bash
# 全機能確認（103項目）
python verify_all.py

# 項目別確認
python verify_all.py rip      # RIP
python verify_all.py ospf     # OSPF
python verify_all.py bgp      # BGP
python verify_all.py nxos     # NX-OS / vPC
python verify_all.py cli      # CLI補完・?ヘルプ
python verify_all.py vendor_log  # ベンダーログ形式

# マルチベンダーラボ検証
python lab_multivendor.py

# pytest
pytest tests/
```

---

## ログ形式（実機準拠）

| 機種 | 形式 |
|------|------|
| **Catalyst** | `*Jun 20 12:34:56.789: %OSPF-5-ADJCHG: Process 1, Nbr 10.0.0.2 from LOADING to FULL` |
| **NX-OS** | `2026 Jun 20 12:34:56.789 Nexus-A %ETH_PORT-5-IF_UP: Interface Ethernet1/1 is up` |
| **Si-R** | `2026/06/20 12:34:56 Router-A Si-R G120 : [OSPF] neighbor 3.3.3.3 on lan0 state: Full` |
| **APRESIA** | `2026/06/20 12:34:56: informational: Port 1/0/1 Link Up (1G full-duplex)` |

---

## syslogサーバ設定

各機種で以下のコマンドを実行すると、UDP 514 でsyslogサーバに転送されます。

```
# Catalyst
logging host 192.168.1.100
logging trap informational

# NX-OS
logging server 192.168.1.100 6

# Si-R
syslog host 192.168.1.100

# APRESIA
config syslog 192.168.1.100
```

---

## ライセンス

MIT License — 自由に使用・改変・再配布できます。

---

## 謝辞・参考

- [富士通 Si-R Gシリーズ コマンドリファレンス](https://www.fsastech.com/ja-jp/products/network/router/manual/sir-g/)
- [Cisco IOS-XE Configuration Guide](https://www.cisco.com/c/en/us/support/ios-nx-os-software/ios-xe-17/series.html)
- [Cisco NX-OS Configuration Guide](https://www.cisco.com/c/en/us/support/switches/nexus-9000-series-switches/series.html)
- [APRESIA ApresiaLight ユーザーガイド](https://www.apresia.jp/)
