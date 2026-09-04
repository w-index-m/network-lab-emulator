# 装置ヘルスチェック・高CPU調査コマンド集（Catalyst 9300 / Catalyst 8000系）

Catalyst 9300シリーズ（スイッチ、IOS-XE）とCatalyst 8000シリーズ
（ルーター、IOS-XE。旧ISR4000/ASR1000後継）に対して、「問題なく動作しているか」
「なぜCPUが高いのか」を確認するための実コマンド集。

このリポジトリのエミュレーターでは、`catalyst`デバイス（C9300-24Tとして
エミュレート、IOS XE 17.9.1）に対して実際にコマンドを投げて動作確認して
いる。✅は本エミュレーターで実際に出力を確認済み、⚠️は本エミュレーターでは
未実装（実機/EVE-NG等の実機検証環境が必要）を示す。

## 1. まず全体の健全性を見る

| コマンド | 用途 | この環境での確認 |
|---|---|---|
| `show version` | OSバージョン、稼働時間(uptime)、再起動理由の確認 | ✅ |
| `show processes cpu` | 現在のCPU使用率（5秒/1分/5分平均） | ✅ |
| `show memory statistics` | メモリ使用状況 | ✅ |
| `show environment` / `show environment all` | 電源・ファン・温度 | ⚠️ 未実装 |
| `show inventory` | 搭載モジュール・シリアル番号 | ⚠️ 未実装 |
| `show logging \| include %SYS-\|%PLATFORM-` | 直近の重大ログ | ✅（`show logging`は実装済み） |

実際の出力例（このエミュレーターの`catalyst`デバイス）:

```
Catalyst# show version
Cisco IOS XE Software, Version 17.09.01
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.9.1
cisco C9300-24T (X86) processor with 1474560K/6147K bytes of memory.
```

## 2. CPU使用率が高い時の調査手順

### Step 1: 現在どれくらい高いか

```
show processes cpu
```
```
CPU utilization for five seconds: 11%/6%; one minute: 6%; five minutes: 7%
```
「5秒値/割込み分」「1分」「5分」の3つを見る。**5秒値だけ高くて1分・5分が
低ければ瞬間的なスパイクの可能性が高く、5分値まで高いなら継続的な問題**。

### Step 2: どのプロセスが食っているか

```
show processes cpu sorted
show processes cpu sorted 5min
```
使用率降順でプロセス一覧が出る（本エミュレーターでは集計値のみ実装、
プロセス別内訳は⚠️実機必要）。実機では上位に出てくるプロセス名の代表例:

| プロセス名 | 意味 |
|---|---|
| `IOSXE-RP Punt Se` | ハードウェアで捌けずCPUに上がってきたパケット処理（**要注意**） |
| `Spanning Tree` | STP再計算中（トポロジ変化直後は一時的に上がるのは正常） |
| `IP Input` | ルーティング/ACL評価などソフトウェアパス処理 |
| `BGP Router` / `OSPF Router` | ルーティングプロトコルのフル収束処理中 |
| `SNMP ENGINE` | SNMPポーリングが過剰な頻度で来ている |
| `Crypto IKMP` | IPsec/IKEネゴシエーション処理 |

### Step 3: 時系列で継続的か一時的かを見る

```
show processes cpu history
```
```
CPU utilization for five seconds: 12%/2%; one minute: 8%; five minutes: 7%
```
（実機ではアスタリスクのグラフが表示され過去60秒/60分/72時間の推移が分かる。
本エミュレーターでは集計値のみ）

### Step 4: パンチ（ハードウェアでオフロードできず制御プレーンに上がった
トラフィック）が原因かを疑う（Catalyst 9300/8000共通のIOS-XE特有ポイント）

⚠️ 実機のみ:
```
show platform software status control-processor brief
show platform hardware qfp active datapath utilization summary
show platform punt statistics port-asic 0 cause all
```
- 1つ目: コントロールプレーンのCPUコア毎の使用率（IOS-XEはLinuxベースの
  ため複数コアがある）
- 2つ目: QFP（データプレーンASIC）の使用率。ここが高いのに`show processes cpu`
  が低いなら、原因はデータプレーン側（ハード）であってソフトウェアではない
- 3つ目: パント（CPUに上げられた）トラフィックの原因別内訳。ACL評価、
  TTL切れ、未知ユニキャスト等が多ければソフトウェア転送起因と分かる

### Step 5: 割り込み（インタラプト）由来か確認

```
show processes cpu | include Interrupt
```
5秒値の`x%/y%`の`y`側（割込み分）が高い場合、NICドライバレベルの
パケット処理量が多い（=トラフィック量そのものが多い）ことを示唆する。

## 3. インターフェース側の異常も併せて確認（CPU高騰の根本原因になりやすい）

```
show interfaces counters errors
show interfaces status
show spanning-tree summary
```

実際の出力例（このエミュレーターで確認済み）:
```
Port        Align-Err     FCS-Err    Xmit-Err     Rcv-Err  UnderSize  OutDiscards
Gi1/0/1     0             0          0            0        0          0
```
エラーカウンタが増え続けている場合、物理層の異常（ケーブル、SFP、
デュプレックスミスマッチ）が原因でフラッピング・再送が発生し、それが
STP再計算やルーティング再収束を誘発してCPUを押し上げている、という
連鎖もよくあるパターン。

## 4. Catalyst 9300とCatalyst 8000系の違い

| | Catalyst 9300 | Catalyst 8000 |
|---|---|---|
| 役割 | L2/L3スイッチ | ルーター（旧ISR4000/ASR1000後継） |
| データプレーン | UADP ASIC | QFP (Quantum Flow Processor) |
| 高CPU時にまず疑うもの | STP、SNMPポーリング過多、ACL/QoS処理 | BGP/OSPFフルルート処理、IPsec暗号化処理、NAT変換テーブル |
| 特有の確認コマンド | `show platform forward` | `show platform hardware qfp active datapath utilization` |

このリポジトリのエミュレーターは現状Catalyst 9300相当（`catalyst`）と
ISR4321相当（`cisco`）のみで、Catalyst 8000系そのものはまだ存在しない。
コマンド体系はIOS-XE共通のため上記の`show processes cpu`系はそのまま
流用できるが、8000系固有のQFP関連コマンドの動作確認は実機でのみ可能。

## 5. AI是正アドバイザーとの連携

`tools/oscap_ai_advisor.py`と同様の設計思想で、将来的には
「`show processes cpu`の結果を解析し、閾値超過時にAIが原因の
切り分け手順を提示する」ツール化も可能（`tools/syslog_ai_monitor.py`の
CPU版に相当）。現時点では本ドキュメントを手順書として使う運用。

## テスト・検証方法

このドキュメントに記載した「✅確認済み」のコマンドは、以下で再現できる:

```bash
python app.py &
curl -X POST http://localhost:8000/api/cli \
  -H "Content-Type: application/json" \
  -d '{"device_id":"catalyst","command":"show processes cpu"}'
```
