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
| Prometheus | 9090 |
| Alertmanager | 9093 |
| webhook_catcher | 9199 |
