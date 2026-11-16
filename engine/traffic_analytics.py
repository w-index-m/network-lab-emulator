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

import re
import threading
import time
from collections import defaultdict, deque

MAX_EVENTS = 20000
MAX_LINK_EVENTS = 5000
MAX_ALERTS = 2000
MAX_LOGS = 5000
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
    'conn_scan': {'name': '短時間の大量接続(ポートスキャン/総当たりの疑い)', 'severity': 'high',
                  'window': 30, 'threshold': 10, 'cooldown': 60,
                  'desc': '同じ送信元から装置の実リスナー(SSH/Telnet)へ短時間に多数の接続'},
    'fw_shun': {'name': 'FWがスキャン元を遮断(shun)', 'severity': 'high',
                'window': 0, 'threshold': 0, 'cooldown': 30,
                'desc': 'ASA の threat-detection scanning-threat shun が送信元を遮断した'},
    'syslog_error': {'name': '装置が error 以上のログを出力', 'severity': 'medium',
                     'window': 0, 'threshold': 3, 'cooldown': 60,
                     'desc': 'syslog 重要度3(error)以下のメッセージ(復旧通知は除く)'},
}
# フラップ(状態の繰り返し)は「警告」扱い。ルールの中身は engine/flap_rules.py
from engine.flap_rules import FLAP_RULES as _FLAP_RULES, ROUTE_FLAP as _ROUTE_FLAP  # noqa: E402
for _r in _FLAP_RULES + [_ROUTE_FLAP]:
    RULES.setdefault(_r['id'], {'name': _r['name'], 'severity': 'warning', 'window': _r['window'],
                                'threshold': _r['threshold'], 'cooldown': max(60, _r['window']),
                                'desc': _r['desc']})
FLAP_RULE_IDS = {_r['id'] for _r in _FLAP_RULES + [_ROUTE_FLAP]}
# 検知元(センサー)。/cyber-view の六角形の花びらはこの単位でまとめる
RULE_SOURCE = {**{_k: 'SYSLOG' for _k in FLAP_RULE_IDS}, 'route_flap': 'NET', 'conn_scan': 'IDS', 'scan': 'IDS', 'unreach_burst': 'IDS', 'flood': 'IDS', 'large_icmp': 'IDS',
               'fw_shun': 'FW', 'syslog_error': 'SYSLOG', 'link_down': 'NET'}
SECURITY_SOURCES = {'IDS', 'FW'}
_TS_PREFIX = re.compile(r'^\*?[A-Z][a-z]{2} +\d+ [\d:.]+: ')
# 復旧を知らせるメッセージ(実機のCiscoでは up も %LINK-3 なので重要度だけでは区別できない)
RECOVERY = re.compile(r'state to up|to FULL|Established|Link ?Up|復旧', re.I)
_CISCO_SEV = re.compile(r'%[\w-]+?-([0-7])-\w+')
SEVERITY_ORDER = {'low': 0, 'warning': 1, 'medium': 1, 'high': 2}


class FlowRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.events = deque(maxlen=MAX_EVENTS)
        self.link_events = deque(maxlen=MAX_LINK_EVENTS)
        self.alerts = deque(maxlen=MAX_ALERTS)
        self.logs = deque(maxlen=MAX_LOGS)          # 装置が出したsyslog相当のメッセージ
        self.seq = 0
        self.alert_seq = 0
        # 検知用の作業領域
        self._by_src = defaultdict(deque)          # src -> deque[(t, dst_ip, ok)]
        self._by_pair = defaultdict(deque)         # (src, dst_ip) -> deque[(t, bytes)]
        self._last_fired = {}                      # (rule, key) -> t
        self._conns = {}                           # (dev, src_ip) -> deque[(t, port)]
        self._flap = {}                            # (rule, dev, key) -> deque[(t, state)]
        self._flap_last = {}                       # (rule, dev, key) -> 直前の状態
        self.device_type_of = None                 # fn(dev) -> device_type(app が設定)

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

    def record_log(self, dev, hostname, severity, msg, now=None):
        """syslog相当のメッセージを記録(severity は syslog の 0=emerg 〜 7=debug)。
        同じ装置・同じ文面が1秒以内に重なったら1件にまとめる(同じ事象を
        実syslog送信とCLI通知の両方で拾うため)。"""
        now = time.time() if now is None else now
        msg = _TS_PREFIX.sub('', msg)          # 装置側の時刻プレフィックスは重複判定の邪魔なので外す
        m = _CISCO_SEV.search(msg)
        if m:                      # %FAC-<sev>-MNEMONIC の重要度を優先(実機のsyslog重要度と同じ)
            severity = int(m.group(1))
        with self._lock:
            # 同じ装置・同文・1秒以内は重複(実syslog送信とCLI通知の両方で拾うため)。
            # ただし間に同じIFの逆の状態変化が挟まっていれば別の出来事なので残す
            # (shutdown→no shutdown を素早く繰り返しても最後の up を落とさない)
            from engine.link_evidence import parse_log_link_event, norm_if
            me = parse_log_link_event(msg)
            for x in reversed(list(self.logs)[-50:]):
                if x['dev'] != dev or now - x['t'] >= 1:
                    continue
                if x['msg'] == msg:
                    return None
                ev = parse_log_link_event(x['msg'])
                if me and ev and norm_if(ev[0]) == norm_if(me[0]) and ev[1] != me[1]:
                    break
            ev = {'t': now, 'dev': dev, 'host': hostname, 'sev': int(severity), 'msg': msg}
            self.logs.append(ev)
            if ev['sev'] <= RULES['syslog_error']['threshold'] and not RECOVERY.search(msg):
                self._fire('syslog_error', dev, now, src=dev, dst=None, detail=msg[:160], related=[])
            self._detect_flap_log(dev, msg, now)
            return ev

    def _flap_count(self, rule_id, dev, key, state, now, window):
        """状態変化(前回と違う状態)だけを数え、window 内の回数を返す"""
        events = rule_id in ('stp_tc_flap', 'stp_root_flap', 'mac_flap', 'errdisable_flap', 'route_flap')
        k = (rule_id, dev, key)
        if not events:
            last = self._flap_last.get(k)
            self._flap_last[k] = state
            if last is None or last == state:
                # 最初に見た状態(設定時の no shutdown 等)は基準。同じ状態の重複(LINK と LINEPROTO 等)も数えない
                return None
        dq = self._flap.setdefault(k, deque())
        dq.append((now, state))
        while dq and window and now - dq[0][0] > window:
            dq.popleft()
        return len(dq)

    def _detect_flap_log(self, dev, msg, now):
        from engine.flap_rules import match
        dt = None
        if self.device_type_of:
            try:
                dt = self.device_type_of(dev)
            except Exception:  # noqa: BLE001
                dt = None
        hit = match(dt, msg)
        if not hit:
            return None
        r, key, state = hit
        n = self._flap_count(r['id'], dev, key, state, now, r['window'])
        if n is None or n < r['threshold']:
            return None
        detail = (f"{key}: {r['window']}秒に {n} 回 状態変化" if r['threshold'] > 1
                  else f"{key}: {msg[msg.find('%'):][:120] if '%' in msg else msg[:120]}")
        return self._fire(r['id'], f'{dev}|{key}', now, src=dev, dst=None, detail=detail, related=[])

    def record_route_event(self, dev, prefix, kind, now=None):
        """経路の追加/削除/変更(route_watch)から経路フラップを検知する"""
        from engine.flap_rules import ROUTE_FLAP as r
        now = time.time() if now is None else now
        with self._lock:
            n = self._flap_count(r['id'], dev, prefix, kind, now, r['window'])
            if n is None or n < r['threshold']:
                return None
            return self._fire(r['id'], f'{dev}|{prefix}', now, src=dev, dst=None,
                              detail=f"{prefix}: {r['window']}秒に {n} 回 追加/削除/変更", related=[])

    def record_conn(self, dev, src_ip, port, now=None):
        """装置の実リスナー(SSH/Telnet)が受け付けた TCP 接続を数え、スキャンを検知する。
        アラートは装置(dev)の場所に出す(src=装置、dst=接続元IP)。"""
        now = time.time() if now is None else now
        r = RULES['conn_scan']
        with self._lock:
            dq = self._conns.setdefault((dev, src_ip), deque())
            dq.append((now, port))
            while dq and now - dq[0][0] > r['window']:
                dq.popleft()
            if len(dq) >= r['threshold']:
                ports = sorted({p for _, p in dq})
                return self._fire('conn_scan', f'{dev}|{src_ip}', now, src=dev, dst=src_ip,
                                  detail=f"{src_ip} から {r['window']}秒間に {len(dq)} 回接続 (port {','.join(map(str, ports))})",
                                  related=[])
        return None

    def external_alert(self, rule, key, src, dst, detail, now=None):
        """エンジン外の検知(FWのshun等)をアラートとして記録"""
        now = time.time() if now is None else now
        with self._lock:
            return self._fire(rule, key, now, src=src, dst=dst, detail=detail, related=[])

    def logs_since(self, start):
        with self._lock:
            return [x for x in self.logs if x['t'] >= start]

    def _expire(self, now):
        cutoff = now - RETENTION_SEC
        for dq in (self.events, self.link_events, self.alerts, self.logs):
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
             'source': RULE_SOURCE.get(rule, 'IDS'),
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
            self.events.clear(); self.link_events.clear(); self.alerts.clear(); self.logs.clear()
            self._by_src.clear(); self._by_pair.clear(); self._last_fired.clear(); self._conns.clear(); self._flap.clear(); self._flap_last.clear()


recorder = FlowRecorder()
