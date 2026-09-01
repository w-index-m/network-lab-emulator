#!/usr/bin/env python3
"""
Route Injector CLI

tools/route_injector/network_route_injector.py（Windows GUI/Tkinter版）の
RIP・BGP注入ロジックを、画面操作無しでコマンドラインから実行できるように
移植したツール。

GUI版はTkinter必須（Tkinterが無い環境ではimportすら失敗する）だが、
本ツールはTkinter/scapyに一切依存しないため、Linux/CI環境でも動く。
プロトコルのパケット構築ロジック（RIP RTE組み立て、BGP OPEN/UPDATE組み立て）
はGUI版と同一の実装を移植している。

対応プロトコル:
  - RIP v1/v2: UDPでRIP Response（経路広告）を送信
  - BGP: TCP(179)でネイバーを確立し、経路を広告（community/large-community/
    extended-community、MED、local-pref対応）

OSPFはscapyでの生パケット構築が必要で、GUI版のタブ実装がTkinterの
イベントループと密結合しているため、本CLI版では未対応（今後の課題）。

使い方:
  # RIP: 192.168.1.1へ経路を1件広告
  python tools/route_injector_cli.py rip --dest 192.168.1.1 \
      --route 10.0.0.0/24:192.168.1.100:1

  # BGP: 192.168.1.1のAS65001へeBGPネイバーを確立し経路を広告
  python tools/route_injector_cli.py bgp --peer 192.168.1.1 \
      --local-as 65002 --remote-as 65001 --router-id 10.0.0.1 \
      --route 10.10.0.0/24 --next-hop 192.168.1.2 \
      --community 65002:100 --hold-seconds 5

免責: 検証・研修用途専用。対象ネットワークの管理者の許可なく
実運用環境で実行しないでください。
"""

import argparse
import socket
import struct
import sys
import threading
import time

RIP_PORT = 520
BGP_PORT = 179
BGP_MARKER = b"\xff" * 16

BGP_NOTIFICATION_CODES = {
    (1, 1): "Message Header Error: Connection Not Synchronized",
    (2, 2): "OPEN Message Error: Bad Peer AS",
    (6, 2): "Cease: Administrative Shutdown",
}


# ══════════════════════════════════════════
# RIP（GUI版 rip_build_rte / rip_build_packet / rip_send_packet の移植）
# ══════════════════════════════════════════
def rip_build_rte(network, netmask, nexthop, metric, tag, version):
    afi = 2
    ip_bytes = socket.inet_aton(network)
    if version == 1:
        tag_bytes = struct.pack("!H", 0)
        mask_bytes = b"\x00\x00\x00\x00"
        nexthop_bytes = b"\x00\x00\x00\x00"
    else:
        tag_bytes = struct.pack("!H", tag)
        mask_bytes = socket.inet_aton(netmask)
        nexthop_bytes = socket.inet_aton(nexthop)
    return (struct.pack("!H", afi) + tag_bytes + ip_bytes + mask_bytes
            + nexthop_bytes + struct.pack("!I", metric))


def rip_build_packet(routes, version, command):
    if len(routes) > 25:
        raise ValueError("1パケットに含められる経路は最大25件です(RFC制限)")
    header = struct.pack("!BBH", command, version, 0)
    body = b"".join(
        rip_build_rte(r["network"], r["netmask"], r["nexthop"], r["metric"], r["tag"], version)
        for r in routes
    )
    return header + body


def rip_send_packet(dest_ip, packet, bind_ip, ttl):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    except OSError:
        pass
    sock.bind((bind_ip or "0.0.0.0", RIP_PORT))
    try:
        sock.sendto(packet, (dest_ip, RIP_PORT))
    finally:
        sock.close()


def _parse_route_spec(spec: str) -> dict:
    """'network/prefix:nexthop:metric[:tag]' 形式をRIP RTE辞書に変換"""
    dest, nexthop, rest = spec.split(":", 2)
    parts = rest.split(":")
    metric = int(parts[0])
    tag = int(parts[1]) if len(parts) > 1 else 0
    network, prefix = dest.split("/")
    prefix = int(prefix)
    mask_int = (0xffffffff << (32 - prefix)) & 0xffffffff if prefix > 0 else 0
    netmask = socket.inet_ntoa(struct.pack("!I", mask_int))
    return {"network": network, "netmask": netmask, "nexthop": nexthop,
            "metric": metric, "tag": tag}


def run_rip(args):
    routes = [_parse_route_spec(s) for s in args.route]
    if not routes:
        print("❌ --route を最低1つ指定してください")
        return 1
    print(f"\n📡 RIPv{args.version} Response を {args.dest} へ送信")
    for r in routes:
        print(f"  - {r['network']}/{r['netmask']} nexthop={r['nexthop']} "
              f"metric={r['metric']} tag={r['tag']}")
    packet = rip_build_packet(routes, args.version, command=2)  # 2=Response
    rip_send_packet(args.dest, packet, args.bind_ip, args.ttl)
    print(f"✅ 送信完了（{len(packet)} bytes）")
    return 0


# ══════════════════════════════════════════
# BGP（GUI版 BGPSpeaker / bgp_build_* の移植）
# ══════════════════════════════════════════
def bgp_build_header(msg_type, body):
    length = 19 + len(body)
    return BGP_MARKER + struct.pack("!HB", length, msg_type) + body


def bgp_build_capabilities(local_as):
    caps = struct.pack("!BB", 1, 4) + struct.pack("!HBB", 1, 0, 1)
    caps += struct.pack("!BB", 2, 0)
    caps += struct.pack("!BB", 65, 4) + struct.pack("!I", local_as)
    return caps


def bgp_build_open(local_as, hold_time, router_id):
    caps = bgp_build_capabilities(local_as)
    opt_params = struct.pack("!BB", 2, len(caps)) + caps
    my_as_field = 23456 if local_as > 0xFFFF else local_as
    body = struct.pack("!BHH4sB", 4, my_as_field, hold_time,
                        socket.inet_aton(router_id), len(opt_params)) + opt_params
    return bgp_build_header(1, body)


def bgp_build_keepalive():
    return bgp_build_header(4, b"")


def bgp_build_notification(code, subcode, data=b""):
    return bgp_build_header(3, struct.pack("!BB", code, subcode) + data)


def bgp_encode_prefix(network, prefix_len):
    num_bytes = (prefix_len + 7) // 8
    ip_bytes = socket.inet_aton(network)
    return struct.pack("!B", prefix_len) + ip_bytes[:num_bytes]


def bgp_parse_community(token: str) -> int:
    well_known = {"no-export": 0xFFFFFF01, "no-advertise": 0xFFFFFF02,
                  "no-export-subconfed": 0xFFFFFF03, "blackhole": 0xFFFF029A}
    token = token.strip()
    if token.lower() in well_known:
        return well_known[token.lower()]
    asn_str, val_str = token.split(":")
    return (int(asn_str) << 16) | int(val_str)


def bgp_build_path_attributes(next_hop, as_path, is_ibgp, origin=0, med=None, local_pref=None,
                               communities=None):
    attrs = struct.pack("!BBB", 0x40, 1, 1) + struct.pack("!B", origin)
    if as_path:
        as_seq = b"".join(struct.pack("!I", asn) for asn in as_path)
        as_path_value = struct.pack("!BB", 2, len(as_path)) + as_seq
    else:
        as_path_value = b""
    attrs += struct.pack("!BBB", 0x40, 2, len(as_path_value)) + as_path_value
    attrs += struct.pack("!BBB", 0x40, 3, 4) + socket.inet_aton(next_hop)
    if med is not None:
        attrs += struct.pack("!BBB", 0x80, 4, 4) + struct.pack("!I", med)
    if is_ibgp:
        lp = local_pref if local_pref is not None else 100
        attrs += struct.pack("!BBB", 0x40, 5, 4) + struct.pack("!I", lp)
    if communities:
        comm_value = b"".join(struct.pack("!I", c) for c in communities)
        attrs += struct.pack("!BBB", 0xC0, 8, len(comm_value)) + comm_value
    return attrs


def bgp_build_update(routes, next_hop, as_path, is_ibgp, med=None, local_pref=None,
                     withdrawn=None, communities=None):
    withdrawn_bytes = b"".join(bgp_encode_prefix(n, p) for n, p in (withdrawn or []))
    path_attrs = (bgp_build_path_attributes(next_hop, as_path, is_ibgp, med=med,
                                            local_pref=local_pref, communities=communities)
                  if routes else b"")
    nlri_bytes = b"".join(bgp_encode_prefix(n, p) for n, p in routes)
    body = (struct.pack("!H", len(withdrawn_bytes)) + withdrawn_bytes
            + struct.pack("!H", len(path_attrs)) + path_attrs + nlri_bytes)
    return bgp_build_header(2, body)


class BGPSpeaker:
    """GUI版 BGPSpeaker の移植（Tkinter依存なし）"""
    STATE_IDLE = "Idle"
    STATE_CONNECT = "Connect"
    STATE_OPENSENT = "OpenSent"
    STATE_OPENCONFIRM = "OpenConfirm"
    STATE_ESTABLISHED = "Established"

    def __init__(self, peer_ip, local_as, remote_as, router_id, local_ip=None,
                hold_time=180, port=BGP_PORT, on_log=None, connect_timeout=10):
        self.peer_ip = peer_ip
        self.local_as = local_as
        self.remote_as = remote_as
        self.router_id = router_id
        self.local_ip = local_ip
        self.hold_time = hold_time
        self.port = port
        self.on_log = on_log or (lambda msg: print(msg))
        self.connect_timeout = connect_timeout
        self.is_ibgp = (local_as == remote_as)

        self.sock = None
        self.state = self.STATE_IDLE
        self.stop_event = threading.Event()
        self.established_event = threading.Event()
        self._send_lock = threading.Lock()

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.on_log(f"[{ts}] {msg}")

    def _set_state(self, new_state):
        if self.state != new_state:
            self._log(f"状態遷移: {self.state} -> {new_state}")
            self.state = new_state
            if new_state == self.STATE_ESTABLISHED:
                self.established_event.set()

    def _send(self, data):
        with self._send_lock:
            self.sock.sendall(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("接続がリモートにより閉じられました")
            buf += chunk
        return buf

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.local_ip:
            self.sock.bind((self.local_ip, 0))
        self.sock.settimeout(self.connect_timeout)
        self._set_state(self.STATE_CONNECT)
        self._log(f"{self.peer_ip}:{self.port} へTCP接続中...")
        self.sock.connect((self.peer_ip, self.port))
        self.sock.settimeout(None)
        self._log(f"TCP接続確立。OPEN送信(local_as={self.local_as}, hold_time={self.hold_time})")
        self._send(bgp_build_open(self.local_as, self.hold_time, self.router_id))
        self._set_state(self.STATE_OPENSENT)
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def close(self, send_cease=True):
        if send_cease and self.sock and self.state != self.STATE_IDLE:
            try:
                self._send(bgp_build_notification(6, 2))
            except OSError:
                pass
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self._set_state(self.STATE_IDLE)

    def _recv_loop(self):
        try:
            while not self.stop_event.is_set():
                header = self._recv_exact(19)
                _marker, length, mtype = struct.unpack("!16sHB", header)
                body_len = length - 19
                body = self._recv_exact(body_len) if body_len > 0 else b""
                self._handle_message(mtype, body)
        except (ConnectionError, OSError) as e:
            if not self.stop_event.is_set():
                self._log(f"[ERROR] 受信ループ終了: {e}")
            self._set_state(self.STATE_IDLE)
            self.stop_event.set()

    def _handle_message(self, mtype, body):
        if mtype == 1:
            version, peer_as_field, hold, bgp_id = struct.unpack("!BHH4s", body[:9])
            self._log(f"OPEN受信: peer_as={peer_as_field} hold={hold} "
                      f"router_id={socket.inet_ntoa(bgp_id)}")
            self._send(bgp_build_keepalive())
            self._set_state(self.STATE_OPENCONFIRM)
        elif mtype == 4:
            if self.state == self.STATE_OPENCONFIRM:
                self._set_state(self.STATE_ESTABLISHED)
        elif mtype == 2:
            self._log(f"UPDATE受信({len(body)} bytes)")
        elif mtype == 3:
            code, subcode = struct.unpack("!BB", body[:2]) if len(body) >= 2 else (0, 0)
            self._log(f"[NOTIFICATION受信] "
                      f"{BGP_NOTIFICATION_CODES.get((code, subcode), f'code={code} subcode={subcode}')}")
            self.stop_event.set()
            self._set_state(self.STATE_IDLE)

    def _keepalive_loop(self):
        interval = max(1, self.hold_time // 3) if self.hold_time else 30
        while not self.stop_event.wait(interval):
            if self.state in (self.STATE_OPENCONFIRM, self.STATE_ESTABLISHED):
                try:
                    self._send(bgp_build_keepalive())
                except OSError:
                    break

    def advertise(self, routes, next_hop, as_path=None, med=None, local_pref=None,
                 communities=None):
        if self.state != self.STATE_ESTABLISHED:
            self._log("[WARN] Established状態ではないため広告をスキップします")
            return False
        pkt = bgp_build_update(routes, next_hop, as_path, self.is_ibgp, med=med,
                               local_pref=local_pref, communities=communities)
        self._send(pkt)
        for net, plen in routes:
            self._log(f"[OK] 広告: {net}/{plen} next_hop={next_hop} as_path={as_path or '(empty)'}")
        return True


def _parse_bgp_route(spec: str):
    network, prefix = spec.split("/")
    return network, int(prefix)


def run_bgp(args):
    speaker = BGPSpeaker(
        peer_ip=args.peer, local_as=args.local_as, remote_as=args.remote_as,
        router_id=args.router_id, local_ip=args.local_ip,
        hold_time=args.hold_time, connect_timeout=args.connect_timeout,
    )
    try:
        speaker.connect()
    except (OSError, socket.timeout) as e:
        print(f"❌ 接続失敗: {e}")
        return 1

    if not speaker.established_event.wait(timeout=args.hold_seconds):
        print(f"❌ {args.hold_seconds}秒待ちましたがEstablishedになりませんでした"
              f"（現在の状態: {speaker.state}）")
        speaker.close()
        return 1

    print(f"✅ BGPセッション確立（peer={args.peer}, state={speaker.state}）")

    routes = [_parse_bgp_route(s) for s in args.route]
    if routes:
        as_path = [int(x) for x in args.as_path.split(",")] if args.as_path else None
        communities = [bgp_parse_community(c) for c in args.community] if args.community else None
        speaker.advertise(routes, next_hop=args.next_hop, as_path=as_path,
                          med=args.med, local_pref=args.local_pref, communities=communities)

    if args.keep_alive_seconds > 0:
        print(f"⏱  セッションを{args.keep_alive_seconds}秒維持します...")
        time.sleep(args.keep_alive_seconds)

    speaker.close()
    print("👋 セッションをクローズしました")
    return 0


def main():
    parser = argparse.ArgumentParser(description='Route Injector CLI（RIP/BGP、Tkinter/scapy不要）')
    sub = parser.add_subparsers(dest='proto', required=True)

    p_rip = sub.add_parser('rip', help='RIP経路をUDPで広告')
    p_rip.add_argument('--dest', required=True, help='送信先IP（ユニキャストまたはマルチキャスト224.0.0.9）')
    p_rip.add_argument('--route', action='append', required=True,
                       help='network/prefix:nexthop:metric[:tag] 形式。複数指定可')
    p_rip.add_argument('--version', type=int, default=2, choices=[1, 2])
    p_rip.add_argument('--bind-ip', default='0.0.0.0')
    p_rip.add_argument('--ttl', type=int, default=1)

    p_bgp = sub.add_parser('bgp', help='BGPネイバーを確立し経路を広告')
    p_bgp.add_argument('--peer', required=True, help='BGPネイバーのIPアドレス')
    p_bgp.add_argument('--local-as', type=int, required=True)
    p_bgp.add_argument('--remote-as', type=int, required=True)
    p_bgp.add_argument('--router-id', required=True)
    p_bgp.add_argument('--local-ip', default=None)
    p_bgp.add_argument('--hold-time', type=int, default=180, help='BGP HOLDTIME（秒）')
    p_bgp.add_argument('--hold-seconds', type=float, default=10,
                       help='Established状態になるまでの最大待機秒数')
    p_bgp.add_argument('--connect-timeout', type=float, default=10)
    p_bgp.add_argument('--route', action='append', default=[],
                       help='network/prefix 形式。複数指定可')
    p_bgp.add_argument('--next-hop', help='広告する経路のnext-hop（--route指定時必須）')
    p_bgp.add_argument('--as-path', default=None, help='カンマ区切りのAS_PATH（例: 65003,65004）')
    p_bgp.add_argument('--med', type=int, default=None)
    p_bgp.add_argument('--local-pref', type=int, default=None)
    p_bgp.add_argument('--community', action='append', default=[],
                       help='65001:100 形式、または no-export 等。複数指定可')
    p_bgp.add_argument('--keep-alive-seconds', type=float, default=0,
                       help='Established後、セッションを維持する秒数（0ならすぐclose）')

    args = parser.parse_args()

    if args.proto == 'rip':
        return run_rip(args)
    elif args.proto == 'bgp':
        if args.route and not args.next_hop:
            print('❌ --route を指定する場合は --next-hop も必須です')
            return 1
        return run_bgp(args)
    return 1


if __name__ == '__main__':
    sys.exit(main())
