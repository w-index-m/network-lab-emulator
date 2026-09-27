# Network Lab Emulator

マルチベンダー対応のネットワーク機器CLIエミュレーター。  
ブラウザ上で実機に近いCLI操作・ルーティングプロトコルのシミュレーションができます。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

> 📊 **[実装進捗ダッシュボード](./IMPLEMENTATION_PROGRESS.md)** ← チーム共有用の最新実装状況  
> ✅ Priority 1 の 50% 完了 | BGP Community ✨ | Big-IP LTM テストツール ✨

---

## コンセプト: Network Infrastructure Digital Twin + AI Observability

このプロジェクトは、一般的な「Digital Twin」（物理設備をセンサーデータで
仮想空間に再現する）のネットワーク版と捉えられます。

| Digital Twinの要素 | このリポジトリでの実装 |
|---|---|
| 物理資産を仮想空間に再現 | Catalyst / Cisco / SR-S / ASA / Nexus / BigIP / APRESIA / Si-R を**コア製品**としてプロトコルエンジンでエミュレート |
| 仮想モデルからリアルなテレメトリを出力 | 仮想SNMPエージェント（MIB-II）、実UDPでのsyslog / SNMP trap送信（`engine/syslog_sender.py`） |
| 監視・観測レイヤー | [SNMPモニタリングダッシュボード](./docs/snmp-dashboard.md)、[Syslog AIモニター](./docs/syslog-ai-monitor.md)（Ollamaによる要約 + ルール生成） |
| 実世界との橋渡し（**ツール群**） | netmiko/paramikoによる実機連携、`route_injector`（経路負荷試験）、`bigip_qkview_collector`（実機ログ採取）等 |

「AIがインフラを仮想化している」のではなく、**仮想化されたネットワークインフラを
AIが観測・要約している**、という構図（Network Infrastructure Digital Twin +
AI Observability）。コア製品とツールの区分は
[`docs/feature-inventory.md`](./docs/feature-inventory.md) を参照。

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

---
