# 監視パイプライン デモケース（仮想環境）

実機を本番で繋ぐ前に、**仮想ラボだけで監視パイプライン全体が動くデモ**を
作れるようにするための手順。`tools/demo_monitoring_pipeline.py` が
中心のツール。

## 全体像

```
tools/demo_monitoring_pipeline.py
   │ 1. 2台にIP設定 + リンク作成
   │ 2. pingを流し続ける（トラフィック/CPUカウンタが動き続ける）
   ▼
Network Lab Emulator（app.py） ── SnmpAgent（MIB-II + CISCO-PROCESS-MIB）
   │
   ├─▶ static/snmp_dashboard.html   （自作ダッシュボードで直接見る）
   │
   └─▶ tools/prometheus_exporter.py ── Prometheus ── (Grafana)
```

## 手順

### 1. エミュレーターを起動

```bash
python app.py
# 認証を無効化したい場合: NETLAB_AUTH_DISABLE=1 python app.py
```

### 2. デモ環境をセットアップしてトラフィックを流す

```bash
python tools/demo_monitoring_pipeline.py
```

既定では `catalyst`(GigabitEthernet1/0/1, 10.9.9.1) と
`cisco`(GigabitEthernet0/0/0, 10.9.9.2) を `/30` でリンクし、
3秒間隔でpingを送り続ける。Ctrl+Cで停止するまで動き続ける。

装置やIPを変えたい場合:

```bash
python tools/demo_monitoring_pipeline.py \
  --device-a catalyst --iface-a GigabitEthernet1/0/1 --ip-a 10.9.9.1 \
  --device-b cisco    --iface-b GigabitEthernet0/0/0 --ip-b 10.9.9.2
```

セットアップだけ行い、トラフィックは流さない場合は `--setup-only`。

### 3-A. 自作ダッシュボードで見る（お手軽）

ブラウザで `http://localhost:8000/static/snmp_dashboard.html` を開く。
`catalyst`/`cisco`のトラフィック・CPUスパークラインが動いているのが見える。

### 3-B. Prometheusで見る（本格派）

```bash
python tools/prometheus_exporter.py
```

別途Prometheusを用意して`http://localhost:9877/metrics`をscrapeすれば、
PromQLで以下のようなクエリが打てる:

```promql
netlab_interface_out_octets_total{device_id="catalyst"}
rate(netlab_interface_out_octets_total{device_id="catalyst"}[1m])
netlab_cpu_percent{device_id="catalyst"}
```

Windows + Docker Desktopでの構築は
[`docs/prometheus-grafana-windows.md`](./prometheus-grafana-windows.md) 参照。

## 実証済みの動作（このデモケースで確認したこと）

このデモケースは実際に動かして検証済み:

1. `--setup-only` を2回実行しても壊れない（冪等）
2. 20秒間トラフィック生成を実行 → 実サーバー(Prometheus)で
   `netlab_interface_out_octets_total{device_id="catalyst"}` が
   `4000` → `12500` に増加することを確認
3. サーバー再起動後も、リンクのインターフェース名が正しく再推定されて
   トラフィックが引き続き記録される（`app.py`の`_load_config()`修正済み）

## 見つけたバグとその修正（この一連の作業で判明）

デモケースを作る過程で、以下の実バグを発見・修正した:

- **`_load_config()`がリンク復元時にインターフェース名を渡していなかった**
  → `dp_engine.bump()`が常にno-opし、事前ロード済みラボ全体でトラフィックが
    永久に0のままになっていた。IPサブネットの一致からインターフェースを
    自動推定するよう修正（`app.py`）

これは「デモを作ろうとしたら、そもそも土台が壊れていた」パターンで、
デモケースを実際に動かして確認する過程で見つかった。

## テスト

```bash
pytest tests/test_demo_monitoring_pipeline.py -v
# 5/5 成功
```
