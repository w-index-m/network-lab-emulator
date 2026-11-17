"""
MAWI(WIDE プロジェクトの公開トラフィックアーカイブ)の pcap をラボに流し込む

MAWI は日米間の回線(samplepoint-F)で毎日15分ずつ取った本物のパケットのヘッダを、IP アドレスを
匿名化(上位ビットの構造を保つ方式)して公開している。URL は
  https://mawi.wide.ad.jp/mawi/samplepoint-F/<年>/<年月日時分>.pcap.gz   (例: 202411151400)
大きい(数 GB)ので、ダウンロードしながら gzip を展開し、先頭から指定のパケット数だけ読む。
手元の .pcap / .pcap.gz も読める。

読み込んだパケットは、pcap の時刻で1秒ごとにフロー(送信元・宛先・プロトコル・宛先ポート)へまとめ、
多い順に上位だけを、指定の速さ(1 = 実時間)で on_flow(NetFlow / sFlow と同じ形の dict)へ渡す。
アドレスは匿名化されているので、国・クラウドの判定には使えない(プロトコル・ポート・量・形は本物)。
"""
import gzip
import io
import struct
import threading
import time
import urllib.request
from collections import Counter, defaultdict

MAWI_URL = 'https://mawi.wide.ad.jp/mawi/samplepoint-F/{y}/{stamp}.pcap.gz'
PROTO = {1: 'icmp', 6: 'tcp', 17: 'udp', 47: 'gre', 50: 'esp', 58: 'icmp6'}


def mawi_url(stamp):
    """'202411151400' → MAWI の URL"""
    return MAWI_URL.format(y=stamp[:4], stamp=stamp)


def open_source(src, timeout=30):
    """ローカルのファイル(.pcap / .pcap.gz)か URL を、バイナリのストリームとして開く"""
    if src.startswith(('http://', 'https://')):
        resp = urllib.request.urlopen(urllib.request.Request(src, headers={'User-Agent': 'network-lab-emulator'}),
                                      timeout=timeout)
        return gzip.GzipFile(fileobj=resp) if src.endswith('.gz') else resp
    f = open(src, 'rb')
    head = f.read(2)
    f.seek(0)
    return gzip.GzipFile(fileobj=f) if head == b'\x1f\x8b' else f


def _read(f, n):
    b = b''
    while len(b) < n:
        chunk = f.read(n - len(b))
        if not chunk:
            break
        b += chunk
    return b


def iter_packets(f, limit=None):
    """pcap(リンク層 Ethernet=1 / Raw IP=101・228 / Linux SLL=113)から IPv4 のパケットを取り出す。
    yield {'t', 'src', 'dst', 'proto', 'sport', 'dport', 'flags', 'length'}"""
    gh = _read(f, 24)
    if len(gh) < 24:
        raise ValueError('pcap のヘッダがありません')
    magic = gh[:4]
    if magic in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
        e = '<'
    elif magic in (b'\xa1\xb2\xc3\xd4', b'\xa1\xb2\x3c\x4d'):
        e = '>'
    else:
        raise ValueError('pcap ではありません(pcapng は未対応)')
    nano = magic in (b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d')
    linktype = struct.unpack(e + 'I', gh[20:24])[0]
    n = 0
    while limit is None or n < limit:
        ph = _read(f, 16)
        if len(ph) < 16:
            return
        sec, frac, incl, orig = struct.unpack(e + 'IIII', ph)
        data = _read(f, incl)
        if len(data) < incl:
            return
        t = sec + frac / (1e9 if nano else 1e6)
        off = 0
        if linktype == 1:                       # Ethernet(VLAN タグは読み飛ばす)
            et = struct.unpack('!H', data[12:14])[0] if len(data) >= 14 else 0
            off = 14
            while et in (0x8100, 0x88A8) and len(data) >= off + 4:
                et = struct.unpack('!H', data[off + 2:off + 4])[0]
                off += 4
            if et != 0x0800:
                continue
        elif linktype == 113:                   # Linux cooked
            if len(data) < 16 or struct.unpack('!H', data[14:16])[0] != 0x0800:
                continue
            off = 16
        elif linktype not in (101, 228, 12):
            raise ValueError(f'未対応のリンク層 {linktype}')
        ip = data[off:]
        if len(ip) < 20 or ip[0] >> 4 != 4:
            continue
        ihl = (ip[0] & 0x0F) * 4
        proto = ip[9]
        src = '.'.join(str(b) for b in ip[12:16])
        dst = '.'.join(str(b) for b in ip[16:20])
        sport = dport = flags = 0
        l4 = ip[ihl:]
        if proto in (6, 17) and len(l4) >= 4:
            sport, dport = struct.unpack('!HH', l4[:4])
            if proto == 6 and len(l4) >= 14:
                flags = l4[13]
        elif proto == 1 and len(l4) >= 2:
            sport, dport = l4[0], l4[1]          # ICMP の type / code
        n += 1
        yield {'t': t, 'src': src, 'dst': dst, 'proto': PROTO.get(proto, str(proto)), 'sport': sport, 'dport': dport,
               'flags': flags, 'length': orig}


def aggregate(packets):
    """1秒ごとに (送信元, 宛先, プロトコル, 宛先ポート) でまとめる → [(秒, [flow...]), ...]"""
    by_sec = defaultdict(lambda: defaultdict(lambda: {'packets': 0, 'bytes': 0, 'tcp_flags': 0, 'sport': 0}))
    for p in packets:
        k = (p['src'], p['dst'], p['proto'], p['dport'])
        f = by_sec[int(p['t'])][k]
        f['packets'] += 1
        f['bytes'] += p['length']
        f['tcp_flags'] |= p['flags']
        f['sport'] = p['sport']
    out = []
    for sec in sorted(by_sec):
        flows = [{'src': k[0], 'dst': k[1], 'proto': k[2], 'dport': k[3], 'via': 'mawi', **v}
                 for k, v in by_sec[sec].items()]
        out.append((sec, sorted(flows, key=lambda x: -x['bytes'])))
    return out


class Replayer:
    def __init__(self):
        self.state = {'running': False}
        self._stop = threading.Event()
        self._thread = None

    def status(self):
        return dict(self.state)

    def start(self, source, on_flow, limit=200000, speed=1.0, topk=40, loop=False):
        if self.state.get('running'):
            raise RuntimeError('すでに再生中です')
        self._stop.clear()
        self.state = {'running': True, 'source': source, 'limit': limit, 'speed': speed, 'topk': topk,
                      'phase': 'reading', 'packets': 0, 'seconds': 0, 'played': 0, 'flows_sent': 0,
                      'bytes': 0, 'protocols': {}, 'top_ports': [], 'error': None, 'started': time.time()}
        self._thread = threading.Thread(target=self._run, args=(source, on_flow, limit, speed, topk, loop), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, source, on_flow, limit, speed, topk, loop):
        st = self.state
        try:
            f = open_source(source)
            pk = list(iter_packets(f, limit))
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
            st['packets'] = len(pk)
            if not pk:
                raise ValueError('IPv4 のパケットがありませんでした')
            protos = Counter(p['proto'] for p in pk)
            ports = Counter(p['dport'] for p in pk if p['proto'] in ('tcp', 'udp'))
            st['protocols'] = dict(protos.most_common())
            st['top_ports'] = ports.most_common(10)
            st['bytes'] = sum(p['length'] for p in pk)
            st['capture_start'], st['capture_end'] = pk[0]['t'], pk[-1]['t']
            secs = aggregate(pk)
            st['seconds'] = len(secs)
            st['phase'] = 'playing'
            while True:
                prev = None
                for sec, flows in secs:
                    if self._stop.is_set():
                        return
                    if prev is not None:
                        self._stop.wait(max(0.0, (sec - prev) / max(0.01, speed)))
                    prev = sec
                    for fl in flows[:topk]:
                        on_flow(fl)
                    st['played'] += 1
                    st['flows_sent'] += min(topk, len(flows))
                    st['now'] = sec
                if not loop or self._stop.is_set():
                    break
            st['phase'] = 'done'
        except Exception as e:  # noqa: BLE001
            st['error'] = str(e)[:300]
            st['phase'] = 'error'
        finally:
            st['running'] = False


replayer = Replayer()


def write_pcap(packets, linktype=1):
    """テストと見本用: [{'t','src','dst','proto','sport','dport','flags','length'}] → pcap のバイト列(Ethernet)"""
    out = io.BytesIO()
    out.write(struct.pack('<IHHiIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, linktype))
    rev = {v: k for k, v in PROTO.items()}
    for p in packets:
        pn = rev.get(p['proto'], 6)
        if pn == 6:
            l4 = struct.pack('!HHIIBBHHH', p.get('sport', 40000), p.get('dport', 80), 0, 0, 0x50, p.get('flags', 0x02), 0, 0, 0)
        elif pn == 17:
            l4 = struct.pack('!HHHH', p.get('sport', 40000), p.get('dport', 53), 8, 0)
        else:
            l4 = struct.pack('!BBHHH', p.get('sport', 8), p.get('dport', 0), 0, 0, 0)
        ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(l4), 0, 0, 64, pn, 0,
                         bytes(int(x) for x in p['src'].split('.')), bytes(int(x) for x in p['dst'].split('.'))) + l4
        frame = (b'\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00' if linktype == 1 else b'') + ip
        sec = int(p['t'])
        out.write(struct.pack('<IIII', sec, int((p['t'] - sec) * 1e6), len(frame), max(len(frame), p.get('length', len(frame)))))
        out.write(frame)
    return out.getvalue()
