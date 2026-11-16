"""
スタンドアロン版(実機向け)のセキュリティ・回線の機能をまとめて動かす

- ATT&CK: 警報を ATT&CK の戦術・手法に結びつけ、相手ごと・拠点ごとの進行度を出す(attack_map.py)
- ネクサス: sources.nexus があれば、警報と ATT&CK のイベントを中央へ送り、返ってきた所見を警報にする(nexus.py)。
  この画面自体を中央にもできる(/api/nexus/report を受ける。sources.nexus.token が必要)
- ダークネット: sources.darknet.prefixes に届いたフロー(NetFlow / sFlow)を分類する(darknet.py)
- フロー: sources.flow の NetFlow v5 / sFlow v5 を受ける(flow_collector.py)。端末どうしの通信の粒にも使う
- 外部センサー: Loki から読んだログに、他社セキュリティ製品のアラートがあれば警報にする(sensor_syslog.py)
- 回線チェック: sources.wan_probe の宛先へ本物の ping を打ち、遅延とロスの推移を出す(wan_watch.py)
"""

import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
from collections import deque

from . import sensor_syslog
from .attack_map import AttackTracker
from .darknet import Darknet
from .flow_collector import FlowCollector
from .nexus import NexusHub, NexusReporter
from .wan_watch import WanSeries


def _csum(b):
    if len(b) % 2:
        b += b'\0'
    v = sum(struct.unpack(f'!{len(b) // 2}H', b))
    v = (v >> 16) + (v & 0xFFFF)
    return ~(v + (v >> 16)) & 0xFFFF


def _icmp_once(sock, raw, addr, ident, seq, timeout):
    body = struct.pack('!d', time.time()) + b'nirvana-wan-probe'
    hdr = struct.pack('!BBHHH', 8, 0, 0, ident, seq)
    pkt = struct.pack('!BBHHH', 8, 0, _csum(hdr + body), ident, seq) + body
    t0 = time.perf_counter()
    sock.sendto(pkt, (addr, 0))
    end = t0 + timeout
    while True:
        left = end - time.perf_counter()
        if left <= 0:
            return None
        if not select.select([sock], [], [], left)[0]:
            return None
        data, _ = sock.recvfrom(2048)
        icmp = data[(data[0] & 0x0F) * 4:] if raw else data
        if len(icmp) >= 8 and icmp[0] == 0 and struct.unpack('!H', icmp[6:8])[0] == seq and \
                (not raw or struct.unpack('!H', icmp[4:6])[0] == ident):
            return (time.perf_counter() - t0) * 1000


def ping(target, count=3, timeout=1.0):
    """本物の ICMP echo(平均遅延 ms or None, ロス率)。
    root 不要の ICMP ソケット → raw ソケット(root)→ ping コマンド の順に試す。
    "host:port" と書いた宛先は TCP の接続時間で測る(ICMP が通らない経路向け)"""
    if ':' in target:
        host, port = target.rsplit(':', 1)
        got = []
        for _ in range(count):
            t0 = time.perf_counter()
            try:
                socket.create_connection((host, int(port)), timeout=timeout).close()
                got.append((time.perf_counter() - t0) * 1000)
            except OSError:
                pass
        return (sum(got) / len(got) if got else None), 1 - len(got) / count
    try:
        addr = socket.gethostbyname(target)
    except OSError:
        return None, 1.0
    for kind, raw in ((socket.SOCK_DGRAM, False), (socket.SOCK_RAW, True)):
        try:
            sock = socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP)
        except (PermissionError, OSError):
            continue
        with sock:
            ident = os.getpid() & 0xFFFF
            got = [r for r in (_icmp_once(sock, raw, addr, ident, k + 1, timeout) for k in range(count)) if r is not None]
        return (sum(got) / len(got) if got else None), 1 - len(got) / count
    try:
        r = subprocess.run(['ping', '-n', '-c', str(count), '-W', str(int(timeout) or 1), target],
                           capture_output=True, text=True, timeout=count * (timeout + 1) + 2)
    except (OSError, subprocess.TimeoutExpired):
        return None, 1.0
    loss = re.search(r'([\d.]+)% packet loss', r.stdout)
    rtt = re.search(r'= [\d.]+/([\d.]+)/', r.stdout)
    return (float(rtt.group(1)) if rtt else None), (float(loss.group(1)) / 100 if loss else 1.0)


class SiteSecurity:
    def __init__(self, cfg, collector):
        self.cfg, self.col = cfg, collector
        src = cfg.get('sources') or {}
        a = collector.analytics
        nx = src.get('nexus') or {}
        self.site = nx.get('site') or cfg.get('title') or 'SITE'
        self.tracker = AttackTracker(org=self.site)
        self._alert_seq = 0
        # ダークネット(フローから)
        self.darknet = Darknet(fire=lambda rule, s, d, detail, now: a.fire(rule, s, s, d, detail, now=now))
        if (src.get('darknet') or {}).get('prefixes'):
            self.darknet.set_prefixes(src['darknet']['prefixes'])
        # フロー
        self.flow_events = deque(maxlen=2000)
        self.flow_seq = 0
        f = src.get('flow') or {}
        self.flows = FlowCollector(self.on_flow, bind=f.get('bind', '0.0.0.0'), netflow=f.get('netflow', 0),
                                   sflow=f.get('sflow', 0)) if (f.get('netflow') or f.get('sflow')) else None
        # 回線チェック
        self.wan = WanSeries(fire=lambda rule, key, detail, now: a.fire(rule, key, key, None, detail, now=now))
        self.probes = src.get('wan_probe') or []
        self.probe_interval = float((src.get('wan_probe_interval') or 10))
        # ネクサス
        self.hub = NexusHub()
        self.hub_token = nx.get('token', '')
        self.reporter = NexusReporter(nx['url'], self.site, nx.get('token', ''), lambda: list(a.alerts),
                                      lambda: list(self.tracker.events), self.on_finding,
                                      interval=float(nx.get('interval', 10))) if nx.get('url') else None
        self._stop = threading.Event()

    # ── 入口 ──
    def on_flow(self, fl):
        if self.darknet.prefixes and self.darknet.contains(fl['dst']):
            self.darknet.observe(fl['src'], fl['dst'], fl['proto'], fl.get('sport', 0), fl.get('dport', 0),
                                 fl.get('tcp_flags', 0), length=fl.get('bytes', 0), via=fl.get('via', 'flow'))
        s_dev, d_dev = self.col.dev_of(fl['src']), self.col.dev_of(fl['dst'])
        self.flow_seq += 1
        pk = max(1, int(fl.get('packets', 1)))
        self.flow_events.append({'seq': self.flow_seq, 't': fl['t'], 'src': s_dev or fl['src'], 'dst': d_dev,
                                 'dst_ip': fl['dst'], 'src_ip': fl['src'], 'path': [x for x in (s_dev, d_dev) if x],
                                 'ok': True, 'pkts': pk, 'size': max(1, int(fl.get('bytes', 0)) // pk),
                                 'bytes': int(fl.get('bytes', 0)), 'proto': fl['proto'], 'via': fl.get('via')})

    def on_log(self, dev, msg, t):
        s = sensor_syslog.parse(msg)
        if s:
            self.col.analytics.fire('ext_sensor', f"{s['product']}:{s['src'] or ''}:{s['name']}", s['src'] or dev,
                                    s['dst'], f"[{s['product']}] {s['detail']}", now=t, severity=s['severity'],
                                    name=f"{s['product']}: {s['name']}"[:80])

    def on_finding(self, f):
        self.col.analytics.fire('nexus_finding', f['id'], f.get('src') or self.site, None, '[ネクサス] ' + f['text'])

    # ── 定期処理 ──
    def tick(self):
        new = [x for x in list(self.col.analytics.alerts) if x.get('seq', 0) > self._alert_seq]
        if new:
            self._alert_seq = max(x['seq'] for x in new)
        for x in new:
            self.tracker.record(x['rule'], x.get('src'), x.get('dst'), x.get('detail', ''), now=x['t'])

    def probe_tick(self):
        for p in self.probes:
            rtt, loss = ping(p['target'])
            self.wan.add(f"probe|{p.get('name') or p['target']}", rtt, loss, kind=p.get('kind', 'probe'),
                         cpe='THIS-SITE', peer=p['target'], label=p.get('name') or p['target'], ip=p['target'])

    def start(self):
        if self.flows:
            self.flows.start()
        if self.reporter:
            self.reporter.start()

        def loop():
            last_probe = 0
            while not self._stop.wait(2):
                try:
                    self.tick()
                    if self.probes and time.time() - last_probe >= self.probe_interval:
                        last_probe = time.time()
                        self.probe_tick()
                except Exception as e:  # noqa: BLE001
                    self.col.errors['security'] = str(e)[:200]
        threading.Thread(target=loop, name='cvs-security', daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.flows:
            self.flows.stop()
        if self.reporter:
            self.reporter.stop()
