# Elasticsearch + Kibana でのログ管理

`tools/syslog_to_elasticsearch.py`

## これは何か

`docs/loki-setup.md`（Grafana Loki）のElasticsearch版。
network-lab-emulatorの各装置は`logging host <IP>`を設定すると実際に
RFC3164形式のsyslogをUDPで送信してくる（`engine/syslog_sender.py`）。
これをElasticsearchの`_bulk` API（NDJSON形式）に変換して転送し、
日付ごとのインデックス（`netlab-syslog-YYYY.MM.DD`）に書き込む。
Kibana側は`netlab-syslog-*`というインデックスパターンでそのまま
検索・可視化できる。

## LokiではなくElasticsearchを選ぶ理由

| | Loki | Elasticsearch |
|---|---|---|
| 索引化の対象 | ラベルのみ（本文は圧縮して保存するだけ） | 本文も含めて全文索引化 |
| 検索 | LogQL（ラベルで絞ってから`grep`的に本文を探す） | 任意のキーワード・フィールドでの横断検索 |
| リソース消費 | 軽量 | 重め（JVMヒープが必要） |
| 向いている用途 | 装置名/重大度/時間帯である程度絞れるログ | 本文の任意のキーワードで深掘りしたい、集計・ダッシュボードを作り込みたい |

## このサンドボックスでの制約 — 実際のElasticsearch/Kibanaは動かせない

`docs/prometheus-grafana-windows.md`や`docs/ai-grafana-autopilot.md`で
書いた制約と同じ理由で、Elasticsearch/Kibana本体はこの開発環境からは
入手できない。

```
curl -sIL https://artifacts.elastic.co/downloads/elasticsearch/...
-> HTTP/1.1 403 Forbidden

$HTTPS_PROXY/__agentproxy/status の recentRelayFailures:
  {"kind": "connect_rejected",
   "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
   "host": "artifacts.elastic.co:443"}
```

egressポリシーで`artifacts.elastic.co`がブロックされているため、
実際のElasticsearch/Kibanaサーバをこの環境で起動して確認することは
できない。そのため以下の2段階で検証した（`ai-grafana-autopilot.md`と
同じ手法）:

1. **単体テスト**: `tests/test_syslog_to_elasticsearch.py`で、
   Elasticsearchの`_bulk` APIと同じ形式（`POST /_bulk`、
   `Content-Type: application/x-ndjson`、action行+source行の
   NDJSON）を話す**モックHTTPサーバー**を標準ライブラリのみで
   実際に起動し、`push_to_elasticsearch()`が正しいペイロードを
   送ることを実HTTP通信で確認（7/7成功）

2. **実機統合確認**: 実際にエミュレーターを起動し、`logging host`を
   設定した装置のインタフェースを`shutdown`/`no shutdown`して
   実際にsyslogを2件発生させ、`syslog_to_elasticsearch.py`
   （モックElasticsearchサーバー宛て）を実際に動かして受信させた。
   モックサーバーのログに実際に

   ```
   RECEIVED _bulk (application/x-ndjson):
     {"index": {"_index": "netlab-syslog-2026.09.19"}}
     {"@timestamp": "2026-09-19T04:02:17.703058+00:00", "source_ip": "127.0.0.1",
      "facility": 23, "severity": 3, "severity_name": "err", "facility_tag": "LINK",
      "message": "Sep 19 04:02:17 ES-Demo %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to up"}
   ```

   が記録されることを確認した。実際にリンクをdown/upさせた2件とも
   正しく`_bulk`形式で届いている。

## 実際のElasticsearch/Kibanaに繋ぐ場合

`--es-url`を実際のElasticsearchのURLに置き換えるだけでよい
（モックサーバーと本物のElasticsearchは同じ`_bulk` API形式なので、
コード変更は不要）。

```bash
# Elasticsearch/Kibanaを別途用意した上で
python3 tools/syslog_to_elasticsearch.py \
    --syslog-port 5514 --es-url http://localhost:9200

# 装置側
configure terminal
logging host <このブリッジを動かすホストのIP> 5514
```

Kibana側では「Stack Management > Index Patterns」で
`netlab-syslog-*`を登録すれば、`facility_tag`（%LINK/%LINEPROTO等）や
`severity_name`（err/notice/info等）で絞り込んだダッシュボードが
すぐ作れる。

## テスト

```bash
pytest tests/test_syslog_to_elasticsearch.py -v
# 7/7 成功
```

固定している内容：
1. RFC3164形式のsyslogから`facility_tag`/`severity`を正しく抽出できること
2. Cisco独自のファシリティタグ（`%LINK-3-...`等）が無い平文メッセージでも
   PRIヘッダから妥当な重大度にフォールバックすること
3. インデックス名が`YYYY.MM.DD`区切りで組み立てられること
4. `_bulk`用のNDJSON（action行+source行）が正しい形式で組み立てられること
5. 実際にモックElasticsearchサーバーへPOSTし、`Content-Type:
   application/x-ndjson`と正しいペイロードが届くこと
6. 接続できないサーバーへの送信は例外を上げること（呼び出し側で
   ログして次のパケット処理を続けられるようにするため）
