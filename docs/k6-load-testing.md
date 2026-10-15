# k6によるSLB(ALB/BIG-IP)への本格負荷試験

## これは何か

ユーザー質問「NW slb 負荷かけツールって知ってます？」への回答として、
ab/wrk/hey/Locust/k6/tsung/httperfなど実務で使われるNW負荷かけツールを
紹介したところ、「本格的に負荷をかけたい(数百〜数千並列、QPS計測、
Grafanaダッシュボードに流す)ならk6をこのラボに統合するのが一番筋が
良いと思います」と回答し、「やってみます」への承諾(「お願いします」)
を受けて実装した。

`tools/virtual_client.py`(既存、少数の仮想クライアントで「どの
バックエンドに振り分けられたか」を可視化するツール)とは目的が違う:

| | `virtual_client.py` | k6統合 |
|---|---|---|
| 目的 | 振り分け/persistence可視化 | 本格的なQPS/レイテンシ負荷試験 |
| 規模 | 数台〜十数台 | 数百〜数千VU(k6のGoランタイムが並列処理) |
| 出力 | テキストサマリー | Prometheus/Grafana連携 |

## 導入

k6はGo製。このサンドボックスでは`go install go.k6.io/k6@latest`で
ビルドできた(`proxy.golang.org`はこの環境のegressポリシーで
ブロックされていない——`download.pytorch.org`/`api.groq.com`等とは
違う扱い)。公式インストーラ(`dl.k6.io`のapt/rpmリポジトリ)は未確認
だが、Goがあればこの方法で十分。

```bash
go install go.k6.io/k6@latest
# ~/go/bin/k6 ができる
```

## `tools/k6_slb_load.js`

```javascript
import http from 'k6/http';
import { check, sleep } from 'k6';

const TARGET_URL = __ENV.TARGET_URL || 'http://127.0.0.1:80/';
const SLEEP_SECONDS = Number(__ENV.SLEEP_SECONDS || 0.1);

export const options = {
  vus: Number(__ENV.VUS || 50),
  duration: __ENV.DURATION || '30s',
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

export default function () {
  const res = http.get(TARGET_URL);
  check(res, { 'status is 200': (r) => r.status === 200 });
  if (SLEEP_SECONDS > 0) sleep(SLEEP_SECONDS);
}
```

`summaryTrendStats`に`p(99)`を明示的に追加しているのは、k6の既定の
集計統計(`avg`/`min`/`med`/`max`/`p(90)`/`p(95)`)には`p(99)`が
含まれず、`tools/prometheus_exporter.py`側がp99を固定で読むため。

## 実行方法

```bash
mkdir -p tools/k6_results
TARGET_URL=http://172.31.0.10:80/index.html VUS=200 DURATION=20s \
  k6 run --summary-export=tools/k6_results/run-$(date +%s).json \
  tools/k6_slb_load.js
```

## Prometheus連携(`tools/prometheus_exporter.py`)

`--k6-results-dir`オプションを追加した。指定したディレクトリ内で
最新の(mtimeが一番新しい)JSONを読み、`netlab_k6_*`メトリクスとして
既存の`/metrics`エンドポイントに追加する——Strixの実行状況
(`netlab_strix_*`)を追加したときと同じ「既存exporterに新しい
データソースをベストエフォートで足す」パターン。新しいscrape jobは
不要(同じ`netlab-emulator`ジョブに相乗りする)。

```bash
python3 tools/prometheus_exporter.py --k6-results-dir tools/k6_results
```

公開されるメトリクス:

- `netlab_k6_has_run` — k6を一度も実行していない場合は`0`(他のメトリクスは出ない)
- `netlab_k6_http_reqs_total` — 直近runの総リクエスト数
- `netlab_k6_http_req_rate` — 直近runのQPS
- `netlab_k6_http_req_failed_rate` — 失敗率(0.0-1.0)
- `netlab_k6_http_req_duration_ms{quantile="avg"|"p95"|"p99"}` — レイテンシ
- `netlab_k6_vus` — 実行したVU数
- `netlab_k6_data_bytes{direction="sent"|"received"}` — 送受信バイト数
- `netlab_k6_last_run_age_seconds` — 直近run完了からの経過秒数

**k6の`--summary-export`のJSON形状はバージョンで変わりうる点に注意**:
k6 v1.8.2で実際に実行して確認したところ、`metrics.<name>`の下に
統計量キー(`count`/`rate`/`avg`/`p(95)`等)が直接並ぶ形だった
(`values`というラッパーは無かった)。将来のバージョンで`values`に
ラップされる形に変わっても動くよう、`build_k6_metrics_text()`は
両方の形に対応している。また`http_req_failed`は他のCounter/Trend系
メトリクスと違い、実際の割合(0.0-1.0)が`"rate"`ではなく`"value"`
キーに入る(k6のRateメトリクス特有の形)。

## Grafanaアラート(`tools/monitoring_stack/alert_rules.yml`)

Strixの時と同じ「`has_run`でゲートして未実行時の誤発火を防ぐ」
パターンで追加:

- `K6LoadTestHighErrorRate` — 直近runのHTTPエラー率が1%超
- `K6LoadTestHighLatencyP95` — 直近runのp95レイテンシが500ms超

## ライブ検証

ALB(`k6alb`、VIP `172.31.0.10:80`)+2台のpc装置に実`http.server`
(`k6web1`=10.94.0.1、`k6web2`=10.94.0.2、ポート9600)を立て、
`k6 run --summary-export=...`で実際に負荷をかけた。

**実装の不整合を発見・修正**: 最初に200 VU・20秒で実行したところ、
失敗率4.73%・p95レイテンシ4.04秒という明らかに悪い結果が出た。
調べると`engine/mock_http_server.py`が単純な`HTTPServer`(シングル
スレッド、1リクエストずつ直列処理)を使っていた——実機の
`python3 -m http.server`はPython 3.7以降、既定で`ThreadingHTTPServer`
(1リクエスト=1スレッド)を使うため、このエミュレータの「実機通りの
コマンドで本物のhttp.serverを立てる」という前提と食い違っていた。
`ThreadingHTTPServer`に修正して再実行:

```
VUS=200, DURATION=20s:
  http_req_failed: rate=4.26% (修正前4.73%から改善したが依然高い)
  http_req_duration: p(95)=4.01s p(99)=5.05s
```

ほとんど変わらなかった。原因を`engine/alb_lb.py`の`AlbListenerProxy`
を見て特定: 接続ごとに`accept`スレッド+双方向リレー用の`_pump`
スレッド2本を生成する設計(Pythonの素朴なthread-per-connection実装)
のため、200同時接続では600本以上のスレッドが同時に動き、CPythonの
GIL競合そのものがボトルネックになっていた——バックエンド側の
スレッディングの問題ではなく、**このエミュレータの実プロキシ実装
(Python threadベース)の本質的なスケール限界**だった。

VU数を下げて再確認:

```
VUS=50,  DURATION=15s: failed=0.09%  p95=1.21s p99=2.21s
VUS=20,  DURATION=15s: failed=0.00%  p95=1.08s p99=1.16s  avg=154ms  med=85ms
```

エラー率は同時接続数を抑えれば実機同様ほぼ0%まで下がる一方、p95/p99
レイテンシは20 VUでも1秒前後と高いまま(med=85msとの差が大きい——
一部のリクエストがスレッドスケジューリングの不利を受けて大きく
遅延する、という性質)。**「数百〜数千並列」の本格負荷は、現状の
Python thread-per-connectionなプロキシ実装では正直なところ厳しい**
——これはk6統合そのものの欠陥ではなく、このラボの実トラフィック
中継の実装がそういう設計だから、という誠実な結論。より高い並列度を
試したい場合は、非同期I/O(asyncio)への置き換えが次の一手になる
(今回はスコープ外、別タスクとして切り出す)。

`netlab_k6_*`メトリクスへの反映を実際に確認(VUS=20の実行結果):

```
$ python3 tools/prometheus_exporter.py --k6-results-dir tools/k6_results --port 9878 &
$ curl -s http://127.0.0.1:9878/metrics | grep netlab_k6
netlab_k6_has_run 1
netlab_k6_http_reqs_total 1970
netlab_k6_http_req_rate 123.20922719070965
netlab_k6_http_req_failed_rate 0
netlab_k6_http_req_duration_ms{quantile="avg"} 153.88491067157378
netlab_k6_http_req_duration_ms{quantile="p95"} 1086.1147197
netlab_k6_http_req_duration_ms{quantile="p99"} 1161.4722247699997
netlab_k6_vus 20
netlab_k6_data_bytes{direction="sent"} 157600
netlab_k6_data_bytes{direction="received"} 277770
netlab_k6_last_run_age_seconds 14
```

複数回実行した`tools/k6_results/`内のJSONから、mtimeが一番新しい
ものが正しく選ばれること(古いrunの値に引き戻されないこと)も確認。

## テスト

`tests/test_prometheus_exporter.py`に`build_k6_metrics_text`/
`_load_latest_k6_summary`のテスト(9件)を追加: 未実行時の
`has_run=0`、実行後の各メトリクス反映、percentile毎の個別ラベル行、
送受信バイトの個別ラベル行、`last_run_age_seconds`がファイルの
mtimeを正しく反映すること、`values`ラップ形式への対応、ディレクトリ
未作成/空/不正JSONでの安全なNone フォールバック、複数ファイルから
最新のものを正しく選ぶこと、を固定する。k6バイナリ自体への依存は
無い(回帰テストはサンプルJSONを直接渡すだけで、実k6実行は手動の
ライブ検証に留める——他の外部ツール連携と同じ方針)。

## スコープ外(実装していない)

- k6の`--out experimental-prometheus-rw`によるリアルタイムpush
  (このラボは既存のポーリング型exporterに合わせて「最新の実行結果
  ファイルを読む」方式に揃えた。本番でリアルタイム性が必要なら検討)
- バックエンド別の分散可視化(`tools/virtual_client.py`の役割と重複
  させない、意図的な分担)
- k6のGrafanaダッシュボード(xk6-dashboard)との統合
