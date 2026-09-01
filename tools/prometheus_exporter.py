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
import json
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_last_payload = {'text': '', 'fetched_at': 0.0, 'error': None}


def _escape_label(v: str) -> str:
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _fetch_dashboard(emulator_url: str, token: str) -> dict:
    req = urllib.request.Request(f'{emulator_url}/api/snmp/dashboard')
    if token:
        req.add_header('X-Session-Token', token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def build_metrics_text(data: dict) -> str:
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


def poll_loop(emulator_url: str, token: str, interval: int):
    while True:
        try:
            data = _fetch_dashboard(emulator_url, token)
            _last_payload['text'] = build_metrics_text(data)
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
    args = parser.parse_args()

    print('\n' + '=' * 70)
    print('Network Lab Emulator — Prometheus Exporter')
    print('=' * 70)
    print(f'  エミュレーター  : {args.emulator_url}')
    print(f'  公開エンドポイント: http://{args.bind}:{args.port}/metrics')
    print(f'  ポーリング間隔  : {args.interval}秒')
    print('=' * 70)

    import threading
    t = threading.Thread(target=poll_loop, args=(args.emulator_url, args.token, args.interval),
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
