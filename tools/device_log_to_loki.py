#!/usr/bin/env python3
"""
装置ログ(show logging) -> Grafana Loki 転送ツール

network-lab-emulatorの `logging host` 設定による実UDP syslog送信は、
現状OSPF/RIP/BGP/STPログ等の一部イベントのみが対象で、CLIの
`shutdown`/`no shutdown`で出る%LINK-3-UPDOWNは実送信パイプラインに
乗らない（`show logging`の内部バッファには記録されるが、実UDP送信は
されない）。この制約を回避するため、対象装置の`show logging`を
定期的にポーリングし、新規に増えた行だけをLokiにpushする。

使い方:
  python tools/device_log_to_loki.py --devices catalyst,nexus --interval 3
"""

import argparse
import json
import sys
import time
import urllib.request


class EmulatorClient:
    def __init__(self, base_url: str, token: str = ''):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def cli(self, device_id: str, command: str) -> str:
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['X-Session-Token'] = self.token
        req = urllib.request.Request(
            f'{self.base_url}/api/cli',
            data=json.dumps({'device_id': device_id, 'command': command}).encode(),
            headers=headers, method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('output', '')

    def get_hostname(self, device_id: str) -> str:
        req = urllib.request.Request(f'{self.base_url}/api/snmp/dashboard')
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        for d in data.get('devices', []):
            if d['device_id'] == device_id:
                return d.get('hostname', device_id)
        return device_id


def push_to_loki(loki_url: str, device_id: str, hostname: str, line: str):
    now_ns = str(time.time_ns())
    payload = {
        'streams': [{
            'stream': {'job': 'netlab-device-log', 'device_id': device_id, 'hostname': hostname},
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


def extract_log_lines(show_logging_output: str) -> list:
    """`show logging`の出力からタイムスタンプ付きログ行のみ抽出
    (ヘッダのサマリー行は除外)"""
    lines = []
    for line in show_logging_output.splitlines():
        s = line.strip()
        if s.startswith('*'):  # 例: *Sep 01 23:20:46.906: %LINK-3-UPDOWN: ...
            lines.append(s)
    return lines


def main():
    parser = argparse.ArgumentParser(description='装置のshow loggingをLokiへ転送')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーターURL')
    parser.add_argument('--token', default='', help='セッショントークン')
    parser.add_argument('--loki-url', default='http://localhost:3100', help='LokiのURL')
    parser.add_argument('--devices', required=True, help='カンマ区切りの対象デバイスID(例: catalyst,nexus)')
    parser.add_argument('--interval', type=float, default=3.0, help='ポーリング間隔(秒)')
    args = parser.parse_args()

    client = EmulatorClient(args.url, args.token)
    devices = [d.strip() for d in args.devices.split(',') if d.strip()]
    hostnames = {d: client.get_hostname(d) for d in devices}
    seen = {d: set() for d in devices}

    print('\n' + '=' * 70)
    print('装置ログ -> Loki 転送ツール')
    print('=' * 70)
    print(f'  対象装置: {", ".join(devices)}')
    print(f'  転送先  : {args.loki_url}')
    print('=' * 70 + '\n')

    while True:
        for device_id in devices:
            try:
                out = client.cli(device_id, 'show logging')
            except Exception as e:
                print(f'[{device_id}] show logging取得失敗: {e}', file=sys.stderr)
                continue
            for line in extract_log_lines(out):
                if line in seen[device_id]:
                    continue
                seen[device_id].add(line)
                try:
                    status = push_to_loki(args.loki_url, device_id, hostnames[device_id], line)
                    print(f'[{device_id}] {line[:100]} -> Loki({status})')
                except Exception as e:
                    print(f'[{device_id}] Loki転送失敗: {e}', file=sys.stderr)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
