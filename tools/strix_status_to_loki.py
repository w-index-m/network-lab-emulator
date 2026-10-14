#!/usr/bin/env python3
"""
Strix(strix-pentest.yml)ペンテスト結果 -> Grafana Loki 転送ツール

ユーザー提案「GrafanaアラートやGrafana Lokiと連携してほしい」への対応。
/pentest操作パネル・tools/prometheus_exporter.pyが使っているのと同じ
`/api/pentest/runs`をポーリングし、新しく完了したrunだけをログ行として
Lokiにpushする——tools/device_log_to_loki.pyの「新規に増えた行だけ
転送する」という重複防止パターンをそのまま踏襲した。

実行中(in_progress/queued)のrunはまだ結論が出ていないため送らず、
completedになった時点で一度だけ転送する。

使い方:
  python tools/strix_status_to_loki.py --url http://localhost:8000 \
      --loki-url http://localhost:3100 --interval 30
"""

import argparse
import json
import sys
import time
import urllib.request


def _fetch_runs(url: str, token: str, limit: int = 5) -> list:
    req = urllib.request.Request(f'{url}/api/pentest/runs?limit={limit}')
    if token:
        req.add_header('X-Session-Token', token)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode('utf-8'))
    return data.get('runs', []) if data.get('ok') else []


def new_completed_runs(runs: list, seen_ids: set) -> list:
    """未転送(seen_idsに無い)かつstatus==completedのrunだけを返す
    (古いもの順に転送できるよう、GitHub APIの新しい順を反転する)。"""
    return [r for r in reversed(runs)
            if r['id'] not in seen_ids and r.get('status') == 'completed']


def format_log_line(run: dict) -> str:
    conclusion = run.get('conclusion') or 'unknown'
    return (f"strix-pentest run #{run['id']} completed: conclusion={conclusion} "
            f"url={run.get('html_url', '')}")


def push_to_loki(loki_url: str, run: dict, line: str) -> int:
    now_ns = str(time.time_ns())
    payload = {
        'streams': [{
            'stream': {'job': 'netlab-strix-pentest',
                       'conclusion': run.get('conclusion') or 'unknown'},
            'values': [[now_ns, line]],
        }]
    }
    req = urllib.request.Request(
        f'{loki_url}/loki/api/v1/push',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def main():
    parser = argparse.ArgumentParser(description='Strixペンテスト結果をLokiへ転送')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーターURL')
    parser.add_argument('--token', default='', help='セッショントークン(認証有効時)')
    parser.add_argument('--loki-url', default='http://localhost:3100', help='LokiのURL')
    parser.add_argument('--interval', type=float, default=30.0, help='ポーリング間隔(秒)')
    args = parser.parse_args()

    seen_ids = set()
    print('\n' + '=' * 70)
    print('Strixペンテスト結果 -> Loki 転送ツール')
    print('=' * 70)
    print(f'  エミュレーター: {args.url}')
    print(f'  転送先        : {args.loki_url}')
    print('=' * 70 + '\n')

    while True:
        try:
            runs = _fetch_runs(args.url, args.token)
        except Exception as e:
            print(f'/api/pentest/runs取得失敗: {e}', file=sys.stderr)
            time.sleep(args.interval)
            continue
        for run in new_completed_runs(runs, seen_ids):
            seen_ids.add(run['id'])
            line = format_log_line(run)
            try:
                status = push_to_loki(args.loki_url, run, line)
                print(f'run #{run["id"]} -> Loki({status}): {line}')
            except Exception as e:
                print(f'Loki転送失敗(run #{run["id"]}): {e}', file=sys.stderr)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
