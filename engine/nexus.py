"""
複数拠点の横断分析(ネクサス型)— NIRVANA改(2023年)の「組織をまたぐ分析」のエッセンス

各拠点のニルヴァーナ(ラボ版・スタンドアロン版)が、警報と ATT&CK のイベントを中央のニルヴァーナへ送る。
中央は集めたものを突き合わせ、単独の拠点では見えない全体像を見つけて、関係する拠点へ返す。

- 共通の攻撃元: 同じ送信元が 24時間以内に 2拠点以上を攻撃している
- 同時期の同じ手口: 同じ ATT&CK の手法が 1時間以内に 2拠点以上で出ている
- 偏り: 1つの拠点に攻撃が集中している(2拠点以上・10件以上のとき、6割以上)

標準ライブラリだけ(ラボ版・スタンドアロン版で同じファイルを使う)。
"""

import json
import threading
import time
import urllib.request
from collections import deque

COMMON_SRC_WINDOW = 24 * 3600
SAME_TECH_WINDOW = 3600
BIAS_MIN, BIAS_SHARE = 10, 0.6


class NexusHub:
    """中央: 拠点からの報告を集めて突き合わせる"""

    def __init__(self, maxlen=20000):
        self._lock = threading.Lock()
        self.sites = {}                          # 名前 -> {last_seen, reports, addr}
        self.alerts = deque(maxlen=maxlen)       # {site, t, rule, src, dst, detail, severity, source}
        self.attack = deque(maxlen=maxlen)       # ATT&CK のイベント(org = 拠点名)

    def ingest(self, site, payload, addr=None, now=None):
        now = time.time() if now is None else now
        site = str(site or '').strip()[:40]
        if not site:
            raise ValueError('site が必要です')
        with self._lock:
            s = self.sites.setdefault(site, {'name': site, 'first_seen': now, 'reports': 0})
            s.update(last_seen=now, addr=addr)
            s['reports'] += 1
            for a in (payload.get('alerts') or [])[:2000]:
                self.alerts.append({'site': site, 't': float(a.get('t', now)), 'rule': str(a.get('rule', '')),
                                    'src': str(a.get('src') or ''), 'dst': str(a.get('dst') or ''),
                                    'detail': str(a.get('detail', ''))[:300], 'severity': a.get('severity', ''),
                                    'source': a.get('source', '')})
            for e in (payload.get('attack') or [])[:2000]:
                self.attack.append({**{k: e.get(k) for k in ('t', 'tactic', 'technique', 'name', 'rule', 'src',
                                                              'target', 'detail')},
                                    'org': site, 'seq': 0})
        return self.findings(now=now, site=site)

    def findings(self, now=None, site=None):
        now = time.time() if now is None else now
        with self._lock:
            al = [a for a in self.alerts if now - a['t'] <= COMMON_SRC_WINDOW]
            ev = [e for e in self.attack if e.get('t') and now - e['t'] <= COMMON_SRC_WINDOW]
        out = []
        # 共通の攻撃元(装置の ID ではなくアドレスのもの)
        by_src = {}
        for a in al:
            if a['src'] and a['src'][0].isdigit():
                by_src.setdefault(a['src'], []).append(a)
        for src, lst in by_src.items():
            sites = sorted({a['site'] for a in lst})
            if len(sites) >= 2:
                out.append({'id': f'common_src:{src}', 'kind': 'common_src', 'src': src, 'sites': sites,
                            'rules': sorted({a['rule'] for a in lst}), 'count': len(lst),
                            'first': min(a['t'] for a in lst), 'last': max(a['t'] for a in lst),
                            'text': f'同じ攻撃元 {src} が {len(sites)}拠点({"・".join(sites)})を攻撃'})
        # 同時期の同じ手口
        by_tech = {}
        for e in ev:
            if now - e['t'] <= SAME_TECH_WINDOW:
                by_tech.setdefault(e['technique'], []).append(e)
        for tech, lst in by_tech.items():
            sites = sorted({e['org'] for e in lst})
            if len(sites) >= 2:
                out.append({'id': f'same_tech:{tech}', 'kind': 'same_technique', 'technique': tech,
                            'name': lst[-1].get('name'), 'tactic': lst[-1].get('tactic'), 'sites': sites,
                            'count': len(lst), 'last': max(e['t'] for e in lst),
                            'text': f'同じ手口 {tech} {lst[-1].get("name") or ""} が1時間以内に {len(sites)}拠点で'})
        # 偏り
        per = {}
        for a in al:
            per[a['site']] = per.get(a['site'], 0) + 1
        total = sum(per.values())
        if len(self.sites) >= 2 and total >= BIAS_MIN:
            top, n = max(per.items(), key=lambda x: x[1])
            if n / total >= BIAS_SHARE:
                out.append({'id': f'bias:{top}', 'kind': 'bias', 'site': top, 'sites': [top], 'share': round(n / total, 2),
                            'count': n, 'text': f'攻撃が {top} に集中(全拠点の {round(n / total * 100)}%)'})
        if site:
            out = [f for f in out if site in f['sites'] or f['kind'] == 'bias']
        return out

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            sites = [{**s, 'age': round(now - s['last_seen'], 1)} for s in self.sites.values()]
            counts = {}
            for a in self.alerts:
                counts[a['site']] = counts.get(a['site'], 0) + 1
        for s in sites:
            s['alerts'] = counts.get(s['name'], 0)
        return {'sites': sorted(sites, key=lambda s: s['name']), 'findings': self.findings(now=now)}

    def attack_events(self):
        with self._lock:
            return list(self.attack)


class NexusReporter:
    """拠点: 新しい警報と ATT&CK のイベントを中央へ送り、返ってきた所見を警報にする"""

    def __init__(self, url, site, token, get_alerts, get_attack, on_finding, interval=10):
        self.url = url.rstrip('/') + '/api/nexus/report'
        self.site, self.token = site, token
        self.get_alerts, self.get_attack, self.on_finding = get_alerts, get_attack, on_finding
        self.interval = interval
        self._alert_seq = 0
        self._attack_seq = 0
        self.seen = set()
        self.status = {'ok': None, 'error': None, 'sent': 0, 'last': None}
        self._stop = threading.Event()
        self._th = None

    def tick(self):
        alerts = [a for a in self.get_alerts() if a.get('seq', 0) > self._alert_seq]
        attack = [e for e in self.get_attack() if e.get('seq', 0) > self._attack_seq]
        body = json.dumps({'site': self.site, 'alerts': [{k: a.get(k) for k in ('t', 'rule', 'src', 'dst', 'detail', 'severity', 'source')}
                                                           for a in alerts],
                           'attack': attack}, ensure_ascii=False, default=list).encode()
        req = urllib.request.Request(self.url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json', 'X-Nexus-Token': self.token or ''})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                res = json.loads(r.read().decode() or '{}')
        except Exception as e:  # noqa: BLE001 - 中央に届かなくても拠点の監視は続ける
            self.status.update(ok=False, error=str(e)[:200])
            return []
        if alerts:
            self._alert_seq = max(a.get('seq', 0) for a in alerts)
        if attack:
            self._attack_seq = max(e.get('seq', 0) for e in attack)
        self.status.update(ok=True, error=None, last=time.time())
        self.status['sent'] += len(alerts) + len(attack)
        new = []
        for f in res.get('findings', []):
            key = (f['id'], f.get('count'))
            if f['id'] in {k[0] for k in self.seen}:
                continue
            self.seen.add(key)
            new.append(f)
            self.on_finding(f)
        return new

    def start(self):
        def loop():
            while not self._stop.wait(self.interval):
                self.tick()
        self._th = threading.Thread(target=loop, name='nexus-reporter', daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()


hub = NexusHub()
