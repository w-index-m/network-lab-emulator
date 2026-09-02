# Grafana Loki セットアップ

`Prometheus/Alertmanager/Grafana`（メトリクス監視）に加えて、ログ集約基盤
として Grafana Loki を実際に検証した記録。

## 取得方法

Loki本体はGitHub Releasesからzip形式で配布されている（Prometheus/
Alertmanagerのtar.gzとは異なる点に注意）。

```bash
curl -sL -o loki.zip \
  "https://github.com/grafana/loki/releases/download/v3.2.0/loki-linux-amd64.zip"
unzip -q loki.zip
chmod +x loki-linux-amd64
```

## 設定ファイル（最小構成）

```yaml
auth_enabled: false

server:
  http_listen_address: 127.0.0.1
  http_listen_port: 3100
  grpc_listen_address: 127.0.0.1
  grpc_listen_port: 9096

common:
  instance_addr: 127.0.0.1
  instance_interface_names:
    - lo
  path_prefix: /path/to/loki-data
  storage:
    filesystem:
      chunks_directory: /path/to/loki-data/chunks
      rules_directory: /path/to/loki-data/rules
  replication_factor: 1
  ring:
    instance_addr: 127.0.0.1
    instance_interface_names:
      - lo
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

frontend_worker:
  frontend_address: 127.0.0.1:9096

ruler:
  alertmanager_url: http://localhost:9093
```

起動:

```bash
./loki-linux-amd64 --config.file=loki-config.yml \
  -frontend.instance-addr=127.0.0.1 \
  -frontend.instance-interface-names=lo \
  -query-scheduler.ring.instance-addr=127.0.0.1 \
  -query-scheduler.ring.instance-interface-names=lo
```

## 発見・修正した実バグ（サンドボックス/コンテナ環境特有）

単一バイナリでもLokiは内部的にquery-frontend/query-scheduler/querierが
gRPC(HTTP/2)で相互通信する設計になっている。この環境ではネットワーク
インターフェースの自動検出が失敗し、advertiseアドレスが
`192.0.2.2`（RFC 5737のドキュメンテーション用アドレス、実在しない）に
誤って解決されてしまい、以下のエラーでクエリが**無限にハング**する
不具合があった:

```
error notifying frontend about finished query" err="rpc error: code = Unavailable
desc = connection error: desc = \"error reading server preface: http2: frame too large\""
```

`push`（ログの取り込み）自体は正常に完了する（`204`が返る）ため、
「動いているように見えて実はクエリが一切通らない」という気づきにくい
壊れ方をする。

**原因**: `common.instance_addr`/`ring.instance_addr`をYAMLで指定しても
query-frontend側のアドバタイズアドレス（`frontend.instance_addr`という
YAMLキーは実は存在せず`lokifrontend.Config`にそのフィールドは無い）には
反映されない。正しくは`-frontend.instance-addr`と
`-frontend.instance-interface-names=lo`を**CLIフラグとして明示的に**
渡す必要がある。

**修正後の確認**: ログに`frontend=127.0.0.1:9096`と正しく表示されるように
なり、`frame too large`エラーが解消。実際に`push`→`query_range`で
投入したログメッセージが正しく取得できることを確認した。

## 動作確認したAPI呼び出し

```bash
# ログ投入
curl -X POST http://localhost:3100/loki/api/v1/push \
  -H "Content-Type: application/json" \
  -d '{"streams":[{"stream":{"job":"netlab-test","device":"catalyst"},"values":[["<unix_nano>","test log message"]]}]}'
# → 204

# クエリ
curl -G http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={job="netlab-test"}' \
  --data-urlencode 'start=<unix_nano>' \
  --data-urlencode 'end=<unix_nano>'
# → 200、投入したログが streams.values に含まれる
```

## Grafanaとの連携（今後）

Prometheusと同様、GrafanaのデータソースとしてLoki（`http://localhost:3100`）
を追加すれば、Explore画面でLogQLクエリが実行できる。まだデータソース
登録・ダッシュボード連携までは未実施。

## 装置ログをLokiに転送する

`tools/syslog_to_loki.py`: `logging host <IP>`設定による実UDP syslogを
受信してLokiに転送するブリッジ。ただし**重要な制約**がある。

### 発見した制約: `shutdown`コマンドはUDP syslogとして飛ばない

network-lab-emulatorの実UDP syslog送信パイプライン
（`engine/syslog_sender.py`の`syslog_dispatcher`）は、OSPF/RIP/BGP/STP
ログ等、限定されたイベント種別(`msg_type`)のみを対象にしている
（`engine/protocols.py`の該当箇所を参照）。CLIで`shutdown`/`no shutdown`
した際に出る`%LINK-3-UPDOWN`は`show logging`の内部バッファには記録
されるが、この実送信パイプラインの対象には含まれていないため、
`syslog_to_loki.py`だけではインターフェースdown/upのログはLokiに
届かない。

### 対応: `tools/device_log_to_loki.py`

この制約を回避するため、対象装置の`show logging`を定期ポーリングし、
新規に増えた行だけをLokiにpushするツールを追加した。

```bash
python tools/device_log_to_loki.py --devices catalyst,nexus --interval 3
```

### 実際に確認した動作

CatalystとNexusで実際にインターフェースを`shutdown`し、Lokiに転送・
LogQLで検索できることを確認した:

```bash
curl -G http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={job="netlab-device-log"} |= "UPDOWN"' \
  --data-urlencode 'start=<unix_nano>' --data-urlencode 'end=<unix_nano>'
```

結果（抜粋）:
```
nexus / nexus
   *Sep 02 07:03:36.724: %LINK-3-UPDOWN: Interface GigabitEthernet0/0/1, changed state to down
catalyst / Dist-SW
   *Sep 02 07:03:31.797: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down
```

両装置のlink downイベントが正しくLokiに取り込まれ、LogQLで検索できる
ことを確認済み。
