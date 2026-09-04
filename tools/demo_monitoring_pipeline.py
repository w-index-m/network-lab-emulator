#!/usr/bin/env python3
"""
監視パイプライン デモケース構築ツール

仮想ラボ内に「実際に動くデモ」を作る:
  1. 2台の装置（既定: catalyst <-> cisco）にIPを設定してリンクを作成
  2. 継続的にpingトラフィックを流し、SNMPカウンタ（CPU/トラフィック）が
     動き続ける状態を作る

これにより、SNMPダッシュボード（static/snmp_dashboard.html）や
Prometheus Exporter（tools/prometheus_exporter.py）を通して見たときに、
「値が動いているデモ」をすぐ見せられる。

使い方:
  # 1. エミュレーターを起動しておく（別ターミナル）: python app.py
  # 2. デモ環境をセットアップしてトラフィックを流し続ける
  python tools/demo_monitoring_pipeline.py

  # 装置やIPを変えたい場合
  python tools/demo_monitoring_pipeline.py --device-a catalyst --device-b cisco

  # 1回セットアップするだけ（トラフィックは流さない）
  python tools/demo_monitoring_pipeline.py --setup-only
"""

import argparse
import json
import sys
import os
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class DemoPipeline:
    def __init__(self, base_url='http://localhost:8000', token=''):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _headers(self):
        h = {'Content-Type': 'application/json'}
        if self.token:
            h['X-Session-Token'] = self.token
        return h

    def cli(self, device_id, command):
        req = urllib.request.Request(
            f'{self.base_url}/api/cli',
            data=json.dumps({'device_id': device_id, 'command': command}).encode(),
            headers=self._headers(), method='POST'
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('output', '')

    def link(self, a, b, iface_a, iface_b):
        req = urllib.request.Request(
            f'{self.base_url}/api/link',
            data=json.dumps({'a': a, 'b': b, 'iface_a': iface_a, 'iface_b': iface_b}).encode(),
            headers=self._headers(), method='POST'
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def check_connectivity(self):
        try:
            req = urllib.request.Request(f'{self.base_url}/api/status', headers=self._headers())
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
            return True
        except Exception as e:
            print(f'❌ エミュレーターに接続できません: {e}')
            return False

    def setup(self, dev_a, iface_a, ip_a, dev_b, iface_b, ip_b, prefix):
        print('\n' + '=' * 70)
        print('📍 デモ環境セットアップ')
        print('=' * 70)

        for dev, iface, ip in ((dev_a, iface_a, ip_a), (dev_b, iface_b, ip_b)):
            self.cli(dev, 'configure terminal')
            self.cli(dev, f'interface {iface}')
            self.cli(dev, f'ip address {ip} {_prefix_to_mask(prefix)}')
            self.cli(dev, 'no shutdown')
            self.cli(dev, 'end')
            print(f'  ✅ {dev} {iface} に {ip}/{prefix} を設定')

        result = self.link(dev_a, dev_b, iface_a, iface_b)
        print(f'  ✅ リンク作成: {dev_a} <-> {dev_b}')
        print(f'      neighbors: {result.get("neighbors")}')

        # 疎通確認
        out = self.cli(dev_a, f'ping {ip_b} repeat 5')
        last_line = out.strip().splitlines()[-1] if out.strip() else ''
        print(f'  📡 疎通確認: {last_line}')
        if 'Success rate is 100' not in out:
            print('  ⚠️  疎通に失敗している可能性があります。IP設定を確認してください。')
        return dev_a, ip_b

    def generate_traffic_forever(self, dev_a, ip_b, interval, ping_count):
        print('\n' + '=' * 70)
        print('📈 トラフィック生成中（Ctrl+Cで停止）')
        print('=' * 70)
        print(f'  {ping_count}件のpingを{interval}秒間隔で送り続けます')
        n = 0
        try:
            while True:
                n += 1
                out = self.cli(dev_a, f'ping {ip_b} repeat {ping_count}')
                last_line = out.strip().splitlines()[-1] if out.strip() else '(no output)'
                ts = time.strftime('%H:%M:%S')
                print(f'  [{ts}] round {n}: {last_line}')
                time.sleep(interval)
        except KeyboardInterrupt:
            print('\n停止しました')


def _prefix_to_mask(prefix: int) -> str:
    bits = (0xffffffff << (32 - prefix)) & 0xffffffff
    return '.'.join(str((bits >> (8 * i)) & 0xff) for i in (3, 2, 1, 0))


def main():
    parser = argparse.ArgumentParser(description='監視パイプライン デモケース構築ツール')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーターURL')
    parser.add_argument('--token', default='', help='セッショントークン（認証有効時）')
    parser.add_argument('--device-a', default='catalyst')
    parser.add_argument('--iface-a', default='GigabitEthernet1/0/1')
    parser.add_argument('--ip-a', default='10.9.9.1')
    parser.add_argument('--device-b', default='cisco')
    parser.add_argument('--iface-b', default='GigabitEthernet0/0/0')
    parser.add_argument('--ip-b', default='10.9.9.2')
    parser.add_argument('--prefix', type=int, default=30)
    parser.add_argument('--interval', type=int, default=3, help='ping送出間隔（秒）')
    parser.add_argument('--ping-count', type=int, default=10, help='1回あたりのping数')
    parser.add_argument('--setup-only', action='store_true', help='セットアップのみ行いトラフィックは流さない')
    args = parser.parse_args()

    pipeline = DemoPipeline(base_url=args.url, token=args.token)
    if not pipeline.check_connectivity():
        print('先に `python app.py` でエミュレーターを起動してください。')
        return 1

    dev_a, ip_b = pipeline.setup(
        args.device_a, args.iface_a, args.ip_a,
        args.device_b, args.iface_b, args.ip_b, args.prefix,
    )

    if args.setup_only:
        print('\n--setup-only のためトラフィック生成はスキップします。')
        return 0

    pipeline.generate_traffic_forever(dev_a, ip_b, args.interval, args.ping_count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
