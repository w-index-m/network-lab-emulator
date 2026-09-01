#!/usr/bin/env python3
"""
ルーティングジェネレーター（CLI）

仮想ラボの装置に大量のスタティックルートを注入し、RIBの経路数を
意図的に増やす。SNMPダッシュボード/Prometheus Exporter経由で
「経路が増えたこと」を確認できるようにするためのツール。

使い方:
  # デフォルト: catalystに 10.50.0.0/24 から連番で100経路注入
  python tools/routing_generator.py

  # 件数・対象・ベースネットワークを指定
  python tools/routing_generator.py --device catalyst --count 500 \
      --base-network 172.16.0.0 --prefix 24 --next-hop 10.9.9.2

  # 注入した経路を削除（クリーンアップ）
  python tools/routing_generator.py --cleanup
"""

import argparse
import ipaddress
import json
import sys
import os
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class RoutingGenerator:
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
            headers=self._headers(), method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('output', '')

    def route_count(self, device_id):
        req = urllib.request.Request(f'{self.base_url}/api/snmp/dashboard', headers=self._headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        for d in data.get('devices', []):
            if d['device_id'] == device_id:
                return d.get('route_count')
        return None

    def check_connectivity(self):
        try:
            req = urllib.request.Request(f'{self.base_url}/api/status', headers=self._headers())
            with urllib.request.urlopen(req, timeout=3) as r:
                r.read()
            return True
        except Exception as e:
            print(f'❌ エミュレーターに接続できません: {e}')
            return False


def _generate_networks(base_network: str, prefix: int, count: int):
    """base_network から prefix幅ずつ連続するネットワークをcount個生成する"""
    base = ipaddress.ip_network(f'{base_network}/{prefix}', strict=False)
    step = base.num_addresses
    start = int(base.network_address)
    for i in range(count):
        net = ipaddress.ip_network((start + i * step, prefix), strict=False)
        yield str(net.network_address), str(net.netmask)


def main():
    parser = argparse.ArgumentParser(description='ルーティングジェネレーター（CLI）')
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--token', default='')
    parser.add_argument('--device', default='catalyst', help='経路を注入する装置')
    parser.add_argument('--count', type=int, default=100, help='注入する経路数')
    parser.add_argument('--base-network', default='10.50.0.0', help='開始ネットワークアドレス')
    parser.add_argument('--prefix', type=int, default=24, help='各経路のプレフィックス長')
    parser.add_argument('--next-hop', default='10.9.9.2', help='ネクストホップIP')
    parser.add_argument('--cleanup', action='store_true',
                        help='--base-network から --count 分の経路を削除する')
    args = parser.parse_args()

    gen = RoutingGenerator(base_url=args.url, token=args.token)
    if not gen.check_connectivity():
        print('先に `python app.py` でエミュレーターを起動してください。')
        return 1

    before = gen.route_count(args.device)
    print('\n' + '=' * 70)
    print(f'🧭 ルーティングジェネレーター — {args.device}')
    print('=' * 70)
    print(f'  注入前の経路数: {before}')

    networks = list(_generate_networks(args.base_network, args.prefix, args.count))

    gen.cli(args.device, 'configure terminal')
    verb = '削除' if args.cleanup else '注入'
    print(f'  {len(networks)}経路を{verb}中...')
    t0 = time.time()
    for net, mask in networks:
        cmd = f'no ip route {net} {mask} {args.next_hop}' if args.cleanup \
            else f'ip route {net} {mask} {args.next_hop}'
        gen.cli(args.device, cmd)
    gen.cli(args.device, 'end')
    elapsed = time.time() - t0

    after = gen.route_count(args.device)
    print(f'  完了（{elapsed:.1f}秒）')
    print(f'  注入後の経路数: {after}')
    if before is not None and after is not None:
        diff = after - before
        print(f'  差分: {diff:+d}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
