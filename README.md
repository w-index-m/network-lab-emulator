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

- Cisco / Nexus / Catalyst / APRESIA / F5 BIG-IP / Si-R / SR-S を含むマルチベンダー対応
- CLIベースの操作と設定流し込み
- OSPF / BGP / RIP / VRRP / HSRP / STP / LACP / VLAN などの実習・検証用途向け挙動
- topology editor による接続構成の作成
- SNMP / Syslog / Prometheus / Alertmanager 連携を含む運用監視の検証
- 実機連携用のツール群

---

## コンセプト: Network Infrastructure Digital Twin + AI Observability

このプロジェクトは、一般的な「Digital Twin」（物理設備をセンサーデータで仮想空間に再現する）のネットワーク版と捉えられます。

| Digital Twinの要素 | このリポジトリでの実装 |
|---|---|
| 物理資産を仮想空間に再現 | Catalyst / Cisco / SR-S / ASA / Nexus / BigIP / APRESIA / Si-R をコア製品としてプロトコルエンジンでエミュレート |
| 仮想モデルからリアルなテレメトリを出力 | 仮想SNMPエージェント（MIB-II）、実UDPでのsyslog / SNMP trap送信（`engine/syslog_sender.py`） |
| 監視・観測レイヤー | [SNMPモニタリングダッシュボード](./docs/snmp-dashboard.md)、[Syslog AIモニター](./docs/syslog-ai-monitor.md)（Ollamaによる要約 + ルール評価） |
| 実世界との橋渡し（ツール群） | netmiko/paramikoによる実機連携、`route_injector`（経路負荷試験）、`bigip_qkview_collector`（実機ログ採取）等 |

「AIがインフラを仮想化している」のではなく、仮想化されたネットワークインフラをAIが観測・要約している、という構図です。  
詳細は [`docs/feature-inventory.md`](./docs/feature-inventory.md) を参照してください。

---

## 対応機種

| 機種 | コマンド体系 | 主な実装機能 |
|------|------------|------------|
| **Cisco Catalyst** | IOS-XE 17.x準拠 | OSPF / BGP / HSRP / STP / EtherChannel |
| **Cisco Nexus** | NX-OS 10.2準拠 | OSPF / BGP / vPC / VRRP / LACP |
| **Cisco ASA** | ASA 9.x準拠 | ファイアウォール / NAT / ACL |
| **APRESIA** | ApresiaLight準拠 | VLAN / STP / LACP |
| **F5 BIG-IP** | TMOS / tmsh準拠 | LTM / Pool / Virtual Server ✨ |
| **富士通 SR-S** | SR-Sシリーズ準拠 | VLAN / LACP / STP |
| **富士通 Si-R** | Si-R Gシリーズ準拠 | RIP / OSPF / VRRP / BGP |
| **Generic Router / Switch** | 汎用CLI準拠 | 軽量なラボ構成、接続検証 |
| **PC / Host** | ホストコマンド互換 | IP設定・デフォルトゲートウェイ確認 |

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

- [README.md](./README.md)
- [IMPLEMENTATION_PROGRESS.md](./IMPLEMENTATION_PROGRESS.md)
- [docs/feature-inventory.md](./docs/feature-inventory.md)
- [docs/implementation-roadmap.md](./docs/implementation-roadmap.md)
- [docs/implementation-status.md](./docs/implementation-status.md)
- [docs/bigip-ltm-usage.md](./docs/bigip-ltm-usage.md)
- [docs/bigip-qkview.md](./docs/bigip-qkview.md)
- [docs/snmp-dashboard.md](./docs/snmp-dashboard.md)
- [docs/syslog-ai-monitor.md](./docs/syslog-ai-monitor.md)
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

