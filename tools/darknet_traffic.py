#!/usr/bin/env python3
"""
ダークネット観測(/cyber-view の DARKNET)の試験用に、4種類の通信をラボのダークネットへ流す。

  python3 tools/darknet_traffic.py --prefix 10.250.0.0/24 scan        # ワーム型の探索(TCP SYN を多数のアドレスへ)
  python3 tools/darknet_traffic.py --prefix 10.250.0.0/24 backscatter # DDoS の跳ね返り(偽装された送信元への SYN-ACK)
  python3 tools/darknet_traffic.py --prefix 10.250.0.0/24 reflection  # 踏み台探し(UDP 53 / 123 / 1900 / 11211)
  python3 tools/darknet_traffic.py --prefix 10.250.0.0/24 misconfig   # 同じアドレスへ繰り返し(設定ミス)
  python3 tools/darknet_traffic.py --prefix 10.250.0.0/24 all

安全のため、宛先が「このホストの lo に引き込まれたプライベートな帯」の時だけ送る
(エミュレータの /api/darknet/config で sensor=true にすると引き込まれる)。外へは一切出ない。
backscatter は送信元 IP を偽装した生パケットを作るので root が必要。
"""
import argparse
import ipaddress
import random
import socket
import struct
import subprocess
import sys
import time


def _check(prefix):
    net = ipaddress.ip_network(prefix, strict=False)
    if not (net.is_private or net.subnet_of(ipaddress.ip_network('100.64.0.0/10'))):
        sys.exit(f'{net} はプライベートな帯ではありません')
    probe = str(net.network_address + 1)
    r = subprocess.run(['ip', 'route', 'get', probe], capture_output=True, text=True)
    if 'local' not in r.stdout or 'dev lo' not in r.stdout:
        sys.exit(f'{net} がこのホストの lo に引き込まれていません(先に /api/darknet/config で sensor=true)')
    return net


def _hosts(net, n):
    hs = list(net.hosts())
    return random.sample(hs, min(n, len(hs)))


SRC = '127.0.0.66'      # 攻撃元(このホストの lo のアドレス。--src で変更)


def _sock(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind((SRC, 0))      # 送信元を決めないと、カーネルは宛先(lo に引き込んだ帯)そのものを送信元にしてしまう
    return s


def scan(net, n=24):
    """ワーム型: 多数のアドレスの 445 / 23 / 22 へ SYN(connect はダークネットなので応答なしで失敗する)"""
    for h in _hosts(net, n):
        for port in (445, 23):
            s = _sock(socket.SOCK_STREAM)
            s.setblocking(False)
            try:
                s.connect_ex((str(h), port))
            finally:
                s.close()
        time.sleep(0.05)


def _csum(b):
    if len(b) % 2:
        b += b'\0'
    s = sum(struct.unpack(f'!{len(b) // 2}H', b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _tcp_packet(src, dst, sport, dport, flags):
    tcp = struct.pack('!HHIIBBHHH', sport, dport, random.getrandbits(32), random.getrandbits(32), 5 << 4, flags, 64240, 0, 0)
    pseudo = socket.inet_aton(src) + socket.inet_aton(dst) + struct.pack('!BBH', 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack('!H', _csum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), random.getrandbits(16), 0, 64, 6, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    ip = ip[:10] + struct.pack('!H', _csum(ip)) + ip[12:]
    return ip + tcp


def backscatter(net, n=12, victim='203.0.113.80'):
    """被害サーバー(victim:80)が、偽装された送信元(=ダークネットのアドレス)へ SYN-ACK を返した、を再現する"""
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    for h in _hosts(net, n):
        flags = 0x12 if random.random() < 0.8 else 0x04            # SYN-ACK か RST
        s.sendto(_tcp_packet(victim, str(h), 80, random.randint(1024, 65535), flags), (str(h), 0))
        time.sleep(0.05)
    s.close()


def reflection(net):
    """オープンリゾルバ・NTP・SSDP・memcached の探索(UDP の問い合わせ)"""
    s = _sock(socket.SOCK_DGRAM)
    for h, port, payload in [(h, p, b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x00\x00\x01\x00\x01')
                             for h in _hosts(net, 3) for p in (53,)] + \
            [(h, 123, b'\x17\x00\x03\x2a' + b'\0' * 4) for h in _hosts(net, 2)] + \
            [(h, 1900, b'M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1900\r\nMAN:"ssdp:discover"\r\nST:ssdp:all\r\n\r\n')
             for h in _hosts(net, 1)] + \
            [(h, 11211, b'\0\x01\0\0\0\x01\0\0stats\r\n') for h in _hosts(net, 1)]:
        s.sendto(payload, (str(h), port))
        time.sleep(0.05)
    s.close()


def misconfig(net, n=8):
    """古い設定が使われていないアドレスの syslog(514)を指したまま送り続けている"""
    h = str(_hosts(net, 1)[0])
    s = _sock(socket.SOCK_DGRAM)
    for i in range(n):
        s.sendto(f'<190>old-config message {i}'.encode(), (h, 514))
        time.sleep(0.1)
    s.close()


def main():
    global SRC
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--prefix', required=True)
    ap.add_argument('--src', default=SRC, help='攻撃元のアドレス(このホストにあるもの。既定 127.0.0.66)')
    ap.add_argument('kind', choices=['scan', 'backscatter', 'reflection', 'misconfig', 'all'])
    a = ap.parse_args()
    SRC = a.src
    net = _check(a.prefix)
    for k in (['scan', 'backscatter', 'reflection', 'misconfig'] if a.kind == 'all' else [a.kind]):
        print(f'[darknet_traffic] {k} → {net}')
        globals()[k](net)
        time.sleep(0.5)


if __name__ == '__main__':
    main()
