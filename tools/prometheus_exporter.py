#!/usr/bin/env python3
"""
Prometheus Exporter for Network Lab Emulator

このエミュレーター（app.py）の /api/snmp/dashboard を定期的にポーリングし、
Prometheus text exposition format で /metrics に公開する。
Windows/Linux/macOS いずれでも python 標準ライブラリのみで動作する
（追加パッケージ不要）。

使い方:
  # デフォルト: エミュレーターは http://localhost:8000、公開ポートは 9877
  python tools/prometheus_exporter.py

  # エミュレーターが別ホスト/ポートの場合
  python tools/prometheus_exporter.py --emulator-url http://192.168.1.50:8000

  # 公開ポート変更
  python tools/prometheus_exporter.py --port 9877

Prometheus側の prometheus.yml には以下を追加:
  scrape_configs:
    - job_name: 'netlab-emulator'
      static_configs:
        - targets: ['localhost:9877']
      scrape_interval: 15s

Windowsの場合は tools/run_prometheus_exporter.bat から起動できる。
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
import urllib.error

# Windowsのデフォルトコンソール文字コード(cp932)は em dash(—)等の一部
# Unicode文字をエンコードできずクラッシュするため、標準出力をUTF-8に固定する。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_last_payload = {'text': '', 'fetched_at': 0.0, 'error': None}

_DEFAULT_WATCHLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor_watchlist.json')


def _load_watchlist(path: str) -> set:
    """tools/nl_monitor_control.py が書き出す監視対象リストを読み込み、
    (device_id, interface) の集合を返す。ファイルが無い/壊れている場合は
    空集合（=フィルタなし扱い）。ポーリング毎に読み直すので、NLツールで
    追加した対象は次のスクレイプから即座に反映される。"""
    try:
        with open(path, encoding='utf-8') as f:
            entries = json.load(f)
        return {(e['device_id'], e['interface']) for e in entries}
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return set()


def _escape_label(v: str) -> str:
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _fetch_dashboard(emulator_url: str, token: str) -> dict:
    req = urllib.request.Request(f'{emulator_url}/api/snmp/dashboard')
    if token:
        req.add_header('X-Session-Token', token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def _fetch_pentest_runs(emulator_url: str, token: str) -> dict:
    """/pentest操作パネルが使っているのと同じ /api/pentest/runs を叩き、
    Strix(strix-pentest.yml)の直近の実行状況を取得する。"""
    req = urllib.request.Request(f'{emulator_url}/api/pentest/runs')
    if token:
        req.add_header('X-Session-Token', token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def _load_latest_k6_summary(results_dir: str):
    """k6の`--summary-export=<file>.json`で書き出された実行結果の中から
    最新のものを1件読む。ユーザー依頼「NW slb 負荷かけツールって
    知ってます？」→「k6をこのラボに統合」への対応——tools/
    virtual_client.py(少数クライアントでの振り分け可視化)とは別に、
    本格的なQPS/レイテンシ負荷試験の結果をGrafanaへ流すための入口。
    ディレクトリが無い/JSONが1件も無い場合はNoneを返す(k6を一度も
    実行していない状態は実機のStrix未Dispatchと同じくエラーではない)。"""
    try:
        files = [os.path.join(results_dir, f) for f in os.listdir(results_dir)
                 if f.endswith('.json')]
    except FileNotFoundError:
        return None, None
    if not files:
        return None, None
    latest = max(files, key=os.path.getmtime)
    try:
        with open(latest, encoding='utf-8') as f:
            return json.load(f), os.path.getmtime(latest)
    except (json.JSONDecodeError, OSError):
        return None, None


def build_k6_metrics_text(summary, run_mtime) -> str:
    """k6の--summary-export JSONをPrometheusメトリクスにする。
    直近1回分の実行結果をgaugeとして公開する(k6はprometheus自体への
    continuous pushも可能だが、このラボは既存のポーリング型exporterに
    合わせて「最新の実行結果ファイルを読む」方式に揃えた)。
    summaryがNone(一度もk6を実行していない)場合は空文字列を返し、
    SNMP側のメトリクスには影響させない。"""
    lines = []
    lines.append('# HELP netlab_k6_has_run Whether a k6 load-test summary has ever been observed (1) or not (0, e.g. never run yet)')
    lines.append('# TYPE netlab_k6_has_run gauge')
    lines.append(f'netlab_k6_has_run {1 if summary else 0}')
    if not summary:
        return '\n'.join(lines) + '\n'

    # k6の--summary-export JSONは"metrics": {"<name>": {<統計量キー>: ...}}
    # という構造(バージョンによっては"values"でラップされることもある
    # ため両方に対応する——k6 v1.8.2で実際に出力させて確認したところ
    # ラップは無く直接キーが並んでいた)。
    metrics = summary.get('metrics', {})

    def _value(name, key, default=0.0):
        m = metrics.get(name, {})
        if 'values' in m and isinstance(m['values'], dict):
            m = m['values']
        return m.get(key, default)

    reqs_total = _value('http_reqs', 'count', 0)
    reqs_rate = _value('http_reqs', 'rate', 0.0)
    # http_req_failedは"rate"型メトリクスで、要約JSONでは"value"キーに
    # 実際の割合(0.0-1.0)が入る("rate"というキーではない——他のCounter/
    # Trend系メトリクスとはキー名が異なる点に注意)。
    fail_rate = _value('http_req_failed', 'value', 0.0)
    dur_p95 = _value('http_req_duration', 'p(95)', 0.0)
    dur_p99 = _value('http_req_duration', 'p(99)', 0.0)
    dur_avg = _value('http_req_duration', 'avg', 0.0)
    vus_max = _value('vus_max', 'value', _value('vus', 'value', 0))
    data_sent = _value('data_sent', 'count', 0)
    data_recv = _value('data_received', 'count', 0)

    lines.append('# HELP netlab_k6_http_reqs_total Total number of HTTP requests made in the most recent k6 run')
    lines.append('# TYPE netlab_k6_http_reqs_total gauge')
    lines.append(f'netlab_k6_http_reqs_total {reqs_total}')

    lines.append('# HELP netlab_k6_http_req_rate Requests per second (QPS) achieved in the most recent k6 run')
    lines.append('# TYPE netlab_k6_http_req_rate gauge')
    lines.append(f'netlab_k6_http_req_rate {reqs_rate}')

    lines.append('# HELP netlab_k6_http_req_failed_rate Fraction of failed HTTP requests in the most recent k6 run (0.0-1.0)')
    lines.append('# TYPE netlab_k6_http_req_failed_rate gauge')
    lines.append(f'netlab_k6_http_req_failed_rate {fail_rate}')

    lines.append('# HELP netlab_k6_http_req_duration_ms HTTP request duration in milliseconds, by percentile, for the most recent k6 run')
    lines.append('# TYPE netlab_k6_http_req_duration_ms gauge')
    lines.append(f'netlab_k6_http_req_duration_ms{{quantile="avg"}} {dur_avg}')
    lines.append(f'netlab_k6_http_req_duration_ms{{quantile="p95"}} {dur_p95}')
    lines.append(f'netlab_k6_http_req_duration_ms{{quantile="p99"}} {dur_p99}')

    lines.append('# HELP netlab_k6_vus Virtual users configured for the most recent k6 run')
    lines.append('# TYPE netlab_k6_vus gauge')
    lines.append(f'netlab_k6_vus {vus_max}')

    lines.append('# HELP netlab_k6_data_bytes Bytes sent/received by k6 during the most recent run')
    lines.append('# TYPE netlab_k6_data_bytes gauge')
    lines.append(f'netlab_k6_data_bytes{{direction="sent"}} {data_sent}')
    lines.append(f'netlab_k6_data_bytes{{direction="received"}} {data_recv}')

    age = int(time.time() - run_mtime) if run_mtime else -1
    lines.append('# HELP netlab_k6_last_run_age_seconds Seconds since the most recent k6 run completed')
    lines.append('# TYPE netlab_k6_last_run_age_seconds gauge')
    lines.append(f'netlab_k6_last_run_age_seconds {age}')

    return '\n'.join(lines) + '\n'


def build_strix_metrics_text(runs_data: dict) -> str:
    """StrixペンテストのGitHub Actions実行状況をPrometheusメトリクスにする。
    「それを、GrafanaやPrometheusでステータスを取ったらどうなるかな」という
    ユーザー提案への対応——まずは既存の/pentestパネルが表示している情報
    (直近runの成功/失敗/実行中)をそのままメトリクス化するところから。
    GITHUB_TOKEN未設定等で/api/pentest/runsが失敗する場合は空文字列を返し、
    SNMP側のメトリクスには影響させない(ベストエフォート)。"""
    if not runs_data.get('ok'):
        return ''
    runs = runs_data.get('runs', [])
    lines = []
    lines.append('# HELP netlab_strix_has_runs Whether at least one Strix workflow run has ever been observed (1) or not (0, e.g. never dispatched yet)')
    lines.append('# TYPE netlab_strix_has_runs gauge')
    lines.append(f'netlab_strix_has_runs {1 if runs else 0}')

    lines.append('# HELP netlab_strix_run_info Most recent Strix pentest workflow runs (info-style metric, value is always 1)')
    lines.append('# TYPE netlab_strix_run_info gauge')
    for run in runs:
        status = _escape_label(run.get('status', ''))
        conclusion = _escape_label(run.get('conclusion') or 'none')
        lines.append(f'netlab_strix_run_info{{run_id="{run["id"]}",status="{status}",conclusion="{conclusion}"}} 1')

    lines.append('# HELP netlab_strix_last_run_success Whether the most recent completed Strix run succeeded (1) or not (0)')
    lines.append('# TYPE netlab_strix_last_run_success gauge')
    lines.append(f'netlab_strix_last_run_success {1 if runs and runs[0].get("conclusion") == "success" else 0}')

    lines.append('# HELP netlab_strix_last_run_in_progress Whether the most recent Strix run is still queued/running (1) or finished (0)')
    lines.append('# TYPE netlab_strix_last_run_in_progress gauge')
    lines.append(f'netlab_strix_last_run_in_progress {1 if runs and runs[0].get("status") != "completed" else 0}')

    success_count = sum(1 for r in runs if r.get('conclusion') == 'success')
    failure_count = sum(1 for r in runs if r.get('conclusion') == 'failure')
    lines.append('# HELP netlab_strix_recent_runs_total Count of recent Strix runs by conclusion (sliding window of the last few runs returned by /api/pentest/runs, not a true cumulative counter)')
    lines.append('# TYPE netlab_strix_recent_runs_total gauge')
    lines.append(f'netlab_strix_recent_runs_total{{conclusion="success"}} {success_count}')
    lines.append(f'netlab_strix_recent_runs_total{{conclusion="failure"}} {failure_count}')

    # 「失敗した」だけでなく「そもそも長期間実行されていない」にも気づける
    # ように、最後に成功したrunからの経過秒数を公開する(Grafanaの
    # 「成功が無い/停滞している」アラート向け)。成功runが1件も無い場合は
    # -1を返す(「0」は紛らわしい——たった今成功した場合と区別できない)。
    success_runs = [r for r in runs if r.get('conclusion') == 'success']
    lines.append('# HELP netlab_strix_last_success_age_seconds Seconds since the most recent successful Strix run completed (-1 if there has never been a successful run)')
    lines.append('# TYPE netlab_strix_last_success_age_seconds gauge')
    age = -1
    if success_runs:
        try:
            # created_at(Dispatch時刻)ではなくupdated_at(completedに
            # なった時点でも更新される)を使う——createdのままだと、
            # 実行時間の分だけ経過秒数が実際より長く出てしまう。
            ts = success_runs[0].get('updated_at') or success_runs[0]['created_at']
            completed = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            age = int((datetime.datetime.now(datetime.timezone.utc) - completed).total_seconds())
        except (KeyError, ValueError):
            age = -1
    lines.append(f'netlab_strix_last_success_age_seconds {age}')

    return '\n'.join(lines) + '\n'


def build_metrics_text(data: dict, watchlist: set = frozenset()) -> str:
    """/api/snmp/dashboard のJSONをPrometheus text exposition formatに変換。

    仕様上、同一メトリック名のサンプルは連続していなければならない
    （装置ごとに混在させてはいけない）ため、メトリック単位で全装置分を
    まとめて出力する。"""
    devices = data.get('devices', [])

    def base_labels(d):
        dev = _escape_label(d['device_id'])
        host = _escape_label(d.get('hostname', d['device_id']))
        dtype = _escape_label(d.get('type', ''))
        return f'device_id="{dev}",hostname="{host}",type="{dtype}"'

    lines = []

    def block(name, help_text, mtype, sample_lines):
        lines.append(f'# HELP {name} {help_text}')
        lines.append(f'# TYPE {name} {mtype}')
        lines.extend(sample_lines)

    block('netlab_device_up', '装置がSNMPエージェントに登録されているか(常時1)', 'gauge',
          [f'netlab_device_up{{{base_labels(d)}}} 1' for d in devices])

    block('netlab_device_health', '1=HEALTHY(全IF up), 0=ATTENTION(いずれかdown)', 'gauge', [
        f'netlab_device_health{{{base_labels(d)}}} '
        f'{0 if any(i["oper_status"] != 1 for i in d.get("interfaces", [])) else 1}'
        for d in devices
    ])

    block('netlab_sys_uptime_seconds', 'sysUpTime(秒)', 'gauge', [
        f'netlab_sys_uptime_seconds{{{base_labels(d)}}} {d.get("sys_uptime_ticks", 0) / 100.0:.2f}'
        for d in devices
    ])

    cpu_samples = [f'netlab_cpu_percent{{{base_labels(d)}}} {d["cpu_percent"]}'
                   for d in devices if d.get('cpu_percent') is not None]
    if cpu_samples:
        block('netlab_cpu_percent', 'CPU使用率(CISCO-PROCESS-MIB相当、対応機種のみ)', 'gauge',
              cpu_samples)

    block('netlab_route_count', 'RIBの最良経路数(rib_engine.get_best_routes)', 'gauge', [
        f'netlab_route_count{{{base_labels(d)}}} {d["route_count"]}'
        for d in devices if d.get('route_count') is not None
    ])

    def iface_labels(d, iface):
        return f'{base_labels(d)},interface="{_escape_label(iface["descr"])}"'

    block('netlab_interface_admin_status', 'ifAdminStatus(1=up,2=down)', 'gauge', [
        f'netlab_interface_admin_status{{{iface_labels(d, i)}}} {i["admin_status"]}'
        for d in devices for i in d.get('interfaces', [])
    ])
    block('netlab_interface_oper_status', 'ifOperStatus(1=up,2=down)', 'gauge', [
        f'netlab_interface_oper_status{{{iface_labels(d, i)}}} {i["oper_status"]}'
        for d in devices for i in d.get('interfaces', [])
    ])
    block('netlab_interface_in_octets_total', 'ifInOctets(累積カウンタ)', 'counter', [
        f'netlab_interface_in_octets_total{{{iface_labels(d, i)}}} {i["in_octets"]}'
        for d in devices for i in d.get('interfaces', [])
    ])
    block('netlab_interface_out_octets_total', 'ifOutOctets(累積カウンタ)', 'counter', [
        f'netlab_interface_out_octets_total{{{iface_labels(d, i)}}} {i["out_octets"]}'
        for d in devices for i in d.get('interfaces', [])
    ])
    block('netlab_interface_speed_bps', 'ifSpeed(bps)', 'gauge', [
        f'netlab_interface_speed_bps{{{iface_labels(d, i)}}} {i["speed"]}'
        for d in devices for i in d.get('interfaces', [])
    ])

    watchlist_samples = [
        f'netlab_watchlist_target{{{iface_labels(d, i)}}} 1'
        for d in devices for i in d.get('interfaces', [])
        if (d['device_id'], i['descr']) in watchlist
    ]
    if watchlist_samples:
        block('netlab_watchlist_target',
              'tools/nl_monitor_control.py で自然言語指示により監視対象登録された場合1', 'gauge',
              watchlist_samples)

    return '\n'.join(lines) + '\n'


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # アクセスログを抑制（標準出力を汚さない）

    def do_GET(self):
        if self.path.rstrip('/') in ('', '/metrics'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            self.wfile.write(_last_payload['text'].encode('utf-8'))
        elif self.path == '/healthz':
            ok = _last_payload['error'] is None
            self.send_response(200 if ok else 503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'ok': ok, 'error': _last_payload['error'],
                'fetched_at': _last_payload['fetched_at'],
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()


def poll_loop(emulator_url: str, token: str, interval: int, watchlist_path: str, include_strix: bool = True,
              k6_results_dir: str = None):
    while True:
        try:
            data = _fetch_dashboard(emulator_url, token)
            watchlist = _load_watchlist(watchlist_path)
            text = build_metrics_text(data, watchlist)
            # Strixの実行状況はベストエフォート: GITHUB_TOKEN未設定等で
            # /api/pentest/runsが失敗しても、SNMP側のメトリクスは
            # 正常に公開し続ける(片方の失敗でexporter全体を止めない)。
            if include_strix:
                try:
                    strix_data = _fetch_pentest_runs(emulator_url, token)
                    text += build_strix_metrics_text(strix_data)
                except Exception as e:
                    print(f'[exporter] Strix実行状況の取得失敗(SNMP側には影響なし): {e}')
            # k6の負荷試験結果もベストエフォート(ローカルファイル読み取りの
            # みなので失敗要因は少ないが、他と同じ方針で揃える)。
            if k6_results_dir:
                try:
                    summary, mtime = _load_latest_k6_summary(k6_results_dir)
                    text += build_k6_metrics_text(summary, mtime)
                except Exception as e:
                    print(f'[exporter] k6結果の取得失敗(SNMP側には影響なし): {e}')
            _last_payload['text'] = text
            _last_payload['error'] = None
            _last_payload['fetched_at'] = time.time()
        except urllib.error.HTTPError as e:
            _last_payload['error'] = f'HTTP {e.code}'
            print(f'[exporter] エミュレーターからの取得失敗: HTTP {e.code}'
                  f'{" — ログインが必要です (--token を指定)" if e.code == 401 else ""}')
        except Exception as e:
            _last_payload['error'] = str(e)
            print(f'[exporter] エミュレーターへの接続失敗: {e}')
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description='Network Lab Emulator 用 Prometheus Exporter')
    parser.add_argument('--emulator-url', default='http://localhost:8000',
                        help='エミュレーターのURL(既定: http://localhost:8000)')
    parser.add_argument('--token', default='', help='エミュレーターのセッショントークン(認証有効時)')
    parser.add_argument('--port', type=int, default=9877, help='Exporterの公開ポート(既定: 9877)')
    parser.add_argument('--bind', default='0.0.0.0', help='待ち受けIP')
    parser.add_argument('--interval', type=int, default=15, help='ポーリング間隔(秒)')
    parser.add_argument('--watchlist', default=_DEFAULT_WATCHLIST,
                        help='tools/nl_monitor_control.py の監視対象リストJSON'
                             f'(既定: {_DEFAULT_WATCHLIST})')
    parser.add_argument('--no-strix', action='store_true',
                        help='/api/pentest/runs(Strixペンテストの実行状況)のポーリングを無効化する')
    parser.add_argument('--k6-results-dir', default=None,
                        help='k6の--summary-exportで書き出したJSONを置くディレクトリ'
                             '(既定: 無効。指定するとtools/k6_slb_load.jsの実行結果を'
                             'netlab_k6_*メトリクスとして公開する)')
    args = parser.parse_args()

    print('\n' + '=' * 70)
    print('Network Lab Emulator — Prometheus Exporter')
    print('=' * 70)
    print(f'  エミュレーター  : {args.emulator_url}')
    print(f'  公開エンドポイント: http://{args.bind}:{args.port}/metrics')
    print(f'  ポーリング間隔  : {args.interval}秒')
    print('=' * 70)

    import threading
    t = threading.Thread(target=poll_loop,
                         args=(args.emulator_url, args.token, args.interval, args.watchlist,
                               not args.no_strix, args.k6_results_dir),
                         daemon=True)
    t.start()

    # 起動直後の最初の取得を待つ（すぐ/metricsを叩かれても空でないように）
    time.sleep(min(2, args.interval))

    server = ThreadingHTTPServer((args.bind, args.port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n終了します')
        server.shutdown()


if __name__ == '__main__':
    main()
