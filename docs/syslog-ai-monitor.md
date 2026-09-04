# Syslog AI モニター（Splunk風ミニ監視ツール）

`tools/syslog_ai_monitor.py`

## これは何か

`engine/syslog_sender.py` は仮想ネットワーク内のイベント（OSPF隣接断、
STPトポロジ変化、RIP認証失敗など）を**実際のUDPパケット**としてsyslog送信する。
このツールはその受け皿となる、実UDPで受信するsyslogサーバー兼、
一定間隔でログをまとめてAI（Ollama）が要約してくれるミニ監視ツール。

Ollamaが検出できない環境では、統計＋簡易異常検知（フラップ検知・
重要度別ピックアップ）によるルールベース要約に自動フォールバックする。

## 使い方

```bash
# 受信のみ・要約は60秒間隔（デフォルト、ポート5514）
python tools/syslog_ai_monitor.py

# ポート/間隔を指定
python tools/syslog_ai_monitor.py --port 5514 --interval 30

# ログの逐次表示を抑制し、要約のみ表示
python tools/syslog_ai_monitor.py --quiet
```

装置（Si-R/Cisco等）側では、このツールを起動しているホストを
syslog送信先に設定する:

```
syslog server <このツールを動かすホストのIP> 5514
```

## 出力例

```
======================================================================
📊 要約レポート (2026-08-31 10:42:53, 直近5秒)
======================================================================
[ルールベース要約]
件数: 4件
装置別: R1=4
重要度別: notifications=4
イベント種別Top5: %OSPF-5-ADJCHG=4
--- 注目すべき点 ---
⚠ R1 で %OSPF-5-ADJCHG が 4回発生（1秒間）— フラップの可能性
======================================================================
```

## 仕組み

- `SyslogUdpProtocol`: asyncio DatagramProtocolでUDP 5514（既定）を待ち受け
- `parse_syslog_packet()`: RFC3164形式（`<PRI>MMM DD HH:MM:SS HOST MSG`）をパースし、
  `%OSPF-5-ADJCHG` のようなCisco/Si-R系イベントタグも抽出
- `SyslogStore`: 受信ログをメモリ保持 + JSONLファイルへ追記保存
- `summary_loop()`: `--interval` 秒ごとに直近ログを集計
  - Ollamaが起動していれば `_ai_summary()` でLLM要約（app.pyと同じ
    `OLLAMA_URL` / `OLLAMA_MODEL` 環境変数を使用）
  - 使えなければ `_rule_based_summary()` で統計とフラップ検知
    （同一装置・同一イベントタグが短時間に3回以上でフラップ警告）

## 制約

- 標準syslogポート514はroot権限が必要なため、既定は非特権ポート5514
- RFC3164のみ対応（RFC5424形式には未対応）
- テスト: `pytest tests/test_syslog_ai_monitor.py -v`（6/6 成功）
