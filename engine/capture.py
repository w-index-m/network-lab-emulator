"""
警報時の通信記録(パケットキャプチャ)— NIRVANA-R の「アラート発行でパケットキャプチャ」のエッセンス

Linux の lo を AF_PACKET で覗き、装置(lo に載ったアドレス)が関わるパケットの先頭 256 バイトを
直近 20000 件まで覚えておく。警報が出たら、その前後の、警報の送信元・宛先が関わるパケットを
pcap(Wireshark / tcpdump でそのまま開ける)にして渡す。root が必要(NETLAB_CAPTURE=1 で有効)。
"""

import socket
import struct
import threading
import time
from collections import deque

SNAPLEN = 256
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4


class PacketRing:
    def __init__(self, maxlen=20000, snaplen=SNAPLEN):
        self._lock = threading.Lock()
        self.frames = deque(maxlen=maxlen)       # (t, src, dst, frame の先頭, 元の長さ)
        self.snaplen = snaplen
        self._stop = threading.Event()
        self._th = None
        self.error = None

    def add(self, t, buf):
        if len(buf) < 34 or buf[12:14] != b'\x08\x00':
            return
        src, dst = socket.inet_ntoa(buf[26:30]), socket.inet_ntoa(buf[30:34])
        with self._lock:
            self.frames.append((t, src, dst, bytes(buf[:self.snaplen]), len(buf)))

    def start(self, iface='lo'):
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            s.bind((iface, 0))
            s.settimeout(0.5)
        except (OSError, AttributeError) as e:
            self.error = f'キャプチャできません(root と Linux が必要): {e}'
            return False

        def loop():
            while not self._stop.is_set():
                try:
                    buf, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if addr[2] != PACKET_OUTGOING:
                    self.add(time.time(), buf)
            s.close()
        self._th = threading.Thread(target=loop, name='capture', daemon=True)
        self._th.start()
        return True

    def stop(self):
        self._stop.set()

    def select(self, ips, start, end):
        ips = {x for x in ips if x}
        with self._lock:
            return [f for f in self.frames if start <= f[0] <= end and (not ips or f[1] in ips or f[2] in ips)]


def to_pcap(frames, snaplen=SNAPLEN):
    """libpcap 形式(リンク種別 1 = Ethernet。lo の擬似 Ethernet ヘッダをそのまま使う)"""
    out = [struct.pack('<IHHiIII', 0xa1b2c3d4, 2, 4, 0, 0, snaplen, 1)]
    for t, _s, _d, data, orig in frames:
        sec = int(t)
        out.append(struct.pack('<IIII', sec, int((t - sec) * 1e6), len(data), orig) + data)
    return b''.join(out)


ring = PacketRing()
