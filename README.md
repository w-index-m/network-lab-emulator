# Network Lab Emulator

**日本語** | [English](./README.en.md)

マルチベンダー対応のネットワーク機器エミュレータ。
ブラウザ上で実機に近いCLI操作ができ、**一部のプロトコルは本物のワイヤプロトコルとして動作する**ため、
ncclient / gNMIクライアント / SNMPツールといった実在のクライアントから接続できます。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![Tests](https://img.shields.io/badge/tests-1038%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## このプロジェクトの特徴

### 「表示だけ」ではなく、実際に喋るプロトコルがある

CLIエミュレータの多くは `show` の出力文字列を返すだけですが、このプロジェクトは
**6つのプロトコルを実ソケット・実パケットで実装**しています。

| 実装 | 中身 | 外部クライアントからの接続 |
|---|---|---|
| `engine/real_ospf_agent.py` | scapy / raw IP proto 89 | 他のOSPF実装と隣接を張れる |
| `engine/real_bgp_agent.py` | TCP 179 | 本物のBGPスピーカーとセッション確立 |
| `engine/real_rip_agent.py` | UDP 520 | RIPv2パケットの送受信 |
| `engine/snmp_udp_agent.py` | UDP 161 | `snmpwalk` 等で実際にポーリング可能 |
| `engine/netconf_agent.py` | paramiko SSH / TCP 830 | **ncclient** から `get-config` / `edit-config` |
| `engine/gnmi_agent.py` | gRPC / TCP 50052 | **gnmic / pygnmi** から Get / Set / Subscribe |

gNMIは openconfig/gnmi の **`gnmi.proto` 原本**をコンパイルして使っています（自作の擬似protoではありません）。
NETCONF・RESTCONF・gNMIは同じデータモデルを共有しているため、
**gNMIで書いた設定がCLIの `show running-config` にそのまま出ます。**

### 障害時の「再収束」まで検証している

収束状態が正しくても、再収束が正しいとは限りません。実際、
OSPFでは「隣接が落ちず経路も撤回されない」不具合が10件、
EIGRPでは「2台構成で設定した瞬間に無限再帰でクラッシュ」する不具合が見つかっています。

主回線をshutdownしてフローティングスタティック（AD 210）へ切り替わるか、
復旧したら戻るか——を各プロトコルで固定しています。
詳細は [`docs/ospf-failover-floating-static.md`](./docs/ospf-failover-floating-static.md)。

```
【平常時】 O  172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1
【障害時】 S  172.31.2.2/32 [210/0]  via 10.90.2.2, GigabitEthernet1/0/2   ← 切替
【復旧後】 O  172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1   ← 復帰
```

### コンセプト: Network Infrastructure Digital Twin + AI Observability

| Digital Twinの要素 | このリポジトリでの実装 |
|---|---|
| 物理資産を仮想空間に再現 | Catalyst / Cisco / Si-R / SR-S / ASA / Nexus / APRESIA / BIG-IP をプロトコルエンジンでエミュレート |
| 仮想モデルからリアルなテレメトリを出力 | 実SNMPエージェント（MIB-II）、実UDPでのsyslog / SNMP trap送信 |
| 監視・観測レイヤー | [SNMPダッシュボード](./docs/snmp-dashboard.md)、[Syslog AIモニター](./docs/syslog-ai-monitor.md)、Prometheus / Grafana連携 |
| 実世界との橋渡し | netmiko / paramiko による実機連携、経路負荷試験、実機ログ採取ツール群 |

---

## 対応機種

| 機種 | `device_type` | コマンド体系 | 主な実装 |
|------|---|------------|------------|
| **Cisco Catalyst 9300** | `catalyst` | IOS-XE 17.x | OSPF / BGP / EIGRP / HSRP / STP / EtherChannel / MPLS / ZBFW / NETCONF / RESTCONF / gNMI |
| **Cisco Nexus 9300** | `nexus` | NX-OS 10.2 | OSPF / BGP / vPC / VRRP / LACP / MPLS |
| **Cisco IOS ルータ** | `cisco` | IOS 15.x | RIP / OSPF / BGP / NAT / IPsec |
| **Cisco ASA** | `asa` | ASA 9.x | ファイアウォール / NAT / ACL |
| **富士通 Si-R G120/G210** | `sir` | Si-R Gシリーズ | RIP / OSPF / BGP / VRRP / STP / IPsec VPN |
| **富士通 SR-S324TR1** | `srs` | SR-Sシリーズ | VLAN / LACP / STP |
| **APRESIA ApresiaLight GM200** | `apresia` | ApresiaLight | VLAN / STP / LACP（L2スイッチのためL3機能は非対応） |
| **F5 BIG-IP** | `bigip` | TMOS / tmsh | LTM / Pool / Virtual Server |
| **PC（Linuxホスト）** | `pc` | bash風 | ifconfig / ip / ping / traceroute / curl |

---

## 実装済み機能

### ルーティング / スイッチング
- **RIP v2 / OSPF / BGP / EIGRP** — 隣接確立・経路学習・再収束・認証（MD5）・経路フィルタ・ECMP
- **スタティックルート** — AD値比較・フローティングスタティック・マルチプロトコル経路選択
- **VRRP / HSRP / GLBP** — Master/Backup遷移・preempt・オブジェクトトラッキング
- **STP / Rapid-PVST+** — Root Bridge選出・PortFast・BPDU Guard
- **LACP / EtherChannel** — バンドル・min-links・並列リンク
- **vPC (NX-OS)** / **MPLS (LDP)** / **NHRP / WCCP**

### 管理・プログラマビリティ
- **NETCONF** (TCP 830) — ncclientから接続可能 → [`docs/netconf-catalyst.md`](./docs/netconf-catalyst.md)
- **RESTCONF** — ietf-interfaces
- **gNMI** (gRPC) — Capabilities / Get / Set / Subscribe → [`docs/gnmi-telemetry.md`](./docs/gnmi-telemetry.md)
- **モデル駆動型テレメトリ (MDT)** — `telemetry ietf subscription`
- **モデルベースAAA (NACM, RFC 8341)** → [`docs/model-based-aaa-nacm.md`](./docs/model-based-aaa-nacm.md)
- **サービスレベルACL** — NETCONF/RESTCONFへの着信を送信元で制限
- **ISMU** — データモデル更新パッケージ（`.dmp.bin`）
- **EEM** — applet から実際に装置の設定を変更できる
- **App Hosting / OpenFlow** → [`docs/eem-apphosting-openflow.md`](./docs/eem-apphosting-openflow.md)

### セキュリティ / サービス
- **ZBFW** — ゾーンベースファイアウォール
- **IPsec VPN** — IKE / DPD（Si-R ↔ Cisco 相互接続）
- **NAT / NAPT / ACL / DHCP / DHCPv6 / IPv6 ND**
- **Auto-QoS / QoS ポリシー**

### CLI
- Tab補完・`?` ヘルプ・短縮コマンド（`sh ip os ne`）・複数行一括投入
- ベンダー別エラーメッセージ（`% Invalid input detected at '^' marker.` 等）

### ログ・監視
- 装置内ログ（`show logging`）・syslog転送（UDP 514）・SNMP trap・NTP
- Prometheus exporter → Grafana ダッシュボード → [`docs/monitoring-stack-guide.md`](./docs/monitoring-stack-guide.md)

---

## セットアップ

```bash
git clone https://github.com/w-index-m/network-lab-emulator.git
cd network-lab-emulator
pip install -r requirements.txt
python app.py
```

ブラウザで http://localhost:8000 を開きます。Windows は `start.bat` をダブルクリック。

- Python 3.10 以上 / RAM 1GB 以上 / Windows・macOS・Linux
- 既定のログインは `admin` / `admin`（`NETLAB_AUTH_USER` / `NETLAB_AUTH_PASS` で変更、
  `NETLAB_AUTH_DISABLE=1` で無効化）
- **実プロトコルリスナー**（OSPF raw socket / TCP 830 / UDP 161 等）を使うには
  管理者権限が必要です。無くてもCLIエミュレーションは動作します
- gNMIを使う場合は `grpcio` / `grpcio-tools` が必要（未導入なら gNMI 機能だけ無効化されます）

---

## 使い方

### ラボ構成例（マルチベンダー接続）

```
APRESIA ─── Si-R G120 ─── Catalyst 9300 ─── Nexus 9300
10.0.12.0/30  10.0.23.0/30       10.0.34.0/30
     RIPv2          OSPF Area 0          eBGP
                              AS65001 ↔ AS65002
```

<details>
<summary>Si-R 設定例</summary>

```
configure
hostname Router-A
lan 0 ip address 10.0.23.1/30
ospf use on
ospf area 0.0.0.0
lan 0 ip ospf use on
syslog host 192.168.1.100
save
```
</details>

<details>
<summary>Catalyst 設定例</summary>

```
conf t
hostname Cat-SW1
interface GigabitEthernet1/0/1
 no switchport
 ip address 10.0.23.2 255.255.255.252
 no shutdown
router ospf 1
 network 10.0.23.0 0.0.0.3 area 0
router bgp 65001
 neighbor 10.0.34.2 remote-as 65002
end
write memory
```
</details>

<details>
<summary>NX-OS 設定例</summary>

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
</details>

### 確認コマンド例

```
show ip route                 # ルーティングテーブル（AD/メトリック付き）
show ip route 172.31.2.2      # 特定経路の詳細（採用理由が分かる）
show ip ospf neighbor
show ip bgp summary
show etherchannel 1 detail
show vpc                      # NX-OS
show logging / show logging syslog   # Catalyst・NX-OS / Si-R
```

### HTTP API

```bash
# 装置を作る
curl -X POST localhost:8000/api/device \
  -H 'Content-Type: application/json' \
  -d '{"id":"sw1","type":"catalyst","hostname":"SW1"}'

# CLIコマンドを流す（レスポンスキーは "output"）
curl -X POST localhost:8000/api/cli \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"sw1","command":"show ip route"}'

# 装置間をリンクする（パラメータは iface_a / iface_b）
curl -X POST localhost:8000/api/link \
  -H 'Content-Type: application/json' \
  -d '{"a":"sw1","b":"sw2","iface_a":"GigabitEthernet1/0/1","iface_b":"GigabitEthernet1/0/1"}'
```

---

## テスト

```bash
pytest tests/           # 全体（1038件、約9分）
pytest tests/test_ospf_failover.py -v     # OSPF障害切替
pytest tests/test_gnmi.py -v              # gNMI
python verify_all.py                      # 全機能確認スクリプト
```

**現状: 1038 passed / 5 skipped / 0 failed**（テストファイル79本）

カバレッジ: `app.py` 51% / `engine/protocols.py` 70% / `engine/rules.py` 53%

---

## ドキュメント

`docs/` に75本あります。まず読むとよいもの:

| ドキュメント | 内容 |
|---|---|
| [`architecture-pitfalls.md`](./docs/architecture-pitfalls.md) | **実装前に必読。** 繰り返し踏んでいる構造的な落とし穴 |
| [`ospf-failover-floating-static.md`](./docs/ospf-failover-floating-static.md) | 障害切替の検証記録と、そこで見つけた不具合10件 |
| [`netconf-catalyst.md`](./docs/netconf-catalyst.md) | NETCONF実装とncclientからの実行結果 |
| [`gnmi-telemetry.md`](./docs/gnmi-telemetry.md) | gNMI / モデル駆動型テレメトリ |
| [`monitoring-stack-guide.md`](./docs/monitoring-stack-guide.md) | Prometheus / Grafana連携 |
| [`feature-inventory.md`](./docs/feature-inventory.md) | 機能一覧（コア製品とツールの区分） |

---

## ファイル構成

```
network-lab-emulator/
├── app.py                      # FastAPIサーバ（CLIディスパッチの起点）
├── engine/
│   ├── protocols.py            # プロトコルエンジン（RIP/OSPF/BGP/EIGRP/STP/vPC/MPLS…）
│   ├── rules.py                # CLIルールエンジン（ベンダー別応答・補完）
│   ├── real_{ospf,bgp,rip}_agent.py   # 実パケットのプロトコルリスナー
│   ├── netconf_agent.py        # NETCONFサーバ（SSH/830）
│   ├── gnmi_agent.py           # gNMIサーバ（gRPC/50052）
│   ├── snmp_udp_agent.py       # SNMPエージェント（UDP/161）
│   ├── programmability.py      # EEM / App Hosting / OpenFlow
│   └── syslog_sender.py        # syslog / SNMP trap / NTP
├── static/                     # WebUI
├── tests/                      # pytest（79ファイル）
├── tools/                      # 運用・検証ツール（20本超）
└── docs/                       # ドキュメント（75本）
```

---

## 注意事項

- **これは学習・検証用のエミュレータです。** 実機の完全な代替ではありません。
  各ドキュメントの「未対応」節に、実機との差を明記しています
- 既定のログイン情報は `admin` / `admin` です。閉じた環境以外で動かす場合は必ず変更してください
- `docs/reference/` にベンダー各社のマニュアルPDFが含まれています。
  再配布の可否は各社の利用条件に従ってください

---

## ライセンス

MIT License — 自由に使用・改変・再配布できます。

## 謝辞・参考

- [富士通 Si-R Gシリーズ コマンドリファレンス](https://www.fsastech.com/ja-jp/products/network/router/manual/sir-g/)
- [Cisco IOS-XE Configuration Guide](https://www.cisco.com/c/en/us/support/ios-nx-os-software/ios-xe-17/series.html)
- [Cisco NX-OS Configuration Guide](https://www.cisco.com/c/en/us/support/switches/nexus-9000-series-switches/series.html)
- [APRESIA ApresiaLight ユーザーガイド](https://www.apresia.jp/)
- [openconfig/gnmi](https://github.com/openconfig/gnmi) — gNMI protoの原本
