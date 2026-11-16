"""
組織内ダークネットの観測(NICTER のエッセンス) — /cyber-view の DARKNET 表示用

ダークネット = 使われていない(どの装置も持たない)アドレス帯。正規の通信は来ないので、
届いたパケットはそれだけで「何かおかしい」証拠になる。NICTER の分類にならい、届いたものを
4つに分ける:

- スキャン(scan): 同じ送信元が短時間に多くのダークネットアドレスを叩く(ワーム型の探索活動)
- DDoS の跳ね返り(backscatter): 送信元を偽装された SYN Flood の被害サーバーが返した SYN-ACK / RST、
  ICMP の応答・到達不能。届いた時点(1パケット目)で「どこかが DDoS を受けている」とわかる
- 設定ミス(misconfig): 同じ送信元が同じアドレスへ繰り返し送る(古い設定が使われていないアドレスを指している等)
- リフレクション攻撃の準備(reflection): DNS(53)・NTP(123)・SNMP(161)・SSDP(1900)・memcached(11211) 等の
  踏み台探し(オープンリゾルバ探索など)

入力は2つ:
- 本物のパケット: Linux の lo を AF_PACKET で覗く(root が必要)。ダークネットの帯を
  `ip route add local <帯> dev lo` で手元に引き込み、`iptables -I INPUT -d <帯> -j DROP` で
  カーネルが応答しないようにする(ダークネットは受け身で、何も返さない)。nmap 等の本物のツールの
  通信がそのまま入る
- ラボの ping: エミュレータ内の ping の宛先がダークネットなら ICMP として記録する(root 不要)
"""

import ipaddress
import socket
import struct
import subprocess
import threading
import time
from collections import Counter, deque

CATS = ('scan', 'backscatter', 'misconfig', 'reflection', 'other')
CAT_NAME = {'scan': 'スキャン', 'backscatter': 'DDoS の跳ね返り', 'misconfig': '設定ミス',
            'reflection': 'リフレクション攻撃の準備', 'other': 'その他'}
REFLECT_PORTS = {53: 'DNS', 123: 'NTP', 161: 'SNMP', 1900: 'SSDP', 11211: 'memcached', 19: 'chargen',
                 389: 'CLDAP', 111: 'portmap', 5353: 'mDNS', 3702: 'WS-Discovery'}
# しきい値(NICTER のような大規模観測ではなく、ラボ・組織内の小さな帯向け)
SCAN_WINDOW, SCAN_DSTS = 60, 5          # 60秒に 5アドレス以上 → スキャン
MISCONF_WINDOW, MISCONF_N = 300, 5      # 同じ宛先へ 300秒に 5回以上(スキャンでない) → 設定ミス
# アラート(花びら DARKNET)。同じ送信元・分類は cooldown の間 1回だけ
ALERT_RULE = {'scan': 'darknet_scan', 'backscatter': 'darknet_backscatter',
              'misconfig': 'darknet_misconfig', 'reflection': 'darknet_reflection'}

TCP_FIN, TCP_SYN, TCP_RST, TCP_ACK = 0x01, 0x02, 0x04, 0x10


def classify_packet(proto, sport, dport, flags=0, icmp_type=None):
    """1パケットだけで決まる分類(跳ね返り・リフレクション準備)。決まらなければ None"""
    if proto == 'tcp':
        if (flags & TCP_SYN and flags & TCP_ACK) or (flags & TCP_RST and not flags & TCP_SYN):
            return 'backscatter'
    elif proto == 'icmp':
        if icmp_type in (0, 3, 11):          # echo reply / 到達不能 / 時間超過 = 誰かの応答
            return 'backscatter'
    elif proto == 'udp':
        if dport in REFLECT_PORTS:
            return 'reflection'
        if sport in REFLECT_PORTS and sport != dport:   # 偽装された問い合わせへの応答が返ってきた
            return 'backscatter'
    return None


class Darknet:
    def __init__(self, fire=None, maxlen=20000):
        self._lock = threading.Lock()
        self.prefixes = []
        self.events = deque(maxlen=maxlen)
        self.seq = 0
        self.fire = fire
        self._src_hits = {}                  # src -> deque[(t, dst)]
        self._pair_hits = {}                 # (src, dst, dport) -> deque[t]
        self.days = {}                       # 'YYYY-MM-DD' -> {packets, srcs:set, dsts:set, cats:Counter, protos:Counter}
        self.sensor = None

    # ── 設定 ──────────────────────────────────────
    def set_prefixes(self, prefixes):
        nets = []
        for p in prefixes:
            n = ipaddress.ip_network(str(p).strip(), strict=False)
            if n.version != 4:
                raise ValueError('IPv4 の帯だけ')
            if n.prefixlen < 16:
                raise ValueError(f'{n}: 広すぎます(/16 より狭く)')
            nets.append(n)
        with self._lock:
            self.prefixes = nets

    def contains(self, ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in self.prefixes)

    def size(self):
        return sum(n.num_addresses for n in self.prefixes)

    def clear(self):
        with self._lock:
            self.events.clear()
            self._src_hits.clear()
            self._pair_hits.clear()
            self.days.clear()

    # ── 記録と分類 ────────────────────────────────
    def observe(self, src, dst, proto, sport=0, dport=0, flags=0, icmp_type=None, length=0, via='sensor', now=None):
        if not self.contains(dst):
            return None
        now = time.time() if now is None else now
        cat = classify_packet(proto, sport, dport, flags, icmp_type)
        with self._lock:
            dq = self._src_hits.setdefault(src, deque())
            dq.append((now, dst))
            while dq and now - dq[0][0] > SCAN_WINDOW:
                dq.popleft()
            pq = self._pair_hits.setdefault((src, dst, dport), deque())
            pq.append(now)
            while pq and now - pq[0] > MISCONF_WINDOW:
                pq.popleft()
            if cat is None:
                if len({d for _, d in dq}) >= SCAN_DSTS:
                    cat = 'scan'
                elif len(pq) >= MISCONF_N:
                    cat = 'misconfig'
                else:
                    cat = 'other'
            self.seq += 1
            ev = {'seq': self.seq, 't': now, 'src': src, 'dst': dst, 'proto': proto, 'sport': sport,
                  'dport': dport, 'flags': flags, 'icmp_type': icmp_type, 'len': length, 'cat': cat, 'via': via}
            self.events.append(ev)
            day = time.strftime('%Y-%m-%d', time.localtime(now))
            d = self.days.setdefault(day, {'packets': 0, 'srcs': set(), 'dsts': set(),
                                           'cats': Counter(), 'protos': Counter()})
            d['packets'] += 1
            d['srcs'].add(src)
            d['dsts'].add(dst)
            d['cats'][cat] += 1
            d['protos'][proto] += 1
            if len(self.days) > 31:
                self.days.pop(min(self.days))
        if cat in ALERT_RULE and self.fire:
            self.fire(ALERT_RULE[cat], src, dst, self.describe(ev), now)
        return ev

    @staticmethod
    def describe(ev):
        port = f":{ev['dport']}" if ev['proto'] in ('tcp', 'udp') else ''
        what = {'scan': 'ダークネットを探索', 'backscatter': 'DDoS の跳ね返り(偽装された送信元への応答)',
                'misconfig': '使われていないアドレスへ繰り返し送信(設定ミスの疑い)',
                'reflection': f"リフレクション攻撃の踏み台探し({REFLECT_PORTS.get(ev['dport'], '')})",
                'other': 'ダークネットへの通信'}[ev['cat']]
        return f"{ev['src']} → {ev['dst']}{port} {ev['proto'].upper()} {what}"

    # ── 画面・API 用 ──────────────────────────────
    def snapshot(self, since=0, limit=400):
        with self._lock:
            evs = [e for e in self.events if e['seq'] > since][-limit:]
            size = self.size() or 1
            days = [{'day': k, 'packets': v['packets'], 'srcs': len(v['srcs']), 'dsts': len(v['dsts']),
                     'per_ip': round(v['packets'] / size, 2), 'cats': dict(v['cats']), 'protos': dict(v['protos'])}
                    for k, v in sorted(self.days.items(), reverse=True)[:14]]
            recent = [e for e in self.events if e['t'] > time.time() - 3600]
            top_src = Counter(e['src'] for e in recent).most_common(8)
            top_port = Counter(f"{e['proto']}/{e['dport']}" for e in recent if e['proto'] != 'icmp').most_common(8)
            cats = Counter(e['cat'] for e in recent)
        return {'prefixes': [str(n) for n in self.prefixes], 'size': self.size(), 'seq': self.seq,
                'events': [{**e, 'text': self.describe(e)} for e in evs], 'days': days,
                'hour': {'cats': {c: cats.get(c, 0) for c in CATS}, 'top_src': top_src, 'top_port': top_port},
                'sensor': self.sensor.status() if self.sensor else {'running': False}}


# ── 本物のパケットを受けるセンサー(Linux・root) ──────────
ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4


def parse_frame(buf):
    """lo の擬似イーサネットフレーム → (src, dst, proto, sport, dport, flags, icmp_type, len)。IPv4 以外は None"""
    if len(buf) < 34 or buf[12:14] != b'\x08\x00':
        return None
    ip = buf[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[0] >> 4 != 4 or len(ip) < ihl:
        return None
    total = struct.unpack('!H', ip[2:4])[0]
    p = ip[9]
    src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
    l4 = ip[ihl:]
    if p == 6 and len(l4) >= 14:
        sp, dp = struct.unpack('!HH', l4[:4])
        return src, dst, 'tcp', sp, dp, l4[13], None, total
    if p == 17 and len(l4) >= 4:
        sp, dp = struct.unpack('!HH', l4[:4])
        return src, dst, 'udp', sp, dp, 0, None, total
    if p == 1 and len(l4) >= 1:
        return src, dst, 'icmp', 0, 0, 0, l4[0], total
    return None


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


class LoSensor:
    """ダークネットの帯を lo に引き込み、届いたパケットを受け身で数える"""

    def __init__(self, darknet):
        self.dn = darknet
        self._stop = threading.Event()
        self._th = None
        self.routes, self.drops, self.notes = [], [], []
        self.passive = False
        self.error = None

    def status(self):
        return {'running': bool(self._th and self._th.is_alive()), 'passive': self.passive,
                'error': self.error, 'notes': self.notes[-5:]}

    def start(self):
        self.stop()
        self._stop.clear()
        self.notes, self.error = [], None
        self.passive = True
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            sock.bind(('lo', 0))
            sock.settimeout(0.5)
        except (OSError, AttributeError) as e:
            self.error = f'パケットを受けられません(root と Linux が必要): {e}'
            self.passive = False
            return False
        for n in self.dn.prefixes:
            ok, msg = _run(['ip', 'route', 'replace', 'local', str(n), 'dev', 'lo', 'table', 'local'])
            if ok:
                self.routes.append(str(n))
            else:
                self.notes.append(f'{n}: 経路を引き込めない({msg})')
            ok, msg = _run(['iptables', '-I', 'INPUT', '-d', str(n), '-j', 'DROP'])
            if ok:
                self.drops.append(str(n))
            else:
                self.passive = False
                self.notes.append(f'{n}: 応答を止められない(カーネルが RST 等を返す): {msg}')

        def loop():
            while not self._stop.is_set():
                try:
                    buf, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if addr[2] == PACKET_OUTGOING:       # lo は送りと受けの2回見えるので受けだけ
                    continue
                r = parse_frame(buf)
                if r and self.dn.contains(r[1]):
                    self.dn.observe(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], via='sensor')
            sock.close()
        self._th = threading.Thread(target=loop, name='darknet-sensor', daemon=True)
        self._th.start()
        return True

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(2)
        self._th = None
        for n in self.drops:
            _run(['iptables', '-D', 'INPUT', '-d', n, '-j', 'DROP'])
        for n in self.routes:
            _run(['ip', 'route', 'del', 'local', n, 'dev', 'lo', 'table', 'local'])
        self.routes, self.drops = [], []


darknet = Darknet()
