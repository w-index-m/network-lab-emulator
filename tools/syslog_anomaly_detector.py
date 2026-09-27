#!/usr/bin/env python3
"""
Grafana Lokiに溜まったsyslogの「メッセージ頻度」に対する異常検知。

tools/anomaly_autoencoder.py はCPU/トラフィックの2次元データに
Autoencoderを使うが、ここで見たいのは「1分あたりの受信ログ件数」という
単一の時系列だけなので、Autoencoderは過剰（1次元の再構成誤差は
実質ただの標準化された絶対偏差になり、学習コストだけ増える）。
そのため学習データ（平常時のログ件数）の平均・標準偏差から
しきい値を決める、同じ「普段のふるまいを学習してから外れを見る」
という考え方を、1次元向けに単純化した実装にしている。

検知結果は tools/network_ontology_query.py の
summarize_logs_via_ollama() をそのまま再利用してAI要約できる
（--summarize指定時。Ollamaが無ければ何もせずNoneのまま）。

使い方:
  # 直近30分を1分バケットに分割し、装置ごとのログ件数の急増を検知
  python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
      --window-minutes 30 --bucket-seconds 60

  # 検知した急増区間のログをOllamaで要約
  python tools/syslog_anomaly_detector.py --loki-url http://localhost:3100 \
      --window-minutes 30 --summarize
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tools.network_ontology_query import summarize_logs_via_ollama  # noqa: E402


def fetch_syslog_entries(loki_url: str, start_epoch: float, end_epoch: float,
                          limit: int = 5000) -> list[dict]:
    """{job="netlab-syslog"} の生ログ全件を(タイムスタンプ, source_ip, 本文)で取得する。"""
    query = '{job="netlab-syslog"}'
    qs = urllib.parse.urlencode({
        'query': query,
        'start': str(int(start_epoch * 1e9)),
        'end': str(int(end_epoch * 1e9)),
        'limit': str(limit),
        'direction': 'forward',
    })
    with urllib.request.urlopen(
            f'{loki_url}/loki/api/v1/query_range?{qs}', timeout=10) as r:
        data = json.loads(r.read())
    entries = []
    for stream in data.get('data', {}).get('result', []):
        source_ip = stream.get('stream', {}).get('source_ip', 'unknown')
        for ts_ns, line in stream.get('values', []):
            entries.append({
                'ts': int(ts_ns) / 1e9,
                'source_ip': source_ip,
                'line': line,
            })
    entries.sort(key=lambda e: e['ts'])
    return entries


def bucket_counts(entries: list[dict], start_epoch: float, end_epoch: float,
                   bucket_seconds: int) -> dict[str, np.ndarray]:
    """装置(source_ip)ごとに、時間バケット単位のログ件数の配列を作る。"""
    n_buckets = max(1, int((end_epoch - start_epoch) // bucket_seconds) + 1)
    per_device: dict[str, np.ndarray] = defaultdict(
        lambda: np.zeros(n_buckets, dtype=float))
    for e in entries:
        idx = int((e['ts'] - start_epoch) // bucket_seconds)
        if 0 <= idx < n_buckets:
            per_device[e['source_ip']][idx] += 1
    return dict(per_device)


class LogRateAnomalyModel:
    """1装置ぶんの「普段のログ件数/バケット」を学習し、急増を検知する。

    Autoencoderの復元誤差の代わりに、平常時の平均・標準偏差からの
    偏差(zスコア)を「異常度」として使う。しきい値の考え方（平均 +
    threshold_k * 標準偏差）はtools/anomaly_autoencoder.pyと共通。
    """

    def __init__(self):
        self._mean = 0.0
        self._std = 0.0
        self._threshold_k = 4.0

    def fit(self, baseline_counts: np.ndarray, threshold_k: float = 4.0) -> None:
        if baseline_counts.size < 2:
            raise ValueError('学習には最低2バケット必要')
        self._mean = float(np.mean(baseline_counts))
        self._std = float(np.std(baseline_counts))
        self._threshold_k = threshold_k

    def score(self, count: float) -> float:
        """zスコア（標準偏差ゼロの場合は平均超過分をそのままスコアにする）。"""
        if self._std < 1e-9:
            return max(0.0, count - self._mean)
        return (count - self._mean) / self._std

    def is_anomaly(self, count: float) -> bool:
        return self.score(count) > self._threshold_k

    @property
    def baseline(self) -> tuple[float, float]:
        return self._mean, self._std


def detect_spikes(per_device_counts: dict[str, np.ndarray],
                   baseline_fraction: float = 0.5,
                   threshold_k: float = 4.0) -> dict[str, dict]:
    """各装置について、前半をベースライン学習に使い、
    後半（直近側）のバケットで急増していないか判定する。"""
    results = {}
    for device_ip, counts in per_device_counts.items():
        n = len(counts)
        split = max(2, int(n * baseline_fraction))
        baseline = counts[:split]
        recent = counts[split:]
        if baseline.size < 2 or recent.size == 0:
            continue
        model = LogRateAnomalyModel()
        model.fit(baseline, threshold_k=threshold_k)
        recent_scores = [model.score(c) for c in recent]
        peak_idx = int(np.argmax(recent_scores))
        peak_count = float(recent[peak_idx])
        peak_score = recent_scores[peak_idx]
        mean, std = model.baseline
        results[device_ip] = {
            'baseline_mean': mean,
            'baseline_std': std,
            'peak_bucket_offset': split + peak_idx,
            'peak_count': peak_count,
            'peak_score': peak_score,
            'is_anomaly': model.is_anomaly(peak_count),
        }
    return results


def main():
    p = argparse.ArgumentParser(
        description='Grafana Lokiのsyslog頻度に対する異常検知')
    p.add_argument('--loki-url', default='http://localhost:3100')
    p.add_argument('--window-minutes', type=float, default=30,
                    help='遡って見る時間範囲(分)')
    p.add_argument('--bucket-seconds', type=int, default=60,
                    help='時間バケットの幅(秒)')
    p.add_argument('--threshold-k', type=float, default=4.0,
                    help='異常判定のしきい値(平均+k*標準偏差)')
    p.add_argument('--summarize', action='store_true',
                    help='急増を検知した装置について、該当区間のログをOllamaで要約')
    args = p.parse_args()

    end_epoch = time.time()
    start_epoch = end_epoch - args.window_minutes * 60

    entries = fetch_syslog_entries(args.loki_url, start_epoch, end_epoch)
    if not entries:
        print('該当期間にsyslogが1件もありません。')
        return

    per_device = bucket_counts(entries, start_epoch, end_epoch,
                                args.bucket_seconds)
    results = detect_spikes(per_device, threshold_k=args.threshold_k)

    any_anomaly = False
    for device_ip, r in sorted(results.items()):
        flag = '🚨 急増検知' if r['is_anomaly'] else '✅ 正常範囲'
        print(f'{flag}  {device_ip}: 直近ピーク {r["peak_count"]:.0f}件/'
              f'{args.bucket_seconds}秒 '
              f'(平常時 平均{r["baseline_mean"]:.1f}±{r["baseline_std"]:.1f}件, '
              f'スコア={r["peak_score"]:.2f})')
        if r['is_anomaly']:
            any_anomaly = True
            if args.summarize:
                bucket_start = (start_epoch
                                 + r['peak_bucket_offset'] * args.bucket_seconds)
                bucket_end = bucket_start + args.bucket_seconds
                window_lines = [
                    e['line'] for e in entries
                    if e['source_ip'] == device_ip
                    and bucket_start <= e['ts'] < bucket_end
                ]
                fake_alert = {
                    'severityLabel': 'warning',
                    'deviceDisplayName': device_ip,
                    'resourceTemplateName': 'Syslog Rate Anomaly',
                    'instanceName': f'{r["peak_count"]:.0f} logs/{args.bucket_seconds}s',
                }
                summary = summarize_logs_via_ollama(fake_alert, window_lines)
                if summary:
                    print(f'      AI要約: {summary}')
                else:
                    print('      AI要約: (Ollama未接続または要約失敗)')

    if not any_anomaly:
        print('\n急増は検知されませんでした。')


if __name__ == '__main__':
    main()
