# Prometheus Exporter + Grafana（Windows版）

`tools/prometheus_exporter.py` + `tools/run_prometheus_exporter.bat`

## これは何か

`static/snmp_dashboard.html`（自作の1枚もの監視ページ）とは別に、本格的な
可視化基盤（Grafana）につなげたい場合の経路。エミュレーターの
`GET /api/snmp/dashboard` を定期ポーリングし、Prometheusが読める
text exposition format で `/metrics` に公開する**Exporter**。

```
Network Lab Emulator (app.py)
   │  GET /api/snmp/dashboard (JSON)
   ▼
tools/prometheus_exporter.py  ── /metrics (Prometheus形式) ──▶  Prometheus (scrape)
                                                                     │
                                                                     ▼
                                                                  Grafana (可視化)
```

Exporter自体はPython標準ライブラリのみで動作する（`urllib`/`http.server`）。
追加パッケージのインストールは不要。

## Windowsでのセットアップ

### 1. Exporterを起動

```
tools\run_prometheus_exporter.bat
```

既定でエミュレーター `http://localhost:8000` をポーリングし、
`http://localhost:9877/metrics` に公開する。エミュレーターのURLや
公開ポートを変えたい場合は `.bat` 内の `EMULATOR_URL` / `EXPORT_PORT`
を書き換えるか、直接:

```
python tools\prometheus_exporter.py --emulator-url http://localhost:8000 --port 9877
```

ログイン認証が有効な構成（`NETLAB_AUTH_DISABLE`未設定）の場合は
`--token <セッショントークン>` を付与する。

### 2. Prometheus + Grafana をセットアップ

**方法A: Docker Desktop（推奨・一番手軽）**

`monitoring/docker-compose.yml` を同梱済み。Docker Desktop for Windows が
入っていれば、インストール作業なしでそのまま:

```
cd monitoring
docker compose up -d
```

- Grafana: http://localhost:3000 （`admin` / `admin`、初回にパスワード変更を求められる）
- Prometheus UI: http://localhost:9090
- Alertmanager UI: http://localhost:9093
- Grafanaのデータソース（Prometheus）は `grafana-provisioning/` により
  **自動設定済み**。手動でのデータソース追加は不要
- `alert_rules.yml` に `InterfaceDown`（ifOperStatus==2）と
  `HighCpu`（CPU使用率80%以上）のアラートルールを同梱。Prometheusが
  評価し、`alertmanager.yml`（受信設定）経由でAlertmanagerに通知される。
  この構成は、Linuxサンドボックス環境で `tools/setup_monitoring_stack.sh`
  により実バイナリで動作確認済みの構成と同じもの。

Exporter（`tools/prometheus_exporter.py`）はコンテナ化しておらず、
Windowsホスト側で直接 `python` 実行する想定。`monitoring/prometheus.yml`
は `host.docker.internal:9877` を見に行く設定にしてあるので、Docker Desktop
（Windows/Mac）ならそのまま届く。

停止は `docker compose down`。

**方法B: ネイティブインストール**

- Prometheus: [prometheus.io/download](https://prometheus.io/download/) から
  Windows用バイナリ（`prometheus-x.x.x.windows-amd64.zip`）を取得し、
  `prometheus.yml` に以下を追加して `prometheus.exe --config.file=prometheus.yml` で起動:
  ```yaml
  scrape_configs:
    - job_name: 'netlab-emulator'
      static_configs:
        - targets: ['localhost:9877']
      scrape_interval: 15s
  ```
- Grafana: [grafana.com/grafana/download](https://grafana.com/grafana/download?platform=windows)
  からWindows installerを取得してインストール・起動（既定 `http://localhost:3000`）。
  Data sources → Add data source → Prometheus → URL `http://localhost:9090` を手動設定。

いずれの方法でも、Explore または新規ダッシュボードで `netlab_*` メトリックをクエリできる。

## 公開しているメトリック

| メトリック | 型 | 説明 |
|---|---|---|
| `netlab_device_up` | gauge | 装置が登録されているか（常時1） |
| `netlab_device_health` | gauge | 1=HEALTHY（全IF up）、0=ATTENTION |
| `netlab_sys_uptime_seconds` | gauge | sysUpTime |
| `netlab_cpu_percent` | gauge | CPU使用率（Cisco系機種のみ、CISCO-PROCESS-MIB相当） |
| `netlab_interface_admin_status` | gauge | ifAdminStatus（1=up, 2=down） |
| `netlab_interface_oper_status` | gauge | ifOperStatus（1=up, 2=down） |
| `netlab_interface_in_octets_total` | counter | ifInOctets（累積） |
| `netlab_interface_out_octets_total` | counter | ifOutOctets（累積） |
| `netlab_interface_speed_bps` | gauge | ifSpeed |
| `netlab_route_count` | gauge | RIBの最良経路数（`rib_engine.get_best_routes()`、`tools/routing_generator.py`で増減を確認可能） |
| `netlab_watchlist_target` | gauge | `tools/nl_monitor_control.py`で自然言語指示により監視対象登録された場合1（未登録のインターフェースには出力されない） |

ラベル: `device_id`, `hostname`, `type`（+インターフェース系メトリックは `interface`）

## 自然言語での監視対象追加との連携

`tools/nl_monitor_control.py`で「Catalystの10.9.9.1のGi1/0/1を監視対象にして」のように
指示すると、`tools/monitor_watchlist.json`に対象が追記される。Exporterは
`--watchlist`（既定でこのファイルを指す）を毎ポーリングごとに読み直しており、
一致するインターフェースに `netlab_watchlist_target{...} 1` を付与する。

Grafana側では例えば以下のようなPromQLで「NLツールで追加した対象だけ」に
絞ったパネル/アラートを作れる:

```
netlab_interface_oper_status * on(device_id, interface) netlab_watchlist_target
```

Exporterの起動場所と`monitor_watchlist.json`は同じマシン上にある必要がある
（既定では`tools/`ディレクトリ内の同じファイルを両方が参照する）。別ホストで
watchlistを管理したい場合は `--watchlist <path>` で明示的に指定する。

## Grafanaでのクエリ例

```promql
# CPU使用率の推移
netlab_cpu_percent{type="catalyst"}

# インターフェースの受信トラフィックレート（bytes/sec）
rate(netlab_interface_in_octets_total[1m])

# Down中のインターフェース数
count(netlab_interface_oper_status == 2)
```

## 実装で気をつけた点

Prometheus text exposition formatは**同一メトリック名のサンプルが
連続していること**が仕様で決まっている（装置ごとに混在させてはいけない）。
最初の実装では装置ループの中で全メトリックを出力していたためこれに違反し、
`prometheus_client`のパーサーで585個ものバラバラのfamilyに分解される
バグがあった（レビューで発見）。メトリック単位でループする構成に修正し、
`prometheus_client.parser`で正しく9個のfamilyにパースされることを確認済み。

## テスト

```bash
pytest tests/test_prometheus_exporter.py -v
# 4/4 成功
```

## 制約

- Prometheus/Grafana本体（コンテナイメージ）はこのリポジトリでは提供しない（Docker Hub上の公式イメージを`docker-compose.yml`が参照）
- 認証・TLSはExporter側では未実装（社内ラボ用途を想定したシンプル構成）
- **`monitoring/docker-compose.yml` / `prometheus.yml` / Grafanaデータソース定義はYAML構文のみ検証済み。
  Docker daemonが使える環境がなく、実際にコンテナを起動しての動作確認（Grafana起動〜Prometheus疎通〜クエリ実行）
  はできていない。** Docker Desktop環境で試した際に問題があれば教えてほしい。
