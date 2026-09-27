# syslogメッセージ頻度の異常検知（Loki連携）

`tools/syslog_anomaly_detector.py`

## これは何か

Grafana Lokiに溜まった装置のsyslogについて、「メッセージ頻度」の
急増を検知する。ユーザーから「Grafana Loki×syslog連携に機械学習を
足したらどうか」という提案を受け、以前CLAUDE.mdに「検討したが未実装」
として残していたアイデアを実装したもの。

`tools/anomaly_autoencoder.py`はCPU/トラフィックの2次元データに
Autoencoderを使うが、ここで見たいのは「1分（任意のバケット幅）あたりの
受信ログ件数」という単一の時系列だけなので、Autoencoderは過剰
（1次元の再構成誤差は実質ただの標準化された絶対偏差になり、学習コスト
だけ増える）。そのため、平常時の平均・標準偏差からしきい値を決める
（平均 + threshold_k × 標準偏差）という、Autoencoder版と同じ「普段の
ふるまいを学習してから外れを見る」考え方を1次元向けに単純化した。

急増を検知した装置については、`tools/network_ontology_query.py`の
`summarize_logs_via_ollama()`をそのまま再利用し、該当区間の生ログを
Ollamaに渡して「何が起きたか」を日本語で要約させることもできる
（`--summarize`指定時。Ollamaが無ければ何もせずNoneのまま、他の
AI要約機能と同じベストエフォート方式）。

## 仕組み

1. `fetch_syslog_entries()` — Lokiの`{job="netlab-syslog"}`から
   指定期間の生ログ全件を(タイムスタンプ, source_ip, 本文)で取得
2. `bucket_counts()` — 装置(source_ip)ごとに、時間バケット単位の
   ログ件数の配列を作る
3. `detect_spikes()` — 各装置について前半をベースライン学習に使い、
   後半（直近側）のバケットで急増していないか`LogRateAnomalyModel`で判定
4. 急増を検知した装置は、該当バケットの生ログを`--summarize`指定時に
   Ollamaへ渡して要約

## 実際に動かして確認した結果

`tools/setup_monitoring_stack.sh`で実際にGrafana+Loki+syslogブリッジを
起動し、実装置を1台作成して以下の手順で検証した:

```bash
# 装置にsyslog転送を設定
enable
configure terminal
logging host 127.0.0.1 5514
logging trap debugging
end

# ベースライン: インタフェースを1回だけflap（2行のsyslog）
interface GigabitEthernet1/0/1
shutdown
no shutdown

# 15秒待ってから、実際のバースト: 3インタフェース×8回flap（48行）
# (シェルループでshutdown/no shutdownを繰り返し送信)
```

```
$ python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
    --window-minutes 5 --bucket-seconds 10 --summarize
🚨 急増検知  127.0.0.1: 直近ピーク 48件/10秒 (平常時 平均0.0±0.0件, スコア=48.00)
      AI要約: (Ollama未接続または要約失敗)
```

Lokiに実際に届いた生ログの総数を直接クエリで確認したところ**50件**で、
ベースライン2件＋バースト48件と正確に一致した（検知ロジックが取りこぼし
なく全件を数えていることを確認）。このサンドボックスにはOllamaが動いて
いないため、`--summarize`は正しくフォールバックして
`(Ollama未接続または要約失敗)`を返し、クラッシュしないことも確認した。

## 使い方

```bash
# 直近30分を1分バケットに分割し、装置ごとのログ件数の急増を検知
python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
    --window-minutes 30 --bucket-seconds 60

# 検知した急増区間のログをOllamaで要約
python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
    --window-minutes 30 --summarize

# しきい値を調整（デフォルト: 平均+4標準偏差）
python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
    --threshold-k 3.0
```

## テスト

```bash
pytest tests/test_syslog_anomaly_detector.py -v
# 10/10 成功
```

Lokiへの実問い合わせ部分（`fetch_syslog_entries`）はネットワークI/Oの
ため合成データでは固定していない。バケット化・異常検知ロジック
（`bucket_counts`/`detect_spikes`/`LogRateAnomalyModel`）を合成データで
固定し、実際のLokiとの組み合わせは上記の実機検証で確認した。

## 制約・今後の拡張余地

- 「前半をベースライン、後半で急増を見る」という単純な分割方式のため、
  観測期間全体を通してゆっくり増加するようなケース（急増ではなく
  トレンド変化）は検知できない
- 装置ごとに独立して評価するため、複数装置にまたがる相関（例:
  ネットワーク全体で同時多発的に少しずつ増えている）は見ない
- ベースライン期間のサンプル数が少ないと標準偏差が小さくなりすぎ、
  普段のブレでも過敏に反応する可能性がある（`--threshold-k`で調整可能）
- ログ本文の内容自体は解釈しない（`--summarize`を付けない限り、
  頻度だけを見て中身は見ない）
