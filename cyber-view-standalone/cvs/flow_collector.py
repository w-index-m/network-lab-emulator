"""
NetFlow v5 / sFlow v5 の受け口 — NIRVANA Rapps(ミラーポート・sFlow で実際の通信を見せる)のエッセンス

スイッチ・ルータ・softflowd などが送るフロー情報を UDP で受け、1件ずつ
{t, src, dst, proto, sport, dport, bytes, packets, exporter} にして渡す。
ニルヴァーナは受け取ったフローを、端末どうしの通信の粒(NETWORK)・ダークネット観測・外部への大量送信の検知に使う。

- NetFlow v5(既定 UDP 2055): ヘッダ 24バイト + レコード 48バイト × count
- sFlow v5(既定 UDP 6343): flow sample(1)/ expanded flow sample(3)の raw packet header(1)から
  Ethernet → IPv4 → TCP/UDP を読み、frame_length × sampling_rate を推定バイト数にする

標準ライブラリだけ(ラボ版・スタンドアロン版で同じファイルを使う)。
"""

import socket
import struct
import threading
import time

PROTO = {1: 'icmp', 6: 'tcp', 17: 'udp'}


def _ip(b):
    return socket.inet_ntoa(b)


def parse_netflow_v5(data, exporter='', now=None):
    now = time.time() if now is None else now
    if len(data) < 24:
        return []
    version, count = struct.unpack('!HH', data[:4])
    if version != 5:
        return []
    out = []
    for k in range(min(count, (len(data) - 24) // 48)):
        r = data[24 + 48 * k: 24 + 48 * (k + 1)]
        src, dst = _ip(r[0:4]), _ip(r[4:8])
        pkts, octets = struct.unpack('!II', r[16:24])
        sport, dport = struct.unpack('!HH', r[32:36])
        flags, proto = r[37], r[38]
        out.append({'t': now, 'src': src, 'dst': dst, 'proto': PROTO.get(proto, str(proto)), 'sport': sport,
                    'dport': dport, 'bytes': octets, 'packets': pkts, 'tcp_flags': flags, 'exporter': exporter,
                    'via': 'netflow'})
    return out


def _parse_eth_ipv4(h):
    """sFlow の raw packet header(Ethernet から)→ (src, dst, proto, sport, dport, flags)"""
    off = 14
    if len(h) < off:
        return None
    et = struct.unpack('!H', h[12:14])[0]
    if et == 0x8100 and len(h) >= 18:                     # VLAN タグ
        et, off = struct.unpack('!H', h[16:18])[0], 18
    if et != 0x0800 or len(h) < off + 20:
        return None
    ip = h[off:]
    ihl = (ip[0] & 0x0F) * 4
    p = ip[9]
    src, dst = _ip(ip[12:16]), _ip(ip[16:20])
    l4 = ip[ihl:]
    sport = dport = flags = 0
    if p in (6, 17) and len(l4) >= 4:
        sport, dport = struct.unpack('!HH', l4[:4])
        if p == 6 and len(l4) >= 14:
            flags = l4[13]
    return src, dst, PROTO.get(p, str(p)), sport, dport, flags


def parse_sflow_v5(data, exporter='', now=None):
    now = time.time() if now is None else now
    out = []
    try:
        version, atype = struct.unpack('!II', data[:8])
        if version != 5:
            return []
        i = 8 + (4 if atype == 1 else 16)
        agent = _ip(data[8:12]) if atype == 1 else exporter
        _sub, _seq, _up, nsamples = struct.unpack('!IIII', data[i:i + 16])
        i += 16
        for _ in range(nsamples):
            fmt, ln = struct.unpack('!II', data[i:i + 8])
            body = data[i + 8: i + 8 + ln]
            i += 8 + ln
            ent, kind = fmt >> 12, fmt & 0xFFF
            if ent != 0 or kind not in (1, 3):
                continue
            if kind == 1:          # flow sample
                _sq, _src_id, rate, _pool, _drops, _inp, _outp, nrec = struct.unpack('!IIIIIIII', body[:32])
                j = 32
            else:                  # expanded flow sample
                _sq, _st, _si, rate, _pool, _drops, _if, _iv, _of, _ov, nrec = struct.unpack('!IIIIIIIIIII', body[:44])
                j = 44
            for _r in range(nrec):
                rfmt, rln = struct.unpack('!II', body[j:j + 8])
                rec = body[j + 8: j + 8 + rln]
                j += 8 + rln
                if rfmt != 1 or len(rec) < 16:            # raw packet header のみ
                    continue
                hproto, frame_len, _stripped, hlen = struct.unpack('!IIII', rec[:16])
                if hproto != 1:                           # Ethernet
                    continue
                x = _parse_eth_ipv4(rec[16:16 + hlen])
                if not x:
                    continue
                out.append({'t': now, 'src': x[0], 'dst': x[1], 'proto': x[2], 'sport': x[3], 'dport': x[4],
                            'tcp_flags': x[5], 'bytes': frame_len * max(1, rate), 'packets': max(1, rate),
                            'exporter': agent or exporter, 'via': 'sflow', 'sampling_rate': rate})
    except (struct.error, IndexError):
        pass
    return out


class FlowCollector:
    """UDP でフローを受け、on_flow(flow) を呼ぶ。ports = {'netflow': 2055, 'sflow': 6343}"""

    def __init__(self, on_flow, bind='0.0.0.0', netflow=2055, sflow=6343):
        self.on_flow = on_flow
        self.bind, self.ports = bind, {'netflow': netflow, 'sflow': sflow}
        self.stats = {'netflow': 0, 'sflow': 0, 'flows': 0, 'errors': {}}
        self._socks, self._stop = [], threading.Event()

    def start(self):
        for kind, port in self.ports.items():
            if not port:
                continue
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.bind, int(port)))
                s.settimeout(0.5)
            except OSError as e:
                self.stats['errors'][kind] = f'UDP {port} を開けません: {e}'
                continue
            self._socks.append(s)
            threading.Thread(target=self._loop, args=(s, kind), name=f'flow-{kind}', daemon=True).start()
        return self

    def _loop(self, s, kind):
        parse = parse_netflow_v5 if kind == 'netflow' else parse_sflow_v5
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.stats[kind] += 1
            for f in parse(data, addr[0]):
                self.stats['flows'] += 1
                try:
                    self.on_flow(f)
                except Exception:  # noqa: BLE001 - 1件の失敗で受け口を止めない
                    pass

    def stop(self):
        self._stop.set()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass
        self._socks = []
