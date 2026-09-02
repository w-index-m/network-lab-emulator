#!/usr/bin/env python3
"""
syslog(UDP) -> Grafana Loki ブリッジ

network-lab-emulatorの各装置は `logging host <IP>` を設定すると
実際にRFC3164形式のsyslogをUDPで送信してくる(engine/syslog_sender.py)。
これをLokiが理解できる/loki/api/v1/push形式に変換して転送する。

`tools/syslog_ai_monitor.py`（AI要約付き）とは別に、こちらは
「Lokiに投入してLogQLで検索できるようにする」ことだけに特化した
軽量ブリッジ。

使い方:
  python tools/syslog_to_loki.py --syslog-port 5514 --loki-url http://localhost:3100

各装置側では:
  configure terminal
  logging host <このブリッジを動かすホストのIP>
"""

import argparse
import json
import re
import socket
import sys
import time
import urllib.request

_SYSLOG_RE = re.compile(r'^<(\d+)>(.*)$', re.S)
# 装置側のsyslog_sender.pyが出すメッセージ例:
#   *Sep 01 23:20:46.906: %LINK-3-UPDOWN: Interface Gi0/0/1, changed state to down
_HOSTNAME_HINT_RE = re.compile(r'%(\w[\w-]*)-(\d)-')


def parse_syslog(data: bytes, addr) -> dict:
    text = data.decode('utf-8', errors='replace')
    m = _SYSLOG_RE.match(text)
    pri = int(m.group(1)) if m else 14
    body = m.group(2) if m else text
    facility = pri // 8
    severity = pri % 8

    sev_m = _HOSTNAME_HINT_RE.search(body)
    facility_tag = sev_m.group(1) if sev_m else 'SYSLOG'
    severity_from_msg = int(sev_m.group(2)) if sev_m else severity

    return {
        'source_ip': addr[0],
        'facility': facility,
        'severity': severity_from_msg,
        'facility_tag': facility_tag,
        'message': body.strip(),
    }


def push_to_loki(loki_url: str, entry: dict):
    now_ns = str(time.time_ns())
    payload = {
        'streams': [{
            'stream': {
                'job': 'netlab-syslog',
                'source_ip': entry['source_ip'],
                'facility_tag': entry['facility_tag'],
                'severity': str(entry['severity']),
            },
            'values': [[now_ns, entry['message']]],
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
    parser = argparse.ArgumentParser(description='syslog(UDP) -> Grafana Loki ブリッジ')
    parser.add_argument('--bind', default='0.0.0.0', help='syslog待ち受けIP')
    parser.add_argument('--syslog-port', type=int, default=5514,
                        help='syslog待ち受けポート(既定: 5514、root権限不要にするため514ではない)')
    parser.add_argument('--loki-url', default='http://localhost:3100', help='LokiのURL')
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.syslog_port))

    print('\n' + '=' * 70)
    print('syslog -> Loki ブリッジ')
    print('=' * 70)
    print(f'  待ち受け: udp://{args.bind}:{args.syslog_port}')
    print(f'  転送先  : {args.loki_url}')
    print('=' * 70 + '\n')

    while True:
        data, addr = sock.recvfrom(65535)
        entry = parse_syslog(data, addr)
        try:
            status = push_to_loki(args.loki_url, entry)
            print(f'[{entry["source_ip"]}] {entry["message"][:100]} -> Loki({status})')
        except Exception as e:
            print(f'[{entry["source_ip"]}] Loki転送失敗: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
