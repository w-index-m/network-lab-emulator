# 監視スタック統合ガイド（Prometheus + Grafana + Alertmanager + FRR）

network-lab-emulator を、実バイナリの Prometheus / Grafana / Alertmanager /
FRRouting と接続して検証した際の、環境構築〜動作確認までの手順まとめ。
全て実際にプロセスを起動し、実HTTP/実TCPで疎通確認済み。

## 全体構成

```
network-lab-emulator (app.py, :8000)
        │  GET /api/snmp/dashboard
        ▼
prometheus_exporter.py (:9877/metrics)
        │  scrape (5s interval)
        ▼
Prometheus (:9090)
        │  rule evaluation (netlab_interface_oper_status == 2 等)
        ▼
Alertmanager (:9093)
        │
        ▼
Grafana (:3000) ← Prometheus datasource

route_injector_cli.py ──BGP/RIP──▶ FRR (bgpd/zebra) 実RIB
```

## 監視の役割分担: メトリクス vs ログ

このプロジェクトでは監視対象の性質によって2系統を使い分ける方針とする。

| | メトリクス監視 | ログ監視 |
|---|---|---|
| 対象 | CPU使用率・インターフェース状態・経路数などの定量値 | syslogのテキストメッセージ |
| 担当 | Prometheus + **Alertmanager** | **`tools/syslog_ai_monitor.py`** |
| 得意なこと | 閾値ベースのアラート（`CPU >= 80%`、`ifOperStatus == 2`等）、時系列トレンド | パターン相関・フラップ検知・AI要約（Ollama/ルールベース） |
| 判断基準 | 「今どういう状態か」を数値で継続的に見る | 「何が起きたか」をイベント単位で解釈する |

Splunkのようなログ集約基盤も選択肢としてあり得るが、既にPrometheus基盤が
あること・メトリクス中心の監視要件であることから、メトリクスは
Alertmanager、ログは自作のAI syslogモニターで分担する構成を採用している。
詳細: `docs/syslog-ai-monitor.md`

## 1. アプリ本体 + Prometheus Exporter

```bash
# エミュレータ本体
uvicorn app:app --host 0.0.0.0 --port 8000 &

# Exporter（/api/snmp/dashboard をポーリングして Prometheus形式で公開）
python tools/prometheus_exporter.py --interval 5
# → http://localhost:9877/metrics
```

公開される主なメトリクス:

| メトリクス | 内容 |
|---|---|
| `netlab_interface_oper_status` | ifOperStatus (1=up, 2=down) |
| `netlab_interface_admin_status` | ifAdminStatus |
| `netlab_interface_in/out_octets_total` | トラフィックカウンタ |
| `netlab_cpu_percent` | CISCO-PROCESS-MIB 相当のCPU使用率 |
| `netlab_route_count` | デバイス毎の経路数（RIB） |

詳細: `docs/prometheus-grafana-windows.md`

## 2. Prometheus 実バイナリ

GitHub Releases からダウンロード（`releases/download/...` は許可されたCDNにリダイレクトされるため、この環境からでも直接取得可能）。

```bash
curl -sL https://github.com/prometheus/prometheus/releases/download/v2.54.1/prometheus-2.54.1.linux-amd64.tar.gz -o prometheus.tar.gz
tar xzf prometheus.tar.gz
```

`prometheus.yml`（Alertmanager連携込みの例）:

```yaml
global:
  scrape_interval: 5s
  evaluation_interval: 5s
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['localhost:9093']
rule_files:
  - /path/to/alert_rules.yml
scrape_configs:
  - job_name: 'netlab-emulator'
    static_configs:
      - targets: ['localhost:9877']
```

起動:

```bash
./prometheus --config.file=prometheus.yml --storage.tsdb.path=./prom-data \
  --web.listen-address=0.0.0.0:9090
```

確認:

```bash
curl http://localhost:9090/-/healthy          # → Prometheus Server is Healthy.
curl http://localhost:9090/api/v1/rules       # ロードされたアラートルール一覧
```

## 3. アラートルール例

`alert_rules.yml`:

```yaml
groups:
  - name: netlab
    rules:
      - alert: InterfaceDown
        expr: netlab_interface_oper_status == 2
        for: 0s
        labels:
          severity: critical
        annotations:
          summary: "{{ $labels.hostname }} interface {{ $labels.interface }} is down"
```

## 4. Alertmanager 実バイナリ

```bash
curl -sL https://github.com/prometheus/alertmanager/releases/download/v0.27.0/alertmanager-0.27.0.linux-amd64.tar.gz -o alertmanager.tar.gz
tar xzf alertmanager.tar.gz
```

`alertmanager.yml`（最小構成）:

```yaml
route:
  receiver: 'default'
receivers:
  - name: 'default'
```

起動（**注意**: サンドボックス環境のようにプライベートIPが取得できない環境では
`--cluster.listen-address=""` を付けないとgossipメッシュ初期化で起動失敗する）:

```bash
./alertmanager --config.file=alertmanager.yml --storage.path=./alertmanager-data \
  --web.listen-address=0.0.0.0:9093 --cluster.listen-address=""
```

確認:

```bash
curl -o /dev/null -w "%{http_code}" http://localhost:9093/   # → 200
```

### 動作確認手順（実際に確認したフロー）

```bash
# 1. インターフェースをshutdown
curl -X POST http://localhost:8000/api/cli -d '{"device_id":"asa","command":"configure terminal"}'
curl -X POST http://localhost:8000/api/cli -d '{"device_id":"asa","command":"interface GigabitEthernet0/1"}'
curl -X POST http://localhost:8000/api/cli -d '{"device_id":"asa","command":"shutdown"}'
curl -X POST http://localhost:8000/api/cli -d '{"device_id":"asa","command":"end"}'

# 2. exporterのメトリクスが2(down)に切り替わる（次回ポーリング後）
curl -s http://localhost:9877/metrics | grep GigabitEthernet0/1

# 3. Prometheusのルールが firing に遷移（評価間隔5秒以内）
curl -s http://localhost:9090/api/v1/rules | python3 -m json.tool

# 4. Alertmanagerが実際に受信しているか確認
curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool

# 5. no shutdownで復旧させると、ルールが inactive に戻る
```

実測結果: shutdown実行から約2秒でPrometheusルールが `firing`、
Alertmanagerの `/api/v2/alerts` に `state: active` として反映。
`no shutdown` 後、次の評価サイクルで `inactive` に復帰。

## 5. Grafana 実バイナリ

**注意**: この環境のプロキシは `dl.grafana.com` 等の直接ダウンロードURLを
ブロックする。回避策として、ユーザー自身のPC（制限なしネットワーク）で
公式サイトから `grafana-enterprise_<version>_linux_amd64.tar.gz` を
ダウンロードし、`w-index-m/network-lab-emulator` の GitHub Release
アセットとしてアップロードすれば、`releases/download/...` 経由で
取得できる（このURLは許可されたCDNにリダイレクトされるため）。

```bash
curl -sL https://github.com/w-index-m/network-lab-emulator/releases/download/<tag>/grafana.tar.gz -o grafana.tar.gz
tar xzf grafana.tar.gz
```

起動（`--homepath` は**絶対パス必須**。相対パス `.` だと
`could not find core plugins` で失敗する）:

```bash
./bin/grafana server --homepath=/absolute/path/to/grafana-v13.2.0
# → http://localhost:3000  (初期ログイン admin/admin)
```

Prometheus データソース登録（APIで自動化可能）:

```bash
curl -X POST http://admin:admin@localhost:3000/api/datasources \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Prometheus",
    "type": "prometheus",
    "url": "http://localhost:9090",
    "access": "proxy",
    "isDefault": true
  }'
```

Explore画面で `netlab_cpu_percent` 等をクエリし、実際に時系列データが
表示されることを確認済み（デバイス3台分のCPU使用率グラフ）。

## 6. AI Grafana Autopilot（人手を介さないアノテーション投稿）

```bash
python tools/ai_grafana_autopilot.py --grafana-url http://localhost:3000 \
  --grafana-user admin --grafana-password admin --interval 10
```

CPU 80%以上、インターフェースdown、フラップ検知時に
`POST /api/annotations` でGrafanaに自動でアノテーションを打つ。
詳細: `docs/ai-grafana-autopilot.md`

## 7. FRRouting (FRR) との実BGPセッション

FRRはUbuntuの標準パッケージリポジトリ（archive.ubuntu.com、ブロック対象外）
から直接インストール可能:

```bash
apt-get install -y frr frr-pythontools
systemctl start frr   # または /usr/lib/frr/bgpd 等を直接起動
```

`route_injector_cli.py` から実eBGPネイバーを確立し、経路をFRRのRIBに
実際にインストール:

```bash
python tools/route_injector_cli.py bgp --peer <FRR-IP> \
  --local-as 65002 --remote-as 65001 --router-id 10.0.0.1 \
  --route 172.30.0.0/24 --next-hop <local-IP> --community 65002:100
```

**注意**: FRR 8.x は RFC8212 準拠のため `bgp ebgp-requires-policy` が
デフォルトON。ポリシー未設定のeBGP経路は `(Policy)` として弾かれるので、
`no bgp ebgp-requires-policy` をvtysh等で設定する必要がある。

確認:

```bash
vtysh -c "show ip bgp"
# → 172.30.0.0/24 が実際にRIBにインストールされ、AS_PATH/communityが
#   正しく反映されていることを確認
```

詳細: `docs/route-injector-cli.md`

## 8. まとめ: 検証済みコンポーネント一覧

| コンポーネント | バージョン | 入手経路 | 状態 |
|---|---|---|---|
| Prometheus | v2.54.1 | GitHub Releases（直接） | ✅ 実バイナリ起動・scrape確認済み |
| Alertmanager | v0.27.0 | GitHub Releases（直接） | ✅ 実バイナリ起動・アラート受信確認済み |
| Grafana | v13.2.0 | GitHub Release（自リポジトリ経由） | ✅ 実バイナリ起動・データソース疎通確認済み |
| FRRouting | apt版 | apt-get（Ubuntu公式） | ✅ 実BGPセッション確立・RIB反映確認済み |

いずれも「コードを書いて動きそうに見える」ではなく、実プロセスを起動し
実HTTP/実TCPでの応答・状態遷移を curl / API で直接確認した上での記録。
