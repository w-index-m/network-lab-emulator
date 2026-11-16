"""
回線チェック(フレッツ網)— /cyber-view の WAN

宅内・拠点のルータ(CPE)から見た、フレッツ網を通るインターネットへの出口の状態をまとめる。

- 経路ごとの遅延・パケットロスの推移(5秒ごと、15分ぶん)
  - ラボ版: 擬似フレッツの BAS の `pppoe congestion <0-100>`(夜間の PPPoE 混雑の再現)に応じて PPPoE の経路だけ
    遅延とロスが増える。IPoE(v6プラス / MAP-E)の経路は PPPoE の網終端装置を通らないので影響を受けない
    (実際のフレッツで IPoE に切り替えると夜間に速くなる理由そのもの)。ラボの値は「再現」で、測定ではない
  - スタンドアロン版: 設定した宛先へ本物の ping を打って測る(`cvs/wan_probe.py`)
- 遅延が続けて高い・ロスがある → 注(警告)。しきい値は NETLAB_WAN_RTT_MS(既定 30ms)/ NETLAB_WAN_LOSS(既定 0.05)

経路の推移とその判定は、ラボ版・スタンドアロン版で共通のこのファイルが持つ。
"""

import os
import random
import threading
import time
from collections import deque

RTT_HIGH = float(os.environ.get('NETLAB_WAN_RTT_MS', '30'))
LOSS_HIGH = float(os.environ.get('NETLAB_WAN_LOSS', '0.05'))
HOLD = 3          # 3回続いたら注


class WanSeries:
    """経路ごとの (時刻, 遅延ms, ロス率) の推移と、遅延・ロスの注"""

    def __init__(self, fire=None, maxlen=180):
        self._lock = threading.Lock()
        self.paths = {}            # key -> {'key','label','kind','cpe','peer','series':deque, 'bad':int, 'warned':bool}
        self.fire = fire
        self.maxlen = maxlen

    def add(self, key, sample_rtt, loss, now=None, **meta):
        now = time.time() if now is None else now
        with self._lock:
            p = self.paths.setdefault(key, {'key': key, 'series': deque(maxlen=self.maxlen), 'bad': 0, 'warned': False})
            p.update(meta)
            p['series'].append((now, sample_rtt, loss))
            bad = (sample_rtt is None) or sample_rtt >= RTT_HIGH or loss >= LOSS_HIGH
            p['bad'] = p['bad'] + 1 if bad else 0
            warn = p['bad'] >= HOLD and not p['warned']
            if warn:
                p['warned'] = True
            elif not bad:
                p['warned'] = False
            label = p.get('label', key)
        if warn and self.fire:
            if sample_rtt is None or loss >= LOSS_HIGH:
                self.fire('wan_loss', key, f'{label}: パケットロス {loss * 100:.0f}%({HOLD}回続けて {LOSS_HIGH * 100:.0f}% 以上)', now)
            else:
                self.fire('wan_slow', key, f'{label}: 遅延 {sample_rtt:.1f}ms({HOLD}回続けて {RTT_HIGH:.0f}ms 以上)', now)

    def forget(self, keep):
        with self._lock:
            for k in list(self.paths):
                if k not in keep:
                    self.paths.pop(k, None)

    def snapshot(self):
        with self._lock:
            out = []
            for p in self.paths.values():
                ser = list(p['series'])
                last = ser[-1] if ser else (0, None, 0)
                rtts = [x[1] for x in ser[-12:] if x[1] is not None]
                out.append({**{k: v for k, v in p.items() if k not in ('series',)},
                            'rtt_ms': last[1], 'loss': last[2], 'rtt_avg_1m': round(sum(rtts) / len(rtts), 2) if rtts else None,
                            'series': [[round(t, 1), None if r is None else round(r, 2), round(lo, 3)] for t, r, lo in ser]})
        return sorted(out, key=lambda x: x['key'])


def simulated_rtt(kind, congestion, rnd=random.random):
    """ラボの再現値。PPPoE は網終端装置の混雑(0〜100)で遅延とロスが増える。IPoE は影響なし"""
    base = 2.0 + rnd() * 0.8 if kind == 'pppoe' else 1.4 + rnd() * 0.5
    if kind != 'pppoe' or congestion <= 0:
        return round(base, 2), 0.0
    rtt = base + congestion * 0.6 * (0.6 + 0.8 * rnd())
    loss = max(0.0, (congestion - 60) / 200) * (0.5 + rnd())
    return round(rtt, 2), round(min(loss, 0.9), 3)


series = WanSeries()
