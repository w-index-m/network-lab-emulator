"""
tools/route_injector_cli.py テスト

GUI版(tools/route_injector/network_route_injector.py)のRIP/BGP
プロトコル構築ロジックの移植が正しいかを検証する。

- RIP: パケット構築のバイト単位検証
- BGP: 実TCPソケットで動くモックBGPピアに対してOPEN/UPDATEを送信し、
  実際に相手側でパースできる正しいバイト列が送られているかを確認
"""

import json
import socket
import struct
import sys
import os
import threading
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.route_injector_cli import (
    rip_build_rte, rip_build_packet, _parse_route_spec,
    bgp_build_open, bgp_build_update, bgp_encode_prefix, bgp_parse_community,
    BGPSpeaker,
)


# ── RIP ──────────────────────────────────────────
def test_rip_build_rte_v2_fields():
    rte = rip_build_rte('10.0.0.0', '255.255.255.0', '192.168.1.1', 5, 100, version=2)
    afi, tag, net, mask, nh, metric = struct.unpack('!HH4s4s4sI', rte)
    assert afi == 2
    assert tag == 100
    assert socket.inet_ntoa(net) == '10.0.0.0'
    assert socket.inet_ntoa(mask) == '255.255.255.0'
    assert socket.inet_ntoa(nh) == '192.168.1.1'
    assert metric == 5


def test_rip_build_rte_v1_zeroes_mask_and_nexthop():
    """RIPv1はclassfulでmask/nexthopフィールドを持たない(常に0)"""
    rte = rip_build_rte('10.0.0.0', '255.255.255.0', '192.168.1.1', 1, 0, version=1)
    afi, tag, net, mask, nh, metric = struct.unpack('!HH4s4s4sI', rte)
    assert tag == 0
    assert socket.inet_ntoa(mask) == '0.0.0.0'
    assert socket.inet_ntoa(nh) == '0.0.0.0'


def test_parse_route_spec():
    spec = _parse_route_spec('10.20.30.0/24:172.16.0.1:5:100')
    assert spec == {'network': '10.20.30.0', 'netmask': '255.255.255.0',
                    'nexthop': '172.16.0.1', 'metric': 5, 'tag': 100}


def test_parse_route_spec_default_tag():
    spec = _parse_route_spec('10.0.0.0/8:1.1.1.1:1')
    assert spec['tag'] == 0


def test_rip_build_packet_header_and_length():
    spec = _parse_route_spec('10.20.30.0/24:172.16.0.1:5:100')
    pkt = rip_build_packet([spec], version=2, command=2)
    command, version, _ = struct.unpack('!BBH', pkt[:4])
    assert command == 2
    assert version == 2
    assert len(pkt) == 4 + 20


def test_rip_build_packet_rejects_over_25_routes():
    spec = _parse_route_spec('10.0.0.0/24:1.1.1.1:1')
    with pytest.raises(ValueError):
        rip_build_packet([spec] * 26, version=2, command=2)


# ── BGP: パケット構築の単体検証 ──────────────────
def test_bgp_encode_prefix():
    encoded = bgp_encode_prefix('10.10.0.0', 24)
    plen = encoded[0]
    assert plen == 24
    assert encoded[1:4] == socket.inet_aton('10.10.0.0')[:3]


def test_bgp_parse_community_numeric():
    assert bgp_parse_community('65001:100') == (65001 << 16) | 100


def test_bgp_parse_community_well_known():
    assert bgp_parse_community('no-export') == 0xFFFFFF01


def test_bgp_build_open_header():
    pkt = bgp_build_open(65002, 30, '10.0.0.1')
    assert pkt[:16] == b'\xff' * 16
    length, mtype = struct.unpack('!HB', pkt[16:19])
    assert length == len(pkt)
    assert mtype == 1  # OPEN


# ── BGP: 実TCPモックピアに対する統合検証 ──────────
class _MockBgpPeer:
    """実際にTCPで待ち受け、OPEN/KEEPALIVE/UPDATEを本物のバイト列として
    受信・パースするモックBGPピア"""

    def __init__(self, port):
        self.port = port
        self.events = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', port))
        self._srv.listen(1)
        self._srv.settimeout(10)

    @staticmethod
    def _recv_exact(sock, n):
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('closed')
            buf += chunk
        return buf

    @staticmethod
    def _header(msg_type, body):
        return b'\xff' * 16 + struct.pack('!HB', 19 + len(body), msg_type) + body

    def run(self):
        conn, _ = self._srv.accept()
        conn.settimeout(10)
        try:
            hdr = self._recv_exact(conn, 19)
            _, length, mtype = struct.unpack('!16sHB', hdr)
            body = self._recv_exact(conn, length - 19)
            version, my_as, hold, bgp_id = struct.unpack('!BHH4s', body[:9])
            self.events.append(('OPEN', my_as, hold, socket.inet_ntoa(bgp_id)))

            conn.sendall(self._header(1, struct.pack(
                '!BHH4sB', 4, 65001, 180, socket.inet_aton('127.0.0.1'), 0)))
            conn.sendall(self._header(4, b''))

            hdr = self._recv_exact(conn, 19)
            _, length, mtype = struct.unpack('!16sHB', hdr)
            self._recv_exact(conn, length - 19)
            self.events.append(('KEEPALIVE', mtype))

            hdr = self._recv_exact(conn, 19)
            _, length, mtype = struct.unpack('!16sHB', hdr)
            body = self._recv_exact(conn, length - 19)
            routes = []
            if mtype == 2:
                wlen = struct.unpack('!H', body[0:2])[0]
                off = 2 + wlen
                palen = struct.unpack('!H', body[off:off + 2])[0]
                nlri = body[off + 2 + palen:]
                i = 0
                while i < len(nlri):
                    plen = nlri[i]
                    nbytes = (plen + 7) // 8
                    ip_bytes = nlri[i + 1:i + 1 + nbytes] + b'\x00' * (4 - nbytes)
                    routes.append(f'{socket.inet_ntoa(ip_bytes)}/{plen}')
                    i += 1 + nbytes
            self.events.append(('UPDATE', mtype, routes))
        finally:
            conn.close()
            self._srv.close()


def test_bgp_speaker_establishes_and_advertises_over_real_tcp():
    port = 17179
    peer = _MockBgpPeer(port)
    t = threading.Thread(target=peer.run, daemon=True)
    t.start()
    time.sleep(0.3)

    speaker = BGPSpeaker(
        peer_ip='127.0.0.1', local_as=65002, remote_as=65001,
        router_id='10.0.0.1', hold_time=30, port=port,
        on_log=lambda msg: None,
    )
    speaker.connect()
    assert speaker.established_event.wait(timeout=5)
    speaker.advertise([('10.10.0.0', 24)], next_hop='192.168.1.2')
    time.sleep(0.5)
    speaker.close()
    t.join(timeout=5)

    assert peer.events[0][0] == 'OPEN'
    assert peer.events[0][1] == 65002  # my_as
    assert peer.events[0][3] == '10.0.0.1'  # router_id
    assert peer.events[1] == ('KEEPALIVE', 4)
    assert peer.events[2][0] == 'UPDATE'
    assert peer.events[2][2] == ['10.10.0.0/24']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
