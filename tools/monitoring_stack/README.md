# SNMP trap → Prometheus → Alertmanager パイプライン一式

2026-09-05に実際に動作確認したパイプラインの設定一式。
Prometheus/Alertmanagerの実バイナリ自体はサイズが大きいためリポジトリには
含めず、設定ファイルとダウンロード手順のみをここに置く。

```
Ciscoで shutdown
  → SNMP trap(linkDown)実送信
  → tools/snmp_trap_receiver.py が受信・/metricsに反映
  → Prometheusが5秒間隔でスクレイプ、alert_rules.ymlで評価 → firing
  → Alertmanagerへ通知
  → webhook_catcher.py（受信テスト用の簡易サーバー）へ着弾
```

## 再現手順

```bash
# 1. バイナリ取得（初回のみ）
mkdir -p /tmp/monitoring_stack && cd /tmp/monitoring_stack
curl -sL https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.linux-amd64.tar.gz -o prometheus.tar.gz
curl -sL https://github.com/prometheus/alertmanager/releases/download/v0.27.0/alertmanager-0.27.0.linux-amd64.tar.gz -o alertmanager.tar.gz
tar xzf prometheus.tar.gz
tar xzf alertmanager.tar.gz

# 2. このディレクトリの設定ファイルをコピー
cp /path/to/network-lab-emulator/tools/monitoring_stack/prometheus.yml prometheus-2.54.1.linux-amd64/
cp /path/to/network-lab-emulator/tools/monitoring_stack/alert_rules.yml prometheus-2.54.1.linux-amd64/
cp /path/to/network-lab-emulator/tools/monitoring_stack/alertmanager.yml alertmanager-0.27.0.linux-amd64/

# 3. エミュレーター起動（別ターミナル）
cd /path/to/network-lab-emulator
NETLAB_AUTH_DISABLE=1 NETLAB_FAST_TIMERS=1 python -m uvicorn app:app --port 8000

# 4. SNMP trap受信ツール起動（別ターミナル）
python tools/snmp_trap_receiver.py --trap-port 1162 --metrics-port 9162

# 5. webhook受信確認用の簡易サーバー起動（別ターミナル）
python tools/monitoring_stack/webhook_catcher.py

# 6. Alertmanager起動（別ターミナル）
cd /tmp/monitoring_stack/alertmanager-0.27.0.linux-amd64
./alertmanager --config.file=alertmanager.yml --storage.path=./alertmanager-data --web.listen-address=0.0.0.0:9093

# 7. Prometheus起動（別ターミナル）
cd /tmp/monitoring_stack/prometheus-2.54.1.linux-amd64
./prometheus --config.file=prometheus.yml --storage.tsdb.path=./data --web.listen-address=0.0.0.0:9090
```

## 発火させる

```bash
curl -X POST http://localhost:8000/api/device -H 'Content-Type: application/json' \
  -d '{"id":"cmon1","type":"cisco","hostname":"cmon1"}'

curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"conf t"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"interface GigabitEthernet0/7"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"ip address 100.64.40.1 255.255.255.252"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"no shutdown"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"snmp-server host 127.0.0.1 udp-port 1162 traps public"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"end"}'

curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"interface GigabitEthernet0/7"}'
curl -X POST http://localhost:8000/api/cli -H 'Content-Type: application/json' \
  -d '{"device_id":"cmon1","command":"shutdown"}'
```

数秒後（Prometheusのスクレイプ間隔=5秒＋alertルールのfor無し即時判定）に:
- `http://localhost:9090/alerts` で `SnmpLinkDownTrapReceived` が firing
- `http://localhost:9093` （Alertmanager Web UI）にアラート表示
- `webhook_catcher.py` の標準出力に着弾ログ

## ポート一覧

| コンポーネント | ポート |
|---|---|
| エミュレーター(app.py) | 8000 |
| snmp_trap_receiver（trap受信） | UDP 1162 |
| snmp_trap_receiver（/metrics） | 9162 |
| prometheus_exporter（/metrics、Strixステータス含む） | 9877 |
| Prometheus | 9090 |
| Alertmanager | 9093 |
| webhook_catcher | 9199 |

## Strix(strix-pentest.yml)ペンテスト結果の監視

ユーザー提案「GrafanaアラートやGrafana Lokiと連携してほしい」への対応。
`/pentest`操作パネル・`tools/prometheus_exporter.py`が使っているのと
同じ`/api/pentest/runs`(GitHub Actions経由のStrix実行状況)を、
Prometheusメトリクスとアラート、Lokiログの両方に流せるようにした。

```
GitHub Actions(strix-pentest.yml)
   │  GET /api/pentest/runs (app.pyがGitHub Actions REST APIを代理)
   ▼
tools/prometheus_exporter.py ── /metrics (netlab_strix_*) ──▶ Prometheus
                             └─ (本ディレクトリのalert_rules.yml: strix-pentest グループ) ──▶ Alertmanager
tools/strix_status_to_loki.py ── 新規completed runのみ ──▶ Loki
```

### メトリクス(`netlab_strix_*`、exporter起動には`GITHUB_TOKEN`が必要)

| メトリクス | 意味 |
|---|---|
| `netlab_strix_has_runs` | 一度でもrunが観測されたか(1/0) |
| `netlab_strix_last_run_success` | 直近runが成功したか(1/0) |
| `netlab_strix_last_run_in_progress` | 直近runが実行中/待機中か(1/0) |
| `netlab_strix_last_success_age_seconds` | 最後に成功したrunからの経過秒数(成功run無しなら-1) |
| `netlab_strix_recent_runs_total{conclusion}` | 直近取得分(既定5件)中の成功/失敗件数 |

### アラート(`alert_rules.yml`の`strix-pentest`グループ)

- `StrixPentestRunFailed`: 直近runが失敗で確定している場合(`has_runs==1`で
  ゲートし、一度もDispatchしていない状態での誤発火を防ぐ)
- `StrixPentestStale`: 成功runが7日間無い、または一度も無い場合

### 起動(上記のPrometheus/Alertmanagerに追加して)

```bash
# exporter(Strixステータスも含めてポーリング)
python tools/prometheus_exporter.py --port 9877

# Lokiへの転送(Lokiが別途動いている前提)
python tools/strix_status_to_loki.py --loki-url http://localhost:3100
```

`/metrics`で`netlab_strix_last_run_success 0`になったタイミングで
`StrixPentestRunFailed`がfiringになり、既存の`webhook_catcher.py`に着弾
する(SNMP trapの時と同じ通知経路をそのまま使う)。
