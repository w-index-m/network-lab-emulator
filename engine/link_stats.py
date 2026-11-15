"""
ルータ間リンクのトラフィック(bps)と状態 — /cyber-view の LINK VIEW 用

2つのデータソース:
- internal: エミュレータ自身のIFカウンタ(dp_engine)を一定間隔でサンプリングし、
  window 秒前のサンプルとの差分から bps を出す。SNMP の ifHCIn/OutOctets と
  同じ値なので、Prometheus で `rate(ifHCOutOctets[1m])*8` を取るのと同じ意味になる。
- prometheus: 環境変数 PROMETHEUS_URL (例 http://127.0.0.1:9090) があれば、
  iac/cisco-traffic-monitoring の snmp_exporter が集めた
  `rate(ifHCOutOctets{job="cisco-snmp"}[1m])*8` を問い合わせ、
  instance(管理IP)+ifName(短縮名)でエミュレータのリンクに対応づける。
  取れなかったリンク・Prometheus 不達時は internal で補い、どちらを使ったかを返す。
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque

SAMPLE_INTERVAL = 10          # 秒
HISTORY_SEC = 3600            # 1時間分の履歴(スパークライン用)
LINK_SPEED_BPS = 1_000_000_000  # ifHighSpeed=1000 (Mbps) と同じ
UTIL_WARN, UTIL_CRIT = 0.5, 0.8


class LinkSampler:
    def __init__(self):
        self._lock = threading.Lock()
        self.hist = defaultdict(lambda: deque(maxlen=HISTORY_SEC // SAMPLE_INTERVAL + 2))

    def sample(self, links, now=None):
        """links: [{'a','b','ab_bytes','ba_bytes'}] (a<b に正規化済み)"""
        now = time.time() if now is None else now
        with self._lock:
            for ln in links:
                self.hist[(ln['a'], ln['b'])].append((now, ln['ab_bytes'], ln['ba_bytes']))

    def rate(self, key, window, now=None):
        """window 秒前(に最も近い古いサンプル)から最新までの bps (ab, ba)"""
        with self._lock:
            h = list(self.hist.get(key, ()))
        if len(h) < 2:
            return None
        now = h[-1][0] if now is None else now
        old = h[0]
        for s in h:
            if s[0] <= now - window:
                old = s
        new = h[-1]
        dt = new[0] - old[0]
        if dt <= 0:
            return None
        # カウンタが巻き戻った(装置の作り直し等)場合は0扱い
        return (max(0, new[1] - old[1]) * 8 / dt, max(0, new[2] - old[2]) * 8 / dt)

    def series(self, key, window, step=60):
        """スパークライン用: step 秒ごとの bps (ab+ba) を古い順に最大 window/step 個"""
        with self._lock:
            h = list(self.hist.get(key, ()))
        out = []
        if len(h) < 2:
            return out
        end = h[-1][0]
        t = end - window
        prev = None
        for s in h:
            if s[0] < t:
                prev = s
                continue
            if prev is None:
                prev = s
                continue
            if s[0] - prev[0] >= step or s is h[-1]:
                dt = s[0] - prev[0]
                if dt > 0:
                    out.append(round((max(0, s[1] - prev[1]) + max(0, s[2] - prev[2])) * 8 / dt))
                prev = s
        return out

    def clear(self):
        with self._lock:
            self.hist.clear()


sampler = LinkSampler()


def prometheus_rates(url, timeout=3.0):
    """{(instance, ifName): out_bps} を返す。失敗時は例外。"""
    q = 'rate(ifHCOutOctets{job="cisco-snmp"}[1m]) * 8'
    full = url.rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': q})
    # 監視スタックはローカル(127.0.0.1)にあるのでプロキシを通さない
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(full, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    if data.get('status') != 'success':
        raise RuntimeError(data.get('error') or 'query failed')
    out = {}
    for row in data['data']['result']:
        m = row.get('metric', {})
        if 'instance' in m and 'ifName' in m:
            out[(m['instance'].split(':')[0], m['ifName'])] = float(row['value'][1])
    return out


def classify(util, down, ng, alerts):
    """リンクの状態: down / crit / warn / ok / idle"""
    if down:
        return 'down'
    if util >= UTIL_CRIT or alerts:
        return 'crit'
    if util >= UTIL_WARN or ng:
        return 'warn'
    return 'ok'
