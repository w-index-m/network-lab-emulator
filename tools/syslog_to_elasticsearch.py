#!/usr/bin/env python3
"""
syslog(UDP) -> Elasticsearch ブリッジ

network-lab-emulatorの各装置は `logging host <IP>` を設定すると
実際にRFC3164形式のsyslogをUDPで送信してくる(engine/syslog_sender.py)。
これをElasticsearchの _bulk API（NDJSON: action行+source行の繰り返し）
に変換して転送する。日付ごとのインデックス（`netlab-syslog-YYYY.MM.DD`）
に書き込むので、Kibanaのインデックスパターン`netlab-syslog-*`で
そのまま検索・可視化できる。

`tools/syslog_to_loki.py`のElasticsearch版。Lokiがラベルだけ索引化する
軽量設計なのに対し、Elasticsearchはメッセージ本文も含めて全文索引化する
ため、任意のキーワードでの横断検索やKibanaでの集計に向く。

使い方:
  python tools/syslog_to_elasticsearch.py \
      --syslog-port 5514 --es-url http://localhost:9200

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
from datetime import datetime, timezone

_SYSLOG_RE = re.compile(r'^<(\d+)>(.*)$', re.S)
# 装置側のsyslog_sender.pyが出すメッセージ例:
#   *Sep 01 23:20:46.906: %LINK-3-UPDOWN: Interface Gi0/0/1, changed state to down
_FACILITY_TAG_RE = re.compile(r'%(\w[\w-]*)-(\d)-')

_SEVERITY_NAME = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug']


def parse_syslog(data: bytes, addr) -> dict:
    text = data.decode('utf-8', errors='replace')
    m = _SYSLOG_RE.match(text)
    pri = int(m.group(1)) if m else 14
    body = m.group(2) if m else text
    facility = pri // 8
    severity = pri % 8

    tag_m = _FACILITY_TAG_RE.search(body)
    facility_tag = tag_m.group(1) if tag_m else 'SYSLOG'
    severity_from_msg = int(tag_m.group(2)) if tag_m else severity

    return {
        'source_ip': addr[0],
        'facility': facility,
        'severity': severity_from_msg,
        'severity_name': _SEVERITY_NAME[severity_from_msg]
                         if 0 <= severity_from_msg < len(_SEVERITY_NAME) else 'unknown',
        'facility_tag': facility_tag,
        'message': body.strip(),
    }


def index_name(prefix: str, when: datetime) -> str:
    """Elasticsearchの日次インデックス命名規則(YYYY.MM.DD区切り)。"""
    return f'{prefix}-{when.strftime("%Y.%m.%d")}'


def build_bulk_payload(prefix: str, entry: dict, when: datetime = None) -> bytes:
    """_bulk APIのNDJSON(action行+source行)を1件分組み立てる。

    複数件をまとめて送る場合は、この関数の出力を連結すればよい
    （_bulkは1リクエストに複数ドキュメントを積める設計）。
    """
    when = when or datetime.now(timezone.utc)
    action = {'index': {'_index': index_name(prefix, when)}}
    source = {
        '@timestamp': when.isoformat(),
        'source_ip': entry['source_ip'],
        'facility': entry['facility'],
        'severity': entry['severity'],
        'severity_name': entry['severity_name'],
        'facility_tag': entry['facility_tag'],
        'message': entry['message'],
    }
    return (json.dumps(action) + '\n' + json.dumps(source) + '\n').encode('utf-8')


def push_to_elasticsearch(es_url: str, prefix: str, entry: dict) -> int:
    body = build_bulk_payload(prefix, entry)
    req = urllib.request.Request(
        f'{es_url}/_bulk',
        data=body,
        headers={'Content-Type': 'application/x-ndjson'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def main():
    parser = argparse.ArgumentParser(description='syslog(UDP) -> Elasticsearch ブリッジ')
    parser.add_argument('--bind', default='0.0.0.0', help='syslog待ち受けIP')
    parser.add_argument('--syslog-port', type=int, default=5514,
                        help='syslog待ち受けポート(既定: 5514、root権限不要にするため514ではない)')
    parser.add_argument('--es-url', default='http://localhost:9200', help='ElasticsearchのURL')
    parser.add_argument('--index-prefix', default='netlab-syslog',
                        help='インデックス名の接頭辞(既定: netlab-syslog -> netlab-syslog-2026.09.19)')
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.syslog_port))

    print('\n' + '=' * 70)
    print('syslog -> Elasticsearch ブリッジ')
    print('=' * 70)
    print(f'  待ち受け      : udp://{args.bind}:{args.syslog_port}')
    print(f'  転送先        : {args.es_url}')
    print(f'  インデックス  : {args.index_prefix}-YYYY.MM.DD')
    print(f'  Kibanaでの索引パターン例: {args.index_prefix}-*')
    print('=' * 70 + '\n')

    while True:
        data, addr = sock.recvfrom(65535)
        entry = parse_syslog(data, addr)
        try:
            status = push_to_elasticsearch(args.es_url, args.index_prefix, entry)
            print(f'[{entry["source_ip"]}] {entry["message"][:100]} -> ES({status})')
        except Exception as e:
            print(f'[{entry["source_ip"]}] Elasticsearch転送失敗: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
