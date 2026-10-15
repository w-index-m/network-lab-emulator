// k6を使った本格的な負荷かけスクリプト(ALB/BIG-IPのVIP向け)。
//
// ユーザー依頼「NW slb 負荷かけツールって知ってます？」→「本格的に
// 負荷をかけたい(数百〜数千並列、QPS計測、Grafanaダッシュボードに
// 流す)ならk6をこのラボに統合するのが一番筋が良いと思います」への
// 対応。tools/virtual_client.py(少数の仮想クライアントで「どの
// バックエンドに振り分けられたか」を可視化するツール)とは目的が違い、
// こちらは実際のQPS/レイテンシを計測する本格的な負荷生成が役割。
//
// 使い方:
//   TARGET_URL=http://172.31.0.10:80/index.html VUS=100 DURATION=30s \
//     k6 run --summary-export=tools/k6_results/run-$(date +%s).json \
//     tools/k6_slb_load.js
//
// --summary-export で書き出したJSONを tools/prometheus_exporter.py が
// ポーリングしてPrometheusメトリクス(netlab_k6_*)として公開する
// (--k6-results-dir オプション、docs/k6-load-testing.md参照)。

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
  // tools/prometheus_exporter.pyのbuild_k6_metrics_text()がp(99)を
  // 固定で読むため、既定の集計統計(avg/min/med/max/p(90)/p(95))に
  // p(99)を明示的に追加する。
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

export default function () {
  const res = http.get(TARGET_URL);
  check(res, { 'status is 200': (r) => r.status === 200 });
  if (SLEEP_SECONDS > 0) {
    sleep(SLEEP_SECONDS);
  }
}
