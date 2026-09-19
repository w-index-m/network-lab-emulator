#!/usr/bin/env python3
"""
LogicMonitor Collector 相当のブリッジ（syslog + SNMP Trap受信）

実際のLogicMonitor Collectorは監視対象の装置からsyslog/SNMP Trapを
受信し、LogicMonitorのクラウド側へ独自の内部プロトコルで転送する
（公開されているREST API v3のエンドポイントではない）。このツールは
その役割をエミュレートし、実際にUDPでsyslog/SNMP Trapを受信して、
`engine/logicmonitor.py`の`POST /santaba/rest/_emulator/events`
（このエミュレータ独自の拡張エンドポイント）へLMv1署名付きで転送する。

syslogのデコードは`tools/syslog_to_loki.py`、SNMP Trapのデコードは
`tools/snmp_trap_receiver.py`の`decode_snmp_v2c_trap()`をそのまま使う。

使い方:
  python tools/logicmonitor_collector.py \
      --emulator-url http://localhost:8000 \
      --syslog-port 5514 --trap-port 1162

各装置側では:
  configure terminal
  logging host <このブリッジを動かすホストのIP> 5514
  snmp-server host <このブリッジを動かすホストのIP> version 2c public udp-port 1162

LogicMonitor側で監視対象Deviceを登録する際、`name`欄に装置のIP
（syslog/Trapの送信元IPと一致する値）を入れておくと、受信した
イベントが正しいDeviceに紐付く（`docs/logicmonitor-api.md`参照）。
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from engine.logicmonitor import compute_lmv1_signature, LM_ACCESS_ID, LM_ACCESS_KEY
from tools.snmp_trap_receiver import decode_snmp_v2c_trap

_SYSLOG_RE = re.compile(r'^<(\d+)>(.*)$', re.S)
_FACILITY_TAG_RE = re.compile(r'%(\w[\w-]*)-(\d)-')


def parse_syslog(data: bytes, addr) -> dict:
    text = data.decode('utf-8', errors='replace')
    m = _SYSLOG_RE.match(text)
    pri = int(m.group(1)) if m else 14
    body = m.group(2) if m else text
    severity = pri % 8

    tag_m = _FACILITY_TAG_RE.search(body)
    facility_tag = tag_m.group(1) if tag_m else 'SYSLOG'
    severity_from_msg = int(tag_m.group(2)) if tag_m else severity

    return {'source_ip': addr[0], 'severity': severity_from_msg,
            'facility_tag': facility_tag, 'message': body.strip()}


def post_event(emulator_url: str, body: dict) -> int:
    body_str = json.dumps(body)
    epoch = str(int(time.time() * 1000))
    sig = compute_lmv1_signature(LM_ACCESS_KEY, 'POST', epoch, body_str, '/_emulator/events')
    req = urllib.request.Request(
        f'{emulator_url}/santaba/rest/_emulator/events',
        data=body_str.encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'LMv1 {LM_ACCESS_ID}:{sig}:{epoch}'},
        method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def _syslog_loop(bind: str, port: int, emulator_url: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, port))
    print(f'[syslog] listening on udp://{bind}:{port}')
    while True:
        data, addr = sock.recvfrom(65535)
        entry = parse_syslog(data, addr)
        try:
            status = post_event(emulator_url, {
                'type': 'syslog', 'source_ip': entry['source_ip'],
                'severity': entry['severity'], 'facility_tag': entry['facility_tag'],
                'message': entry['message'],
            })
            print(f'[syslog] {entry["source_ip"]} {entry["message"][:80]} -> {status}')
        except Exception as e:
            print(f'[syslog] 転送失敗: {e}', file=sys.stderr)


def _trap_loop(bind: str, port: int, emulator_url: str):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, port))
    print(f'[trap] listening on udp://{bind}:{port}')
    while True:
        data, addr = sock.recvfrom(65535)
        decoded = decode_snmp_v2c_trap(data)
        if decoded is None:
            continue
        try:
            status = post_event(emulator_url, {
                'type': 'trap', 'source_ip': addr[0],
                'trap_oid': decoded['trap_oid'], 'description': decoded['description'],
            })
            print(f'[trap] {addr[0]} {decoded["trap_oid"]} -> {status}')
        except Exception as e:
            print(f'[trap] 転送失敗: {e}', file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--syslog-port', type=int, default=5514)
    p.add_argument('--trap-port', type=int, default=1162,
                   help='既定1162（162は特権ポートのためroot以外は使えない）')
    p.add_argument('--emulator-url', default='http://localhost:8000')
    args = p.parse_args()

    print('\n' + '=' * 70)
    print('LogicMonitor Collector 相当ブリッジ (syslog + SNMP Trap)')
    print('=' * 70)
    print(f'  syslog: udp://{args.bind}:{args.syslog_port}')
    print(f'  trap  : udp://{args.bind}:{args.trap_port}')
    print(f'  転送先: {args.emulator_url}/santaba/rest/_emulator/events')
    print('=' * 70 + '\n')

    t1 = threading.Thread(target=_syslog_loop, args=(args.bind, args.syslog_port,
                                                      args.emulator_url), daemon=True)
    t2 = threading.Thread(target=_trap_loop, args=(args.bind, args.trap_port,
                                                    args.emulator_url), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


if __name__ == '__main__':
    main()
