"""
ルーティングテーブルの変化(経路の追加・削除・変更)を記録する — /cyber-view の Info 表示用

装置ごとの最良経路(rib_engine.get_best_routes)を定期的に取り、前回との差分を
イベントにする。OSPF/BGP/RIP で学習した経路、スタティック、直結のどれでも同じ扱い。
実機の IOS は経路の増減を syslog に出さないので(debug ip routing を除く)、
syslog とは別の「情報」イベントとして持つ。

最初に見た装置は基準を取るだけでイベントにしない(起動直後に全経路が「追加」に
見えてしまうのを避ける)。
"""

import threading
import time
from collections import deque

SOURCE_CODE = {'connected': 'C', 'static': 'S', 'ospf': 'O', 'ospf_ia': 'O IA', 'ospf_e1': 'O E1',
               'ospf_e2': 'O E2', 'bgp': 'B', 'rip': 'R', 'eigrp': 'D'}


def _code(src):
    return SOURCE_CODE.get(src or '', (src or '?')[:4].upper())


def _snapshot(routes):
    """{ "net/prefix": (コード, 次ホップ, 出口IF) }"""
    out = {}
    for r in routes:
        key = f"{r['network']}/{r['prefix']}"
        nh = r.get('next_hop') or ''
        if nh in ('0.0.0.0',):
            nh = ''
        out[key] = (_code(r.get('source')), nh, r.get('iface') or '')
    return out


class RouteWatcher:
    def __init__(self, maxlen=2000):
        self._lock = threading.Lock()
        self._prev = {}                    # device_id -> snapshot
        self.events = deque(maxlen=maxlen)
        self.seq = 0
        self.counts = {}                   # (装置, add/del/change) -> 起動からの件数(/metrics 用)

    def observe(self, device_id, hostname, routes, now=None):
        """1台分の現在の最良経路を渡す。新しく出たイベントのリストを返す"""
        now = time.time() if now is None else now
        cur = _snapshot(routes)
        new = []
        with self._lock:
            prev = self._prev.get(device_id)
            self._prev[device_id] = cur
            if prev is None:
                return []
            for key in sorted(set(prev) | set(cur)):
                a, b = prev.get(key), cur.get(key)
                if a == b:
                    continue
                kind = 'add' if a is None else 'del' if b is None else 'change'
                code, nh, iface = b if b else a
                self.seq += 1
                ev = {'seq': self.seq, 't': now, 'dev': device_id, 'host': hostname, 'kind': kind,
                      'prefix': key, 'code': code, 'next_hop': nh, 'iface': iface}
                if kind == 'change':
                    ev['old'] = {'code': a[0], 'next_hop': a[1], 'iface': a[2]}
                self.events.append(ev)
                self.counts[(hostname or device_id, kind)] = self.counts.get((hostname or device_id, kind), 0) + 1
                new.append(ev)
        return new

    def forget(self, keep_ids):
        """もう存在しない装置の基準を捨てる"""
        with self._lock:
            for d in list(self._prev):
                if d not in keep_ids:
                    self._prev.pop(d, None)

    def since(self, seq, limit=100):
        with self._lock:
            out = [e for e in self.events if e['seq'] > seq]
        return out[-limit:]

    def clear(self):
        with self._lock:
            self._prev.clear()
            self.events.clear()


def describe(ev):
    """1行の説明(一覧表示用)"""
    via = f" via {ev['next_hop']}" if ev['next_hop'] else ''
    on = f" [{ev['iface']}]" if ev['iface'] else ''
    if ev['kind'] == 'change':
        o = ev['old']
        return (f"{ev['code']} {ev['prefix']}{via}{on} (変更前: {o['code']}"
                f"{' via ' + o['next_hop'] if o['next_hop'] else ''})")
    return f"{ev['code']} {ev['prefix']}{via}{on}"


watcher = RouteWatcher()
