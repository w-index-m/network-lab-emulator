"""
トラフィックの記録・侵害検知・再現 (CyNote 風)

NICT の NIRVANA改 を補強する構造計画研究所の「CyNote」が持つ2機能を、
このエミュレータの /traffic-flow 可視化向けに小さく再現する。

- FlowRecorder: ping ごとのフローイベント(経路・バイト数・成否)と、
  リンクの up/down を時刻付きで保存する(トラフィック再現機構の「蓄積」側)。
  query() で期間・送信元・宛先・結果を指定して取り出し、可視化ページが
  当時の時刻どおりに再生する。
- Detector: 記録と同時に流れてくるイベントをルールで評価し、アラートを
  出す(セグメント侵害検知の簡易版)。ルールは下の RULES を参照。

保存はプロセス内メモリのみ(件数と保持時間で上限)。再起動すると消える。
"""

import threading
import time
from collections import defaultdict, deque

MAX_EVENTS = 20000
MAX_LINK_EVENTS = 5000
MAX_ALERTS = 2000
RETENTION_SEC = 24 * 3600

# 検知ルールのしきい値 (window 秒の間に threshold 以上で発報、cooldown 秒は同じ組を再発報しない)
RULES = {
    'scan': {'name': 'ICMPスイープ(スキャン)の疑い', 'severity': 'high',
             'window': 60, 'threshold': 8, 'cooldown': 120,
             'desc': '同じ送信元から短時間に多数の異なる宛先IPへ通信'},
    'unreach_burst': {'name': '到達不可の通信が連続', 'severity': 'medium',
                      'window': 60, 'threshold': 5, 'cooldown': 120,
                      'desc': '存在しない宛先への探索、または経路断・設定ミス'},
    'flood': {'name': '大量トラフィック(フラッド)の疑い', 'severity': 'high',
              'window': 10, 'threshold': 30 * 1024 * 1024, 'cooldown': 60,
              'desc': '同じ送信元→宛先の組で短時間に大量のバイト数'},
    'large_icmp': {'name': 'MTU超のICMP(フラグメント)', 'severity': 'low',
                   'window': 0, 'threshold': 1500, 'cooldown': 300,
                   'desc': 'ICMPのサイズがMTU(1500byte)を超えている'},
    'link_down': {'name': 'リンク断', 'severity': 'medium',
                  'window': 0, 'threshold': 0, 'cooldown': 0,
                  'desc': 'インタフェースのshutdown等でリンクが落ちた'},
}
SEVERITY_ORDER = {'low': 0, 'medium': 1, 'high': 2}


class FlowRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.events = deque(maxlen=MAX_EVENTS)
        self.link_events = deque(maxlen=MAX_LINK_EVENTS)
        self.alerts = deque(maxlen=MAX_ALERTS)
        self.seq = 0
        self.alert_seq = 0
        # 検知用の作業領域
        self._by_src = defaultdict(deque)          # src -> deque[(t, dst_ip, ok)]
        self._by_pair = defaultdict(deque)         # (src, dst_ip) -> deque[(t, bytes)]
        self._last_fired = {}                      # (rule, key) -> t

    # ── 記録 ──────────────────────────────────────
    def record_flow(self, src, dst_ip, dst, path, ok, pkts, size, proto='icmp', now=None, src_ip=None):
        now = time.time() if now is None else now
        with self._lock:
            self.seq += 1
            ev = {'seq': self.seq, 't': now, 'proto': proto, 'src': src, 'src_ip': src_ip, 'dst_ip': dst_ip,
                  'dst': dst, 'path': list(path), 'ok': bool(ok), 'pkts': pkts,
                  'size': size, 'bytes': pkts * size, 'alerts': []}
            self.events.append(ev)
            for a in self._detect_flow(ev):
                ev['alerts'].append(a['seq'])
            self._expire(now)
            return ev

    def record_link(self, a, b, iface, state, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self.link_events.append({'t': now, 'a': a, 'b': b, 'iface': iface, 'state': state})
            if state == 'down':
                self._fire('link_down', f'{a}|{iface}', now, src=a, dst=b,
                           detail=f'{iface} が down ({a} - {b})', related=[])

    def _expire(self, now):
        cutoff = now - RETENTION_SEC
        for dq in (self.events, self.link_events, self.alerts):
            while dq and dq[0]['t'] < cutoff:
                dq.popleft()

    # ── 検知 ──────────────────────────────────────
    def _fire(self, rule, key, now, src, dst, detail, related):
        r = RULES[rule]
        last = self._last_fired.get((rule, key))
        if last is not None and r['cooldown'] and now - last < r['cooldown']:
            return None
        self._last_fired[(rule, key)] = now
        self.alert_seq += 1
        a = {'seq': self.alert_seq, 't': now, 'rule': rule, 'name': r['name'],
             'severity': r['severity'], 'src': src, 'dst': dst, 'detail': detail,
             'related': related}
        self.alerts.append(a)
        return a

    def _detect_flow(self, ev):
        out = []
        now, src = ev['t'], ev['src']
        hist = self._by_src[src]
        hist.append((now, ev['dst_ip'], ev['ok'], ev['seq']))
        horizon = max(RULES['scan']['window'], RULES['unreach_burst']['window'])
        while hist and now - hist[0][0] > horizon:
            hist.popleft()

        r = RULES['scan']
        recent = [h for h in hist if now - h[0] <= r['window']]
        dsts = {h[1] for h in recent}
        if len(dsts) >= r['threshold']:
            a = self._fire('scan', src, now, src, None,
                           f'{r["window"]}秒間に {len(dsts)} 個の宛先へ通信',
                           [h[3] for h in recent])
            if a:
                out.append(a)

        r = RULES['unreach_burst']
        fails = [h for h in hist if not h[2] and now - h[0] <= r['window']]
        if len(fails) >= r['threshold']:
            a = self._fire('unreach_burst', src, now, src, None,
                           f'{r["window"]}秒間に到達不可が {len(fails)} 回', [h[3] for h in fails])
            if a:
                out.append(a)

        r = RULES['flood']
        pair = self._by_pair[(src, ev['dst_ip'])]
        pair.append((now, ev['bytes'], ev['seq']))
        while pair and now - pair[0][0] > r['window']:
            pair.popleft()
        total = sum(p[1] for p in pair)
        if total >= r['threshold']:
            a = self._fire('flood', f'{src}|{ev["dst_ip"]}', now, src, ev['dst_ip'],
                           f'{r["window"]}秒間に {total / 1048576:.1f} MB', [p[2] for p in pair])
            if a:
                out.append(a)

        r = RULES['large_icmp']
        if ev['proto'] == 'icmp' and ev['size'] > r['threshold']:
            a = self._fire('large_icmp', f'{src}|{ev["dst_ip"]}', now, src, ev['dst_ip'],
                           f'size {ev["size"]} byte', [ev['seq']])
            if a:
                out.append(a)
        return out

    # ── 取り出し ──────────────────────────────────
    def since(self, seq, alert_seq):
        with self._lock:
            return ([e for e in self.events if e['seq'] > seq],
                    [a for a in self.alerts if a['seq'] > alert_seq],
                    self.seq, self.alert_seq)

    def query(self, start=None, end=None, src=None, dst=None, result='all', limit=MAX_EVENTS):
        """再現用: 期間と条件で絞り込んだイベント、リンク状態の変化、アラートを返す。

        dst は宛先IPか宛先装置ID。result は all / ok / ng。
        リンク状態は「期間開始時点の状態」を再現できるよう、end までの全件を返す。
        """
        with self._lock:
            evs = []
            for e in self.events:
                if start is not None and e['t'] < start:
                    continue
                if end is not None and e['t'] > end:
                    continue
                if src and e['src'] != src:
                    continue
                if dst and dst not in (e['dst_ip'], e['dst']):
                    continue
                if result == 'ok' and not e['ok'] or result == 'ng' and e['ok']:
                    continue
                evs.append(e)
            links = [x for x in self.link_events if end is None or x['t'] <= end]
            alerts = [a for a in self.alerts
                      if (start is None or a['t'] >= start) and (end is None or a['t'] <= end)]
            first = self.events[0]['t'] if self.events else None
            return {'events': evs[-limit:], 'link_events': links, 'alerts': alerts,
                    'truncated': len(evs) > limit, 'oldest': first}

    def clear(self):
        with self._lock:
            self.events.clear(); self.link_events.clear(); self.alerts.clear()
            self._by_src.clear(); self._by_pair.clear(); self._last_fired.clear()


recorder = FlowRecorder()
