# Network Lab Emulator

マルチベンダー対応のネットワーク機器CLIエミュレーター。  
ブラウザ上で、実機に近いCLI操作・ルーティングプロトコルのシミュレーション・観測/監視の検証ができます。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> 📊 **[実装進捗ダッシュボード](./IMPLEMENTATION_PROGRESS.md)**  
> ✅ Priority 1 の進捗をまとめて確認できます。

---

## 概要

このプロジェクトは、ネットワーク機器のCLI・設定・プロトコル動作をブラウザ上で再現するためのエミュレーション基盤です。

- Cisco / Nexus / Catalyst / ASA / Arista / Juniper / Yamaha RTX / APRESIA / FortiGate / F5 BIG-IP / AWS ALB / Si-R / SR-S / BAS(擬似FLET'S) を含むマルチベンダー対応
- CLIベースの操作と設定流し込み（一部機種はaws-cli/tmsh/setコマンド等、実機の構文そのまま）
- OSPF / BGP(Route Reflector・RTBH・Confederation・Graceful Restart/LLGR・RPKI等含む) / RIP / VRRP(unicast-peer含む) / HSRP / STP / LACP / VLAN / IPv6ルーティング / IPsec / PPPoE / v6プラス(MAP-E) などの実習・検証用途向け挙動
- F5 BIG-IP / AWS ALBは実ヘルスチェック + 実TCP/HTTPトラフィック分散（round-robin / least-connections / パーシステンス / SNAT）まで実装
- topology editor による接続構成の作成
- SNMP / Syslog / Prometheus / Grafana / Loki / Alertmanager 連携を含む運用監視の検証
- Strix(AIペンテストエージェント)・ZGrab2・k6などセキュリティ/負荷試験ツールとの連携
- 実機連携用のツール群（netmiko / Ansible / ncclient / pygnmi 等）

---

## コンセプト: Network Infrastructure Digital Twin + AI Observability

このプロジェクトは、一般的な「Digital Twin」（物理設備をセンサーデータで仮想空間に再現する）のネットワーク版と捉えられます。

| Digital Twinの要素 | このリポジトリでの実装 |
|---|---|
| 物理資産を仮想空間に再現 | Catalyst / Cisco / SR-S / ASA / Nexus / Arista / Juniper / Yamaha RTX / APRESIA / FortiGate / BigIP / AWS ALB / Si-R / BAS をコア製品としてプロトコルエンジンでエミュレート |
| 仮想モデルからリアルなテレメトリを出力 | 仮想SNMPエージェント（MIB-II）、実UDPでのsyslog / SNMP trap送信（`engine/syslog_sender.py`） |
| 監視・観測レイヤー | [SNMPモニタリングダッシュボード](./docs/snmp-dashboard.md)、[Syslog AIモニター](./docs/syslog-ai-monitor.md)（Ollamaによる要約 + ルール評価） |
| 実世界との橋渡し（ツール群） | netmiko/paramikoによる実機連携、`route_injector`（経路負荷試験）、`bigip_qkview_collector`（実機ログ採取）等 |

「AIがインフラを仮想化している」のではなく、仮想化されたネットワークインフラをAIが観測・要約している、という構図です。  
詳細は [`docs/feature-inventory.md`](./docs/feature-inventory.md) を参照してください。

---

## 対応機種

| 機種 | コマンド体系 | 主な実装機能 |
|------|------------|------------|
| **Cisco Catalyst** | IOS-XE 17.x準拠 | OSPF / BGP / HSRP / STP / EtherChannel / ZTP(AUTOINSTALL) |
| **Cisco Nexus** | NX-OS 10.2準拠 | OSPF / BGP / vPC / VRRP / LACP / TACACS+・AAA |
| **Cisco ASA** | ASA 9.x準拠 | ファイアウォール / NAT / ACL / IPsec / PPPoE WAN / threat-detection scanning-threat |
| **Arista** | EOS準拠 | 共有Ciscoツリーに相乗り、netmiko/Ansible(eAPI JSON)対応 |
| **Juniper** | Junos準拠(set/delete/commit) | 2段階candidate/active config、NETCONF/SSH実リスナー |
| **Yamaha RTX(RTX1300)** | RTX準拠 | ルーティング実反映、PPPoEクライアント、v6プラス(MAP-E)、NAT/PPPoE設定反映 |
| **APRESIA** | ApresiaLight準拠 | VLAN / STP / LACP |
| **FortiGate** | FortiOS準拠(config/edit/set/next/end) | PPPoE WAN、IPsec(IKE/IPsec) |
| **F5 BIG-IP** | TMOS / tmsh準拠 | LTM(Pool/Virtual/Monitor) ✨ **実TCPヘルスチェック + 実トラフィック分散**(round-robin/least-connections)、パーシステンス(source-addr)、SNAT(automap/snatpool)、net vlan/net self(モックSelf IP) |
| **AWS ALB** | aws-cli(elbv2)準拠 | **実HTTPヘルスチェック + 実L7トラフィック分散**(path/host-basedルーティング)、cookieスティッキーセッション |
| **富士通 SR-S** | SR-Sシリーズ準拠 | VLAN / LACP / STP |
| **富士通 Si-R** | Si-R Gシリーズ準拠 | RIP / OSPF / VRRP / BGP / IPsec |
| **BAS(擬似FLET'S網)** | 独自(収容局モデル) | PPPoE/v6プラス(MAP-E)ルールサーバー役 |
| **Generic Router / Switch** | 汎用CLI準拠 | 軽量なラボ構成、接続検証 |
| **PC / Host** | ホストコマンド互換 | IP設定・デフォルトゲートウェイ確認、実`http.server` |

BGPはRoute Reflector(cluster-id/route-reflector-client)・RTBH・Confederation・
Hostname Capability・Graceful Restart/LLGR・RPKIにも対応。IPv6アドレッシング/
スタティックルーティング/ping・tracerouteも実装済み（詳細は`docs/ipv6-routing.md`）。

---

## 主要機能

- CLIエミュレーション
  - 実機に近い対話型CLIインターフェース
  - `configure`, `interface`, `router ospf`, `show ip route` などのコマンド処理
- トポロジ編集
  - ブラウザ上のドラッグ&ドロップで装置とリンクを配置
  - 接続の編集・削除・VLAN管理
- プロトコルエミュレーション
  - RIP / OSPF / BGP / VRRP / HSRP / STP / LACP
- 監視・運用検証
  - SNMP trap を受信して metrics 化
  - Prometheus + Alertmanager によるアラート検証
- 実機連携
  - netmiko / paramiko を使った実機設定投入
  - route_injector / qkview / UCS 収集ツール

---

## 起動方法

### 1. Python依存を入れる

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. アプリ起動

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

ブラウザで以下にアクセスします。

```text
http://localhost:8000
```

### 3. ランチャーで装置選択

- 起動時のランチャーで装置を選択
- 接続を追加して、CLIターミナルを起動
- トポロジ編集から、装置間リンクを作成

---

## 代表的な利用例

### CLIを使った検証

```bash
show ip route
show ip ospf neighbor
show run
```

### トポロジでラボ作成

- スイッチ・ルータ・PCを組み合わせてネットワーク設計を検証
- VLAN / LACP / STP / OSPF の動作確認
- マルチベンダー混在構成の構築

### 実機連携

```bash
python tools/eveng_deploy.py export --api http://127.0.0.1:8099 --out ./out --platform c3650
```

実機 Catalyst 3650 などへの設定展開・検証が可能です。

---

## ドキュメント一覧

`docs/`配下には機能ごとに実機ライブ検証のトランスクリプト付きドキュメントが
多数あります（2026年時点で90件超）。全件は`docs/`ディレクトリを参照してください。
ここでは代表的なものをカテゴリ別に挙げます。

**全体像・進捗**
- [README.md](./README.md)
- [IMPLEMENTATION_PROGRESS.md](./IMPLEMENTATION_PROGRESS.md)
- [docs/feature-inventory.md](./docs/feature-inventory.md)
- [docs/implementation-roadmap.md](./docs/implementation-roadmap.md)
- [docs/implementation-status.md](./docs/implementation-status.md)
- [docs/multivendor_multiprotocol_test_report.md](./docs/multivendor_multiprotocol_test_report.md)

**新規デバイスタイプ・プロトコル**
- [docs/pppoe-ipsec-mesh-status.md](./docs/pppoe-ipsec-mesh-status.md)（Arista/Juniper/Yamaha/BAS/FortiGate/ASAのPPPoE+IPsec対応状況マトリクス）
- [docs/ipoe-ipsec-verified.md](./docs/ipoe-ipsec-verified.md) / [docs/ipsec-cisco-cisco-verified.md](./docs/ipsec-cisco-cisco-verified.md)
- [docs/ipv6-routing.md](./docs/ipv6-routing.md)（IPv6アドレッシング/スタティックルート/ping・traceroute）
- [docs/bgp-community.md](./docs/bgp-community.md)（Route Reflector/RTBH/Confederation/Graceful Restart/LLGR/RPKI含む）
- [docs/vrrp-hsrp.md](./docs/vrrp-hsrp.md)（VRRP unicast-peer含む）

**ロードバランサー(F5 BIG-IP / AWS ALB)**
- [docs/bigip-load-balancing.md](./docs/bigip-load-balancing.md)（実ヘルスチェック・実トラフィック分散・SNAT・パーシステンス・モックSelf IP）
- [docs/aws-alb-load-balancer.md](./docs/aws-alb-load-balancer.md)（実HTTPヘルスチェック・path/host-basedルーティング・cookieスティッキー）
- [docs/load-balancer-testing-quickstart.md](./docs/load-balancer-testing-quickstart.md)（`tools/virtual_client.py`を使った再現クイックスタート）
- [docs/k6-load-testing.md](./docs/k6-load-testing.md)（k6によるQPS/レイテンシ負荷試験）
- [docs/bigip-ltm-usage.md](./docs/bigip-ltm-usage.md) / [docs/bigip-qkview.md](./docs/bigip-qkview.md)

**監視・観測**
- [docs/snmp-dashboard.md](./docs/snmp-dashboard.md)
- [docs/syslog-ai-monitor.md](./docs/syslog-ai-monitor.md)
- [docs/syslog-anomaly-detection.md](./docs/syslog-anomaly-detection.md)
- [docs/anomaly-autoencoder.md](./docs/anomaly-autoencoder.md)（NumPy/torch両実装）
- [docs/gnmi-telemetry.md](./docs/gnmi-telemetry.md)

**セキュリティ・AI連携**
- [docs/strix-pentest-integration.md](./docs/strix-pentest-integration.md)（AIペンテストエージェントのGitHub Actions/操作パネル連携）
- [docs/zgrab2-scan-detection.md](./docs/zgrab2-scan-detection.md)
- [docs/asa-threat-detection-scanning.md](./docs/asa-threat-detection-scanning.md)
- [docs/llm-ansible-playbook-generation.md](./docs/llm-ansible-playbook-generation.md)（LLM→Ansible Playbook生成）
- [docs/unusual-verifications-gnmi-cps-automap.md](./docs/unusual-verifications-gnmi-cps-automap.md)（gNMI over BIG-IP / CPS試験 / automap SNATの実バグ発見）

**実機連携**
- [docs/netmiko-testing-guide.md](./docs/netmiko-testing-guide.md)
- [docs/pyats-integration.md](./docs/pyats-integration.md)
- [examples/c3650/README.md](./examples/c3650/README.md)
- [tools/monitoring_stack/README.md](./tools/monitoring_stack/README.md)

---

## 実装進捗

詳細は [IMPLEMENTATION_PROGRESS.md](./IMPLEMENTATION_PROGRESS.md) を参照。  
現時点では、BGP Community や Big-IP LTM テストツールなどの機能追加が続いています。

---

## 例: Catalyst 3650 実機連携

`examples/c3650/README.md` には、3台の Catalyst 3650 を使った STP / OSPF 検証ガイドがあります。

```bash
python tools/eveng_deploy.py deploy --inventory ./c3650_out/inventory.json
python tools/eveng_deploy.py verify --inventory ./c3650_out/inventory.json \
    --checks examples/c3650/checks.stp-ospf.json
```

---

## 監視スタックのサンプル

`tools/monitoring_stack/README.md` には、SNMP trap → Prometheus → Alertmanager のパイプライン検証手順が含まれています。

```bash
# 例: SNMP trap受信器起動
python tools/snmp_trap_receiver.py --trap-port 1162 --metrics-port 9162
```

---

## 主要ディレクトリ

```text
.
├── app.py
├── engine/
├── docs/
├── examples/
├── tools/
├── tests/
├── static/
├── requirements.txt
├── README.md
├── IMPLEMENTATION_PROGRESS.md
└── ...
```

---

## 使い分けの目安

- 「CLIを使ってルーティングを勉強したい」→ ブラウザのエミュレータでラボ作成
- 「実機と同じ設定を試したい」→ netmiko / RESTCONF / export deploy フロー
- 「監視とアラートを確認したい」→ Prometheus + Alertmanager のサンプル
- 「機能追加の進捗を見たい」→ `IMPLEMENTATION_PROGRESS.md`

---

## ライセンス

MIT License

---

## まとめ

Network Lab Emulator は、ネットワーク学習・検証・運用演習・デモ環境の構築を簡単にするための、CLIベースのマルチベンダー ネットワークエミュレータです。

実験環境の構築、プロトコル挙動の理解、運用監視の検証、実機との連携まで、1つのリポジトリで扱えるようにしています。

