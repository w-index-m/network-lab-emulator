"""
ログ・アラートの記録と検知(スタンドアロン版)

ラボ版(engine/traffic_analytics.py)のうち、実機の監視データだけで成り立つ部分:
- syslog の重要度推定と重複除去(Loki を毎回少し重ねて読むため)
- error 以上のログ → syslog_error
- フラップ検知(ルールは engine/flap_rules.py と同じものを使う)
- リンク断(どれかの検知経路で down になった瞬間)→ link_down
- Zabbix の問題 → zabbix_problem
ping の経路・ポートスキャン検知などラボ固有のものは無い。
"""
import re
import threading
import time
from collections import deque

from .flap_rules import FLAP_RULES, ROUTE_FLAP, match as flap_match

RULES = {
    'link_down': {'name': 'リンク断', 'severity': 'medium', 'cooldown': 0, 'source': 'NET'},
    'syslog_error': {'name': '装置が error 以上のログを出力', 'severity': 'medium', 'cooldown': 60, 'source': 'SYSLOG'},
    'zabbix_problem': {'name': 'Zabbix の問題', 'severity': 'medium', 'cooldown': 0, 'source': 'NET'},
    'valhalla': {'name': 'ヴァルハラ(Prometheus)の警報', 'severity': 'medium', 'cooldown': 0, 'source': 'VALHALLA'},
    'nirvana_rule': {'name': 'ニルヴァーナのアラートルール', 'severity': 'medium', 'cooldown': 0, 'source': 'RULE'},
    'cpu_high': {'name': 'CPU 高負荷', 'severity': 'warning', 'cooldown': 300, 'source': 'NET'},
}
for _r in FLAP_RULES + [ROUTE_FLAP]:
    RULES.setdefault(_r['id'], {'name': _r['name'], 'severity': 'warning', 'window': _r['window'],
                                'threshold': _r['threshold'], 'cooldown': max(60, _r['window']), 'source': 'SYSLOG'})
FLAP_RULE_IDS = {r['id'] for r in FLAP_RULES + [ROUTE_FLAP]}
KNOWN_VENDOR_TYPES = {v for r in FLAP_RULES for v in r['vendors']}
EVENT_RULES = {'stp_tc_flap', 'stp_root_flap', 'mac_flap', 'errdisable_flap', 'route_flap'}

RECOVERY = re.compile(r'state to up|to FULL|Established|Link ?Up|復旧', re.I)
_CISCO_SEV = re.compile(r'%[\w-]+?-([0-7])-\w+')
_TS_PREFIX = re.compile(r'^\*?[A-Z][a-z]{2} +\d+ [\d:.]+: ')
_LEVEL = {'EMERG': 0, 'ALERT': 1, 'CRIT': 2, 'ERR': 3, 'ERROR': 3, 'WARNING': 4, 'WARN': 4,
          'NOTICE': 5, 'INFO': 6, 'DEBUG': 7}


def guess_severity(msg, level=None):
    """%FAC-<sev>-MNEMONIC(Cisco 系)/ syslog の level ラベル / 文面 から重要度を推定"""
    m = _CISCO_SEV.search(msg or '')
    if m:
        return int(m.group(1))
    if level is not None:
        lv = str(level).upper()
        if lv.isdigit():
            return int(lv)
        if lv in _LEVEL:
            return _LEVEL[lv]
    low = (msg or '').lower()
    if any(w in low for w in ('error', 'fail', 'denied', 'mismatch', 'invalid', 'エラー', '失敗')):
        return 3
    if any(w in low for w in (' down', 'dead', 'lost', 'timeout', 'expired', '切断', 'ダウン')):
        return 4
    return 6


class Analytics:
    def __init__(self, max_logs=5000, max_alerts=2000):
        self._lock = threading.Lock()
        self.logs = deque(maxlen=max_logs)
        self.alerts = deque(maxlen=max_alerts)
        self.alert_seq = 0
        self._seen = deque(maxlen=max_logs * 2)
        self._seen_set = set()
        self._last_fired = {}
        self._flap = {}
        self._flap_last = {}
        self.device_type_of = lambda dev: None

    # ── アラート ──
    def fire(self, rule, key, src, dst, detail, now=None, severity=None, name=None):
        now = time.time() if now is None else now
        r = RULES[rule]
        last = self._last_fired.get((rule, key))
        if last is not None and r['cooldown'] and now - last < r['cooldown']:
            return None
        self._last_fired[(rule, key)] = now
        self.alert_seq += 1
        a = {'seq': self.alert_seq, 't': now, 'rule': rule, 'name': name or r['name'], 'source': r['source'],
             'severity': severity or r['severity'], 'src': src, 'dst': dst, 'detail': detail, 'related': []}
        self.alerts.append(a)
        return a

    def alerts_since(self, seq):
        with self._lock:
            return [a for a in self.alerts if a['seq'] > seq]

    def alerts_between(self, start, end):
        with self._lock:
            return [a for a in self.alerts if start <= a['t'] <= end]

    # ── ログ ──
    def record_log(self, dev, host, msg, t, level=None):
        msg = _TS_PREFIX.sub('', (msg or '').strip())
        if not msg:
            return None
        k = (round(t, 3), dev, msg)
        with self._lock:
            if k in self._seen_set:
                return None
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(k)
            self._seen_set.add(k)
            ev = {'t': t, 'dev': dev, 'host': host, 'sev': guess_severity(msg, level), 'msg': msg}
            self.logs.append(ev)
            if ev['sev'] <= 3 and not RECOVERY.search(msg):
                self.fire('syslog_error', dev, dev, None, msg[:160], now=t)
            self._detect_flap(dev, msg, t)
            return ev

    def logs_between(self, start, end):
        with self._lock:
            return [x for x in self.logs if start <= x['t'] <= end]

    # ── フラップ(ラボ版と同じ考え方: 最初の状態は基準、状態変化だけ数える) ──
    def _count(self, rule_id, dev, key, state, now, window):
        k = (rule_id, dev, key)
        if rule_id not in EVENT_RULES:
            last = self._flap_last.get(k)
            self._flap_last[k] = state
            if last is None or last == state:
                return None
        dq = self._flap.setdefault(k, deque())
        dq.append(now)
        while dq and window and now - dq[0] > window:
            dq.popleft()
        return len(dq)

    def _detect_flap(self, dev, msg, now):
        dt = self.device_type_of(dev)
        hit = flap_match(dt if dt in KNOWN_VENDOR_TYPES else None, msg)
        if not hit:
            return None
        r, key, state = hit
        n = self._count(r['id'], dev, key, state, now, r['window'])
        if n is None or n < r['threshold']:
            return None
        detail = (f"{key}: {r['window']}秒に {n} 回 状態変化" if r['threshold'] > 1
                  else f"{key}: {msg[msg.find('%'):][:120] if '%' in msg else msg[:120]}")
        return self.fire(r['id'], f'{dev}|{key}', dev, None, detail, now=now)
