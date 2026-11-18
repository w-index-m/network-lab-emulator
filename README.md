# Network Lab Emulator

マルチベンダー対応のネットワーク機器CLIエミュレーター。  
ブラウザ上で、実機に近いCLI操作・ルーティングプロトコルのシミュレーション・観測/監視の検証ができます。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

<p align="center">
  <a href="docs/videos/cyber-view-demo-music.mp4"><img src="docs/videos/nirvana-hero.gif" alt="ニルヴァーナ / Nirvana — 3D network monitoring view" width="100%"></a><br>
  <b>ニルヴァーナ(<code>/cyber-view</code>)</b> — ネットワークを 3D で見る監視画面 / a 3D network monitoring view<br>
  <sub>通常運用 → リンク障害と迂回 → フラップ → 経路の変化(情)。クリックで音楽つきの動画 / normal → link failure and reroute → flapping → route change info. Click for the full video with music.</sub>
</p>

**ニルヴァーナ(Cyber View)**は、既存の監視(Prometheus・Grafana Loki・SNMP / TRAP・NetFlow / sFlow・MRTG・Cacti・Zabbix)の値を読み、ネットワークのトラフィックと障害を **3D** で見せ、変化を「情」「注」「警」で知らせる監視画面です。実機の監視にも使える単体版(`cyber-view-standalone/`)があります。
紹介: [docs/nirvana-introduction.md](docs/nirvana-introduction.md) / [PowerPoint](docs/nirvana-introduction.pptx)

<sub>キーワード: ネットワーク可視化 · ネットワーク監視 · トラフィック可視化 · 3D · SNMP · NetFlow · sFlow · Prometheus · Grafana · Loki · MRTG · Cacti · Zabbix · OSPF · ダークネット · network traffic visualization · network monitoring · 3D network view · NIRVANA-style</sub>

> 🇬🇧 **English version: [at the bottom of this page](#english)**
>
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
- **モックニルヴァーナ(`/cyber-view`)**: ネットワークを 3D で見る監視画面(NIRVANA改 風)。実機の監視環境向けのスタンドアロン版あり → [下の節](#モックニルヴァーナ--cyber-viewcyber-view)

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

## モックニルヴァーナ / Cyber View(`/cyber-view`)

> 🇯🇵 日本語 / 🇬🇧 English below

![ネットワーク全体(IP アドレス空間)](docs/images/cyber-view-v3-ok_24.png)

### 日本語

NICT の NIRVANA改 を参考に作った、ネットワークを 3D で見る画面です。アプリ内では「ニルヴァーナ」と呼んでいます。

**紹介資料**: [docs/nirvana-introduction.md](docs/nirvana-introduction.md)(PowerPoint 版: [docs/nirvana-introduction.pptx](docs/nirvana-introduction.pptx))。取り込めるデータ、ヴァルハラとの連携、「情」「注」「警」の出し方、4つの経路の見え方、流量と粒の動き、スキンをまとめています。
名前・ロゴは独自のもので、実在の製品のマークは使っていません。Prometheus は画面の上では「ヴァルハラ / VALHALLA」と表示します。

使い方は2通りあります。

- **ラボ版**: `python app.py` を起動して `http://localhost:8000/cyber-view` を開くと、エミュレータ内の装置がそのまま映ります。
- **スタンドアロン版(実機向け)**: `cyber-view-standalone/` を使います。既存の Prometheus(snmp_exporter)・Loki・Zabbix と、SNMP の直接取得から読み込みます。
  実機の Catalyst を登録する手順は [docs/real-catalyst-registration.md](docs/real-catalyst-registration.md) にあります。

| 画面 | できること |
|---|---|
| NETWORK | IP アドレス空間を /0 → /8 → /16 → /24 → 装置(/32)とクリックで掘り下げる。通信は三角形の粒、警報は六角形の「警」と検知元ごとの花びら(IDS / FW / SYSLOG / NET / VALHALLA / RULES) |
| ROUTER LINKS | リンクごとの流量・使用率・LINK DOWN。断の検知経路(STATE / SYSLOG / TRAP / POLL)をチップで表示。SNMP が応答しない装置は「NO RESPONSE 消失」 |
| OSPF | 隣接の状態と Dead タイマーの残り、エリア、宛先までの最短経路ツリー、SPF の再計算 |
| DARKNET | NICTER のエッセンス。使われていない帯(ダークネット)に届いた通信を、送信元からの放物線の矢で表示(TCP 青 / UDP 黄 / ICMP 赤)。スキャン・DDoS の跳ね返り・設定ミス・リフレクション攻撃の準備に分類し、日ごとの観測統計(1 IP あたりのパケット数)も出す |
| ATT&CK | NIRVANA改 の Organizations View 風。組織を青い輪、攻撃の進行度(ATT&CK の14段階)を中央の橙の円柱で。戦術 × 手法の表と時系列。複数拠点(ネクサス)なら拠点ごとの輪が並ぶ |
| RESPONSE | 警報から遮断・隔離。本物の CLI を装置に入れる(line vty の access-class / Null0 / 隣の装置の shutdown)、解除、自動対処、警報の前後の通信記録(pcap) |
| WAN | 回線チェック。フレッツ網の PPPoE / IPoE(MAP-E)/ IPsec の状態と、経路ごとの遅延・ロスの推移(夜間の PPPoE 混雑を再現、実機版は本物の ping) |
| L2 LOOP | スイッチの輪(STP で止まっていない)を1秒で検知。赤く脈打つ輪・嵐の粒・使用率 約90%・MAC フラップ・「警」+ LOOP の花びら。止め方は実機と同じ storm-control / BPDU guard / errdisable recovery / APRESIA loopdetect、RESPONSE の「ループ遮断」。止まると「情 LOOP CLEARED」と止めた理由 |
| WORLD | 地球儀。自社から AWS / Google Cloud の各リージョンへの流量(公開 IP 帯で判定)、Cloudflare Radar の世界の HTTP 流量・L7 攻撃の弧・異常、IIJ IHR の日本の AS の遅延・障害、MAWI(日米回線の本物の pcap)の再生。取れないときは理由を表示、`NETLAB_WORLD_SAMPLE=1` で見本 |
| WORLD(都市) | 都市 → 都市の本物の経路と遅延(RIPE Atlas の traceroute。例 ニューヨーク→ロンドン→…→モスクワ 119 ms)、攻撃元の都市(自分に来たダークネット・ログイン失敗・フローの送信元、SANS ISC DShield の報告件数)、Cloudflare Radar の L3(DDoS)攻撃の国。IP → 都市は DB-IP City Lite(都市単位の精度はおおむね5〜8割)。他人どうしの都市間の流量は公開されていないので出さない |
| AI ログ解析 | **AI** ボタン。syslog とニルヴァーナの検知を AI(Claude・OpenAI 互換・Ollama)に読ませ、要約・所見・対処の CLI を提案。パスワード等と IP を伏せてから送る。危険なコマンドは実行不可、実行は人が確認して押す(ラボ版)。AI が無ければルールで解析 |
| SYSTEM STATUS | ネットワーク / トラフィック / セキュリティ / syslog の4観点で 正常・注意・異常 |
| 情 / 注 | 経路の追加・削除・変更、CPU の急上昇・回復は「情」。CPU の高負荷が続く・フラップは「注」(しきい値は設定で変更可) |
| BOARD / EXPLORE / ALERTS | Grafana 相当の機能。ダッシュボード、PromQL / LogQL の問い合わせ、アラートルールと webhook。Grafana の JSON もそのまま取り込める |
| ヴァルハラ連携 | Prometheus の履歴から過去を 3D で再生、使用率の予測、Prometheus の警報を花びらで表示。ニルヴァーナの判定も `/metrics` で Prometheus に渡す |
| 見た目 | 背景は 標準 / 金魚鉢 / 富士山、BGM は ジャズ / レゲエ / アンビエント、DEMO(自動シナリオ+音楽)。**FX** ボタンでサイバー演出(六角グリッド・データの雨・警報中の火の粉・光の尾・走査線・グリッチ)。通信が多いときは同じ結果を「×N」にまとめ、プレートどうし・メニューと重ねない |

保存されるもの: 設定ファイル(`config.yaml`)と BOARD で作ったもの(`nirvana_board.json`)だけです。
SNMP で取った値・情・警報・ログはメモリだけに置き、長く残す履歴は Prometheus / Loki / Zabbix 側に任せます。

### English

A 3D network view inspired by NICT's NIRVANA-Kai, called "Nirvana" in this app. The name and logo are original, and no
real product marks are used. On screen, Prometheus is labelled "VALHALLA".

Introduction (Japanese): [docs/nirvana-introduction.md](docs/nirvana-introduction.md), PowerPoint: [docs/nirvana-introduction.pptx](docs/nirvana-introduction.pptx).

There are two ways to use it:

- **Lab edition**: run `python app.py` and open `http://localhost:8000/cyber-view`. The devices inside the emulator appear as they are.
- **Standalone edition (for real devices)**: use `cyber-view-standalone/`. It reads from your existing Prometheus (snmp_exporter),
  Loki and Zabbix, and can also poll devices directly over SNMP.
  How to register a real Catalyst: [docs/real-catalyst-registration.md](docs/real-catalyst-registration.md).

| View | What it does |
|---|---|
| NETWORK | Drill down the IP address space /0 → /8 → /16 → /24 → host (/32) by clicking. Traffic flows as triangle particles. Alerts appear as a hexagon "警" with one petal per detection source (IDS / FW / SYSLOG / NET / VALHALLA / RULES) |
| ROUTER LINKS | Per-link traffic, utilization and LINK DOWN. Chips show which evidence saw the outage (STATE / SYSLOG / TRAP / POLL). Devices that stop answering SNMP show "NO RESPONSE" |
| OSPF | Adjacency state with the remaining Dead timer, areas, the shortest-path tree to a destination, and SPF recalculations |
| DARKNET | The essence of NICTER: traffic reaching an unused address block (darknet) flies in as parabolic arrows from its sources (TCP blue / UDP yellow / ICMP red), classified as scan, DDoS backscatter, misconfiguration or reflection-attack preparation, with daily statistics (packets per IP) |
| ATT&CK | NIRVANA-Kai-style Organizations View: organizations as blue rings, attack progress through the 14 ATT&CK tactics as a central orange cylinder, a tactic × technique matrix and a timeline. With several sites (nexus), one ring per site |
| RESPONSE | Block or quarantine from an alert by pushing real CLI to devices (line vty access-class / Null0 / shut the neighbour's port), undo, automatic rules, and a capture (pcap) around the alert |
| WAN | Line check: PPPoE / IPoE (MAP-E) / IPsec state over FLET'S and per-path latency and loss history (evening PPPoE congestion is reproduced in the lab; the standalone edition uses real pings) |
| L2 LOOP | Detects a switch ring not blocked by STP within a second: a pulsing red ring, storm particles, ~90% utilization, MAC flaps, and a "警" with a LOOP petal. Stopped the way real gear does it (storm-control, BPDU guard, errdisable recovery, APRESIA loopdetect) or by the RESPONSE "loop cut"; then "LOOP CLEARED" with the reason |
| WORLD | A globe: traffic from your site to each AWS / Google Cloud region (matched against the published IP ranges), Cloudflare Radar's top HTTP countries, L7 attack arcs and anomalies, IIJ IHR delay/outage events for Japanese ASes, and replay of MAWI (real trans-Pacific pcap). Shows why a source is unavailable; `NETLAB_WORLD_SAMPLE=1` fills in samples |
| WORLD (cities) | Real city-to-city paths and latency (RIPE Atlas traceroutes, e.g. New York → London → … → Moscow, 119 ms), attack-source cities (darknet, failed logins and flows reaching your site; SANS ISC DShield report counts), and Cloudflare Radar L3 (DDoS) countries. IP → city via DB-IP City Lite (roughly 50–80% city accuracy). Third-party city-to-city traffic volume is not published anywhere, so it is not shown |
| AI log analysis | The **AI** button sends syslog and Nirvana's own detections to an AI (Claude, OpenAI-compatible, Ollama) for a summary, findings and proposed CLI fixes. Secrets and IPs are masked before sending; dangerous commands are never executable and a human confirms every fix (lab). Falls back to rules without an AI |
| SYSTEM STATUS | Normal / warning / critical for four areas: network, traffic, security and syslog |
| 情 (info) / 注 (notice) | Route add/delete/change and CPU jump/recovery appear as "info". Sustained high CPU and flapping appear as "notice". Thresholds are configurable |
| BOARD / EXPLORE / ALERTS | Grafana-like features: dashboards, PromQL / LogQL queries, alert rules with webhooks. Grafana dashboard JSON can be imported as is |
| VALHALLA integration | Replay past states in 3D from Prometheus history, forecast utilization, and show Prometheus alerts as petals. Nirvana's own judgements are exported to Prometheus at `/metrics` |
| Look and feel | Backgrounds: standard / goldfish bowl / Mt. Fuji. BGM: jazz / reggae / ambient. DEMO mode (scripted scenario with music). The **FX** button adds cyber effects (hex grid, data rain, embers during alerts, light trails, scanlines, glitches). Under heavy traffic, repeated results collapse into "×N" and plates never overlap each other or the menu |

What is saved: only the config file (`config.yaml`) and what you build in BOARD (`nirvana_board.json`). SNMP values, info
events, alerts and logs stay in memory; long-term history is left to Prometheus / Loki / Zabbix.

### 動画 / Videos

[![ニルヴァーナ DEMO / Nirvana DEMO](docs/videos/nirvana-hero.gif)](docs/videos/cyber-view-demo-music.mp4)

| DEMO(自動シナリオ / scripted scenario) | 流れのアニメと MAC フラップ / flow animation and a MAC flap |
|---|---|
| [![DEMO](docs/videos/cyber-view-demo.gif)](docs/videos/cyber-view-demo.mp4) | [![flow](docs/videos/cyber-view-flow-macflap.gif)](docs/videos/cyber-view-flow-macflap.mp4) |

| Catalyst の L2 ループ検知 / Catalyst L2 loop detection |
|---|
| [![L2 loop](docs/videos/cyber-view-l2-loop-catalyst.gif)](docs/videos/cyber-view-l2-loop-catalyst.mp4) |

- [Catalyst の L2 ループ検知(80秒・字幕つき / 80 s with captions)](docs/videos/cyber-view-l2-loop-catalyst.mp4): 輪ができる → 嵐 → storm-control で err-disable → errdisable recovery で再ループ・再停止 → RESPONSE のループ遮断 / ring forms → storm → storm-control err-disable → recovery, re-loop, re-stop → RESPONSE loop cut
- [DEMO(音楽つき / with music, 1分40秒)](docs/videos/cyber-view-demo-music.mp4)
- GIF をクリックすると mp4 が開きます / Click a GIF to open the mp4.

### 画像 / Screenshots

| | |
|---|---|
| ROUTER LINKS(正常 / normal)<br>![](docs/images/cyber-view-v3-ok_links.png) | LINK DOWN と4つの検知経路 / LINK DOWN with four evidence sources<br>![](docs/images/cyber-view-v4-linkdown-4sources.png) |
| OSPF(隣接と最短経路 / adjacencies and SPF tree)<br>![](docs/images/cyber-view-ospf-normal.png) | OSPF の Dead タイマーのカウントダウン / Dead timer countdown<br>![](docs/images/cyber-view-ospf-deadcount.png) |
| ポートスキャンの検知 / port scan detected<br>![](docs/images/cyber-view-v6-portscan.png) | SSH 総当たりとログインのブロック / SSH brute force and login block<br>![](docs/images/cyber-view-v7-ssh-brute.png) |
| DARKNET(NICTER のエッセンス / NICTER essence)<br>![](docs/images/cyber-view-darknet.png) | ポートスキャンの検知(ライブネット)/ port scan on the live network<br>![](docs/images/cyber-view-v6-portscan-host.png) |
| ATT&CK(拠点ごとの輪と進行度 / per-site rings and progress)<br>![](docs/images/cyber-view-attack.png) | RESPONSE(警報から遮断 / block from an alert)<br>![](docs/images/cyber-view-response.png) |
| WAN(フレッツ網の回線チェック / FLET'S line check)<br>![](docs/images/cyber-view-wan.png) | CPU の「情」/ CPU info<br>![](docs/images/cyber-view-cpu-info.png) |
| 経路の変化「情」/ route change info<br>![](docs/images/cyber-view-v8-route-info.png) | ダークネット観測 / darknet<br>![](docs/images/cyber-view-darknet.png) |
| フラップの警告 / flap notice<br>![](docs/images/cyber-view-v8-flap.png) | SNMP 応答なし / no SNMP response<br>![](docs/images/cyber-view-v5-no-response.png) |
| ヴァルハラから再生 / replay from VALHALLA<br>![](docs/images/cyber-view-valhalla-replay.png) | 使用率の予測 / utilization forecast<br>![](docs/images/cyber-view-valhalla-forecast.png) |
| BOARD(Grafana 相当 / Grafana-like)<br>![](docs/images/nirvana-board.png) | Grafana JSON の取り込み / Grafana JSON import<br>![](docs/images/nirvana-board-grafana-import.png) |
| EXPLORE<br>![](docs/images/nirvana-explore.png) | ALERTS<br>![](docs/images/nirvana-alerts.png) |
| 背景: 金魚鉢 / goldfish bowl<br>![](docs/images/cyber-view-bg-goldfish-ospf.png) | 背景: 富士山 / Mt. Fuji<br>![](docs/images/cyber-view-bg-fuji-ospf.png) |
| スタンドアロン版 + Prometheus / Loki<br>![](docs/images/standalone-prom-loki-fault.png) | スタンドアロン版 + Zabbix<br>![](docs/images/standalone-zabbix-fault.png) |
| スマホ / phone<br>![](docs/images/cyber-view-ospf-phone.png) | 装置ごとの表示 / host view<br>![](docs/images/cyber-view-host.png) |
| AI ログ解析 / AI log analysis<br>![](docs/images/cyber-view-ai-panel.jpg) | MRTG・Cacti で動く単体版 / standalone on MRTG + Cacti<br>![](docs/images/standalone-mrtg-cacti-links.jpg) |
| WORLD 都市 → 都市の経路 / city-to-city paths<br>![](docs/images/cyber-view-world-city-europe.jpg) | 攻撃元の都市 / attack-source cities<br>![](docs/images/cyber-view-world-city-asia.jpg) |
| WORLD(クラウド宛て・Radar・IHR)/ WORLD globe<br>![](docs/images/cyber-view-world.jpg) | MAWI の再生(NETWORK)/ MAWI replay<br>![](docs/images/cyber-view-world-net-mawi.jpg) |
| L2 ループ発生 / L2 loop in progress<br>![](docs/images/l2-loop-links.jpg) | storm-control が2秒で止めた / stopped by storm-control in 2 s<br>![](docs/images/l2-loop-storm-control.jpg) |
| L2 ループの警報(NETWORK)/ loop alert<br>![](docs/images/l2-loop-network.jpg) | RESPONSE のループ遮断 / RESPONSE loop cut<br>![](docs/images/l2-loop-response-cut.jpg) |
| FX(サイバー演出、警報中)/ FX during alerts<br>![](docs/images/cyber-view-fx.jpg) | 通信が多いとき(直した後)/ heavy traffic (after the fix)<br>![](docs/images/cyber-view-busy-after.jpg) |

### 残タスク / Remaining work

ニルヴァーナ / Nirvana

| | 日本語 | English |
|---|---|---|
| ☐ | 遮断・隔離はラボの装置(Cisco 系)だけ。実機向け(スタンドアロン版)からは装置へコマンドを入れない | Block/quarantine works on lab (Cisco-type) devices only; the standalone edition does not push commands to real devices |
| ☐ | OSPF ビューの DR/BDR がルータ単位で、DR 側だけコストが小さく出る(エンジン側の既存の挙動) | OSPF view: DR/BDR is per router and the DR side shows a smaller cost (existing engine behaviour) |
| ☐ | スタンドアロン版には ping の粒・ポートスキャン検知・DEMO が無い(ラボ専用) | The standalone edition lacks ping particles, port-scan detection and DEMO (lab only) |
| ☐ | BOARD に無いもの: 利用者・権限の管理、プラグイン、PDF レポート、Slack/Teams/メールの専用通知(webhook で代用) | Not in BOARD: user and permission management, plugins, PDF reports, native Slack/Teams/mail (use webhooks) |
| ☐ | 内部データソースの PromQL で `or` / `and` / `offset` / `histogram_quantile` が未対応(ヴァルハラ=Prometheus 経由なら使える) | Internal data source PromQL lacks `or` / `and` / `offset` / `histogram_quantile` (available via VALHALLA = Prometheus) |
| ☐ | ATT&CK の対応はこのエミュレータが検知できる事象だけ(エンドポイントのエージェントは無い) | ATT&CK mapping covers only what this emulator can detect (no endpoint agents) |
| ☐ | ヤマハ RTX の CPU の MIB は未確認(設定で OID を指定する)。sFlow は実機の agent では未確認(エミュレータの装置が出す sFlow と sfcapd で確認) | Yamaha RTX CPU MIB unconfirmed (set the OID in config); sFlow not yet tested with a hardware agent (checked with the emulator's own exporter and sfcapd) |
| ☐ | CPU の「情」「注」のしきい値(80% / +20)は仮の値。実機で調整が必要 | CPU info/notice thresholds (80% / +20) are provisional and need tuning on real devices |
| ☐ | README の動画は GIF。再生プレーヤーにするには github.com の編集画面で mp4 をドラッグ&ドロップする | README videos are GIFs; for an inline player, drag and drop the mp4 in the github.com editor |
| ☐ | STP のテストが GitHub の CI でだけ時々失敗する(調査用の出力を仕込み済み) | An STP test fails intermittently only on GitHub CI (diagnostics are in place) |
| ☐ | AI ログ解析: 本物の Claude / Groq / Ollama の答えの質は未確認(この作業環境から届かない。公式 SDK の送信内容は手元の代役で確認)。インストーラーの systemd 登録・--docker は未確認 | AI log analysis: answer quality from real Claude / Groq / Ollama not yet checked (unreachable here; the official SDK's requests were checked against a local stand-in). Installer systemd and --docker paths not yet run |
| ☐ | WORLD の都市: 本物の RIPE Atlas・DB-IP・DShield・Radar L3 はこの作業環境では遮断され未確認(見本と、本物のダークネット・SSH ログイン失敗で表示確認) | WORLD cities: real RIPE Atlas, DB-IP, DShield and Radar L3 are blocked here and not yet verified live (checked with samples plus real darknet packets and SSH login failures) |
| ☐ | WORLD: 本物の Cloudflare Radar(要トークン)・IIJ IHR・MAWI・Google Cloud の IP 帯は、この作業環境では通信がブロックされ未確認(形は公開スキーマで確認、見本で表示確認) | WORLD: real Cloudflare Radar (token needed), IIJ IHR, MAWI and Google Cloud ranges are blocked here and not yet verified live (formats checked against public schemas, display checked with samples) |
| ☐ | L2 ループ: 並列リンクと EtherChannel を区別しない(1本と数える)、VLAN ごとのループ・loopguard・UDLD は未対応。APRESIA の loopdetect の文言は D-Link 系に合わせた(実機の版で違う可能性) | L2 loop: parallel links are not told apart from EtherChannel (counted once); per-VLAN loops, loopguard and UDLD are not modelled; APRESIA loopdetect wording follows the D-Link style (may differ by model/OS) |
| ☐ | 本家 NIRVANA改 ほど「警」で埋まらない(同じ組み合わせの警報は一定時間まとめる) | Fewer "警" than the original NIRVANA-Kai, because repeated alerts are grouped for a cooldown |

エミュレータ全体 / Emulator

| | 日本語 | English |
|---|---|---|
| ☐ | Si-R の `show spanning-tree` の表示のずれ(動作は正しい) | Si-R `show spanning-tree` display is slightly off (behaviour is correct) |
| ☐ | SR-S に Si-R 形式の `lan 0 ip address` を打つとエラーなのに running-config に残る | On SR-S, Si-R-style `lan 0 ip address` errors yet remains in running-config |
| ☐ | 直結サブネット内の誰も持たない IP への ping が成功する(仕様にするか判断待ち) | Ping to an unowned IP in a connected subnet succeeds (awaiting a decision) |
| ☐ | Si-R の SCP サーバー機能(実機のコマンド確認待ち) | Si-R SCP server (waiting to confirm the real command syntax) |

作業の記録は [docs/progress-log.md](docs/progress-log.md) / Progress log: [docs/progress-log.md](docs/progress-log.md)

### 詳しい資料 / Documents

| 内容 / Topic | 資料 / Document |
|---|---|
| 3D 画面の作り / 3D view | [docs/cyber-view-3d.md](docs/cyber-view-3d.md) |
| 2D の流れ表示・侵害検知・再現 / 2D flow, detection, replay | [docs/traffic-flow-visualization.md](docs/traffic-flow-visualization.md) |
| スタンドアロン版 / standalone edition | [cyber-view-standalone/README.md](cyber-view-standalone/README.md)、[docs/cyber-view-standalone.md](docs/cyber-view-standalone.md) |
| 実機の Catalyst を登録 / registering a real Catalyst | [docs/real-catalyst-registration.md](docs/real-catalyst-registration.md) |
| ヴァルハラ連携 / VALHALLA integration | [docs/valhalla-nirvana.md](docs/valhalla-nirvana.md) |
| BOARD / EXPLORE / ALERTS | [docs/nirvana-board.md](docs/nirvana-board.md) |
| 経路と CPU の情・注 / route and CPU info | [docs/snmp-route-cpu-info.md](docs/snmp-route-cpu-info.md) |
| ダークネット観測 / darknet observation | [docs/darknet-nicter.md](docs/darknet-nicter.md) |
| 遮断・ATT&CK・複数拠点・回線チェック・フロー / response, ATT&CK, nexus, WAN, flows | [docs/nirvana-response-attack-nexus-wan-flow.md](docs/nirvana-response-attack-nexus-wan-flow.md) |
| ニルヴァーナで見る監視スタックの IaC(docker compose)/ Nirvana monitoring stack IaC | [docs/nirvana-monitoring-iac.md](docs/nirvana-monitoring-iac.md)(`iac/nirvana-monitoring/`) |
| L2 ループの検知と止め方 / L2 loop detection and stopping | [docs/l2-loop-detection.md](docs/l2-loop-detection.md) |
| WORLD(クラウド・Radar・IHR・MAWI)/ WORLD globe | [docs/world-view.md](docs/world-view.md) |
| Zenn 向けまとめ(できること・できないこと・面白いこと)/ Article draft for Zenn | [docs/zenn/articles/mock-nirvana.md](docs/zenn/articles/mock-nirvana.md) |
| SNMPv3(USM・DES/AES・SHA-2・view)/ SNMPv3 | [docs/snmpv3.md](docs/snmpv3.md) |
| 既存の監視に足す(MRTG・Cacti・インストーラー・AI ログ解析)/ Add-on for existing monitoring | [docs/nirvana-addon-monitoring-ai.md](docs/nirvana-addon-monitoring-ai.md) |
| このプロジェクトの立ち位置(NIRVANA改のリイマジン)/ What this is: a re-imagining of NIRVANA-Kai | [docs/reimagining-nirvana.md](docs/reimagining-nirvana.md) |
| FX・通信が多いときの表示 / FX and heavy-traffic display | [docs/cyber-view-3d.md](docs/cyber-view-3d.md)(末尾 / at the end) |
| 機種 × プロトコルの試験 / vendor × protocol tests | [docs/vendor-protocol-matrix-cyber-view.md](docs/vendor-protocol-matrix-cyber-view.md)、[docs/verification/README.md](docs/verification/README.md) |
| 作業記録 / progress log | [docs/progress-log.md](docs/progress-log.md) |

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


---

<a id="english"></a>

# Network Lab Emulator (English)

A multi-vendor network device CLI emulator. In the browser you can use a CLI close to real equipment, simulate routing
protocols, and try out monitoring and observability. It is not just a "show text generator": several protocols run as real
sockets and real packets (OSPF over raw IP protocol 89, BGP over TCP 179, RIPv2 over UDP 520, SNMP over UDP 161,
NETCONF / SSH-CLI / Telnet-CLI, gNMI over gRPC), so real clients (ncclient, gnmic / pygnmi, snmpwalk, ssh / telnet) can
connect to emulated devices.

Public deployment: https://network-lab-emulator.onrender.com/

## Overview

- Multi-vendor: Cisco / Nexus / Catalyst / ASA / Arista / Juniper / Yamaha RTX / APRESIA / FortiGate / F5 BIG-IP / AWS ALB / Si-R / SR-S / BAS (pseudo FLET'S)
- CLI operation and config loading (some devices use their real syntax as is: aws-cli, tmsh, Junos `set` commands)
- Behaviour for training and testing: OSPF / BGP (Route Reflector, RTBH, Confederation, Graceful Restart / LLGR, RPKI) / RIP /
  VRRP (incl. unicast-peer) / HSRP / STP / LACP / VLAN / IPv6 routing / IPsec / PPPoE / v6plus (MAP-E)
- F5 BIG-IP and AWS ALB do real health checks and real TCP / HTTP load balancing (round-robin, least-connections, persistence, SNAT)
- Topology editor to build connections
- Monitoring with SNMP / syslog / Prometheus / Grafana / Loki / Alertmanager
- Security and load testing tools: Strix (AI pentest agent), ZGrab2, k6
- Tools for real devices: netmiko / Ansible / ncclient / pygnmi
- **Mock Nirvana (`/cyber-view`)**: a 3D network monitoring view in the style of NIRVANA-Kai, with a standalone edition for
  real monitoring environments → [see the section above](#モックニルヴァーナ--cyber-viewcyber-view) (written in Japanese and English)

## Concept: Network Infrastructure Digital Twin + AI Observability

This project can be seen as the network version of a "digital twin" (reproducing physical equipment in a virtual space from sensor data).

| Digital twin element | Implementation here |
|---|---|
| Reproduce physical assets virtually | Catalyst / Cisco / SR-S / ASA / Nexus / Arista / Juniper / Yamaha RTX / APRESIA / FortiGate / BIG-IP / AWS ALB / Si-R / BAS emulated with protocol engines |
| Real telemetry from the virtual model | Virtual SNMP agent (MIB-II, IF-MIB, OSPF-MIB, IP-FORWARD-MIB, CISCO-PROCESS-MIB), real UDP syslog / SNMP traps (`engine/syslog_sender.py`) |
| Monitoring and observability layer | [SNMP dashboard](./docs/snmp-dashboard.md), [Syslog AI monitor](./docs/syslog-ai-monitor.md), Nirvana (`/cyber-view`) |
| Bridge to the real world (tools) | netmiko / paramiko for real devices, `route_injector` (route load tests), `bigip_qkview_collector` (log collection), etc. |

It is not "AI virtualizing the infrastructure" but "AI observing and summarizing a virtualized network infrastructure".
See [`docs/feature-inventory.md`](./docs/feature-inventory.md) for details.

## Supported devices

| Device | CLI style | Main features |
|---|---|---|
| **Cisco Catalyst** | IOS-XE 17.x | OSPF / BGP / HSRP / STP / EtherChannel / ZTP (AUTOINSTALL) / 802.1X |
| **Cisco Nexus** | NX-OS 10.2 | OSPF / BGP / vPC / VRRP / LACP / TACACS+ and AAA / VXLAN EVPN |
| **Cisco ASA** | ASA 9.x | firewall / NAT / ACL / IPsec / PPPoE WAN / threat-detection scanning-threat |
| **Arista** | EOS | shares the Cisco-style tree; netmiko / Ansible (eAPI JSON) |
| **Juniper** | Junos (set / delete / commit) | two-stage candidate / active config, real NETCONF / SSH listeners |
| **Yamaha RTX (RTX1300)** | RTX | real routing, PPPoE client, v6plus (MAP-E), NAT / PPPoE config |
| **APRESIA** | ApresiaLight | VLAN / STP / LACP |
| **FortiGate** | FortiOS (config / edit / set / next / end) | PPPoE WAN, IPsec (IKE / IPsec) |
| **F5 BIG-IP** | TMOS / tmsh | LTM (pool / virtual / monitor) with **real TCP health checks and real traffic distribution**, persistence (source-addr), SNAT (automap / snatpool), net vlan / net self |
| **AWS ALB** | aws-cli (elbv2) | **real HTTP health checks and real L7 distribution** (path / host-based rules), cookie stickiness |
| **Fujitsu SR-S** | SR-S series | VLAN / LACP / STP |
| **Fujitsu Si-R** | Si-R G series | RIP / OSPF / VRRP / BGP / IPsec |
| **BAS (pseudo FLET'S)** | original (access concentrator model) | PPPoE / v6plus (MAP-E) rule server |
| **NetAttest / WLC** | NetAttest EPS style / Catalyst 9800 | real RADIUS, 802.1X, Wi-Fi authentication |
| **PC / Host** | host commands | IP settings, default gateway, real `http.server`, `nmcli` supplicant |

BGP also supports Route Reflector (cluster-id / route-reflector-client), RTBH, Confederation, Hostname Capability,
Graceful Restart / LLGR and RPKI. IPv6 addressing, static routing and ping / traceroute are implemented (`docs/ipv6-routing.md`).

## Main features

- CLI emulation: an interactive CLI close to real devices (`configure`, `interface`, `router ospf`, `show ip route`, …)
- Topology editing: place devices and links by drag and drop; edit and delete connections; manage VLANs
- Protocol emulation: RIP / OSPF / BGP / VRRP / HSRP / STP / LACP
- Monitoring: SNMP traps to metrics, alerting with Prometheus + Alertmanager, Nirvana 3D view
- Real devices: config deployment with netmiko / paramiko; route_injector / qkview / UCS collection tools

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000          (login admin / admin by default; NETLAB_AUTH_DISABLE=1 skips login)
# open http://localhost:8000/cyber-view for Nirvana
```

In the launcher, choose devices, add connections and open CLI terminals; create links between devices in the topology editor.

## Typical uses

- **Learn routing with a CLI**: build a lab in the browser emulator (`show ip route`, `show ip ospf neighbor`, `show run`)
- **Build labs from a topology**: combine switches, routers and PCs; check VLAN / LACP / STP / OSPF; mix vendors
- **Try the same config as real devices**: netmiko / RESTCONF / export-deploy flow, e.g.
  `python tools/eveng_deploy.py export --api http://127.0.0.1:8099 --out ./out --platform c3650`
- **Check monitoring and alerts**: Prometheus + Alertmanager samples (`tools/monitoring_stack/README.md`), the Cisco traffic
  monitoring IaC with Grafana (`iac/cisco-traffic-monitoring/`) or with Nirvana (`iac/nirvana-monitoring/`: snmp_exporter +
  VALHALLA + Loki + Nirvana, with the emulator's devices exporting real sFlow / NetFlow)
- **Watch real devices in 3D**: `cyber-view-standalone/` ([README](cyber-view-standalone/README.md#english))

## Documents

`docs/` has one document per feature area, each with a live verification transcript (more than 90 documents).
Representative ones are listed in the Japanese section above ("ドキュメント一覧"). Nirvana-related documents are listed in the
Nirvana section (日本語 / English).

## Running the tests

```bash
pytest tests/ -q -p no:cacheprovider      # full regression suite (~15 min)
ruff check .
```

## License

MIT License

## Summary

Network Lab Emulator is a CLI-based multi-vendor network emulator that makes network learning, testing, operations
training and demo environments easy to build — from building a lab and understanding protocol behaviour to verifying
monitoring and working with real devices, all in one repository.
