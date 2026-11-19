"""
監視データを集めて、画面(cyber_view.html)が使う形にまとめる

- 流量: Prometheus(snmp_exporter の ifHCIn/OutOctets)→ 無ければ Zabbix(Bits received/sent)
  → 無ければ SNMP 直接(sources.snmp.traffic: 実機の ifHCIn/OutOctets の差)→ MRTG / Cacti
- リンク断の検知経路: SYSLOG(Loki のログ)/ TRAP(この画面が受ける SNMP trap)/
  POLL(Prometheus の ifOperStatus、Zabbix の Operational status)。STATE はラボ専用なので無し
- 応答なし: Prometheus の up=0、Zabbix のインタフェース到達性=応答なし
- アラート: link_down / syslog_error / フラップ各種 / Zabbix の問題
"""
import threading
import time
from collections import defaultdict, deque

from . import sources as S
from .analytics import FLAP_RULE_IDS, RECOVERY, Analytics
from .link_evidence import norm_if, parse_log_link_event, traps
from . import valhalla as V
from .valhalla_views import AlertImporter

# 画面に出す呼び名(メトリクス収集基盤 Prometheus は Cyber View では「ヴァルハラ」と呼ぶ)
DISPLAY_NAME = {'prometheus': 'ヴァルハラ'}

EVIDENCE_ORDER = ('STATE', 'SYSLOG', 'TRAP', 'POLL')
UTIL_WARN, UTIL_CRIT = 0.5, 0.8
TRAP_STALE_AFTER = 90.0


def classify(util, down):
    if down:
        return 'down'
    return 'crit' if util >= UTIL_CRIT else 'warn' if util >= UTIL_WARN else 'ok'


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        src = cfg.get('sources') or {}
        self.prom = S.Prometheus(**src['prometheus']) if src.get('prometheus', {}).get('url') else None
        self.loki = S.Loki(**src['loki']) if src.get('loki', {}).get('url') else None
        self.zbx = S.Zabbix(**src['zabbix']) if src.get('zabbix', {}).get('url') else None
        self.loki_labels = (src.get('loki') or {}).get('device_labels') or ['device_id', 'host', 'hostname', 'instance']
        # linkDown の trap から、これだけ経っても POLL が up を返し続けるなら、linkUp の trap を取りこぼしたとみなす(秒)
        self.trap_stale_after = float((src.get('snmp_trap') or {}).get('stale_after', TRAP_STALE_AFTER))
        self.devs = {d['id']: d for d in cfg['devices']}
        self.analytics = Analytics()
        self.analytics.device_type_of = lambda dev: (self.devs.get(dev) or {}).get('type')
        self._lookup = {}
        for d in cfg['devices']:
            keys = {d['id'], d['hostname'], d.get('mgmt_ip'), d.get('zabbix_host'), d.get('loki_host')}
            keys |= {i.get('ip') for i in d['interfaces']}
            for k in keys:
                if k:
                    self._lookup.setdefault(str(k).lower(), d['id'])
        self.errors = {}
        self._cache = {}
        self._lock = threading.Lock()
        self._log_until = time.time() - 300
        self._zbx_seen = set()
        self._zbx_hosts = {}
        self._prev_down = {}
        self.hist = defaultdict(lambda: deque(maxlen=400))
        self.valhalla_alerts = AlertImporter()
        from .legacy_sources import LegacyRates
        self.legacy = LegacyRates(cfg, self.devs, lambda k: self.dev_of(k))   # MRTG・Cacti
        from .snmp_poll import SnmpPoller
        self.snmp = SnmpPoller(cfg, self.analytics, self.errors)   # 経路表・CPU を実機から直接(sources.snmp)
        from .security import SiteSecurity
        self.sec = SiteSecurity(cfg, self)        # ATT&CK・ネクサス・ダークネット・フロー・外部センサー・回線チェック
        if self.prom and not self.prom.use_proxy:
            V.OPENER = S._opener(False)     # 構内の監視基盤へは直接(sources と同じ扱い)
        self._stop = threading.Event()

    # ── 装置の特定 ──
    def dev_of(self, *keys):
        for k in keys:
            if k and str(k).lower() in self._lookup:
                return self._lookup[str(k).lower()]
            if k and ':' in str(k) and str(k).split(':')[0].lower() in self._lookup:
                return self._lookup[str(k).split(':')[0].lower()]
        return None

    def _dev_in_msg(self, msg):
        for d in self.devs.values():
            if d['hostname'] and d['hostname'] in msg:
                return d['id']
        return None

    # ── 取り込み ──
    def poll_logs(self, now=None):
        if not self.loki:
            return 0
        now = time.time() if now is None else now
        start = self._log_until - 5          # 少し重ねて読む(重複は Analytics が除く)
        try:
            rows = self.loki.logs(start, now)
            self.errors.pop('loki', None)
        except Exception as e:  # noqa: BLE001 - 取れなくても他は動かす
            self.errors['loki'] = str(e)[:200]
            return 0
        n = 0
        for r in rows:
            lab = r['labels']
            dev = self.dev_of(*[lab.get(k) for k in self.loki_labels]) or self._dev_in_msg(r['msg'])
            if not dev:
                continue
            if self.analytics.record_log(dev, self.devs[dev]['hostname'], r['msg'], r['t'],
                                         lab.get('level') or lab.get('severity')):
                n += 1
                self.sec.on_log(dev, r['msg'], r['t'])
        self._log_until = now
        return n

    def poll_zabbix_problems(self):
        if not self.zbx:
            return
        try:
            hosts = self._zabbix_hosts()
            by_id = {v['hostid']: k for k, v in hosts.items()}
            for p in self.zbx.problems(list(by_id)):
                sev = S.ZBX_SEVERITY.get(p['severity'])
                if not sev or p['eventid'] in self._zbx_seen:
                    continue
                self._zbx_seen.add(p['eventid'])
                dev = self.dev_of(by_id.get(p['hostid']))
                self.analytics.fire('zabbix_problem', p['eventid'], dev, None, p['name'], now=p['clock'],
                                    severity=sev, name=f"Zabbix: {p['name'][:40]}")
            self.errors.pop('zabbix', None)
        except Exception as e:  # noqa: BLE001
            self.errors['zabbix'] = str(e)[:200]

    def _zabbix_hosts(self):
        names = [d.get('zabbix_host') or d['hostname'] for d in self.devs.values()]
        self._zbx_hosts = self.zbx.hosts(names)
        return self._zbx_hosts

    # ── 1回分の状態(短時間キャッシュ) ──
    def snapshot(self, window=60):
        window = max(10, min(int(window), 3600))
        with self._lock:
            c = self._cache.get(window)
            if c and time.time() - c[0] < 3:
                return c[1]
        snap = self._build(window)
        with self._lock:
            self._cache[window] = (time.time(), snap)
        return snap

    def _build(self, window):
        now = time.time()
        rates, oper, up, counters, zitems = {}, {}, {}, {}, {}
        if self.prom:
            try:
                rates = self.prom.rates(window)
                oper = self.prom.oper_status()
                up = self.prom.targets_up()
                counters = self.prom.counters()
                self.errors.pop('prometheus', None)
            except Exception as e:  # noqa: BLE001
                self.errors['prometheus'] = str(e)[:200]
        zavail = {}
        if self.zbx:
            try:
                hosts = self._zabbix_hosts()
                zi = self.zbx.interface_items([h['hostid'] for h in hosts.values()])
                for hname, h in hosts.items():
                    dev = self.dev_of(hname)
                    if dev:
                        zitems[dev] = zi.get(h['hostid'], {})
                        zavail[dev] = h['available']
                self.errors.pop('zabbix', None)
            except Exception as e:  # noqa: BLE001
                self.errors['zabbix'] = str(e)[:200]

        legacy = {}
        if self.legacy.enabled():
            legacy = self.legacy.rates(now)
            for k in ('mrtg', 'cacti'):
                if k in self.legacy.errors:
                    self.errors[k] = self.legacy.errors[k]
                else:
                    self.errors.pop(k, None)

        sn = self.snmp.traffic_snapshot(window) if self.snmp.traffic_on else {'rates': {}, 'oper': {}, 'counters': {}, 'lost': {}}

        def end_rate(dev, ifn):
            """(in_bps, out_bps, 取得元)。Prometheus → Zabbix → SNMP 直接 → MRTG / Cacti の順"""
            k = (self.devs[dev].get('mgmt_ip'), norm_if(ifn))
            if k in rates and (rates[k][0] is not None or rates[k][1] is not None):
                return rates[k][0], rates[k][1], 'prometheus'
            z = zitems.get(dev, {}).get(norm_if(ifn))
            if z and ('in_bps' in z or 'out_bps' in z):
                return z.get('in_bps'), z.get('out_bps'), 'zabbix'
            sr = sn['rates'].get((dev, norm_if(ifn)))
            if sr and (sr[0] is not None or sr[1] is not None):
                return sr[0], sr[1], 'snmp'
            lg = legacy.get((dev, norm_if(ifn)))
            if lg:
                return lg[0], lg[1], lg[2]
            return None, None, None

        # 検知経路
        seen = {}

        def note(src, dev, ifn, t, st):
            if dev and ifn:
                k = (src, dev, norm_if(ifn))
                if k not in seen or t >= seen[k][0]:
                    seen[k] = (t, st)
        for r in self.analytics.logs_between(now - 3600, now):
            ev = parse_log_link_event(r['msg'])
            if ev:
                note('SYSLOG', r['dev'], ev[0], r['t'], ev[1])
        for e in traps.snapshot():
            note('TRAP', self.dev_of(e['host'], e['src_ip']), e['iface'], e['t'], e['state'])
        for (inst, ifn), st in oper.items():
            note('POLL', self.dev_of(inst), ifn, now, st)
        for dev, ifs in zitems.items():
            for ifn, z in ifs.items():
                if z.get('oper'):
                    note('POLL', dev, ifn, now, z['oper'])
        for (dev, ifn), st in sn['oper'].items():
            note('POLL', dev, ifn, now, st)
        # trap は UDP なので linkUp を取りこぼすことがある。それで「down」が残り続けないよう、
        # linkDown から trap_stale_after 秒以上たっても POLL(いまの状態の読み取り)が up なら、その trap の down は捨てる。
        # 新しい linkDown(POLL の更新が追いついていない間)は、そのまま down とする。POLL が無いときも捨てない
        for (src, dev, ifn), (t, st) in list(seen.items()):
            if src == 'TRAP' and st == 'down':
                poll = seen.get(('POLL', dev, ifn))
                if poll and poll[1] == 'up' and now - t >= self.trap_stale_after:
                    del seen[(src, dev, ifn)]
        lost = dict(sn['lost'])
        for inst, ok in up.items():
            d = self.dev_of(inst)
            if d and not ok:
                lost[d] = inst
        for d, av in zavail.items():
            if av is False:
                lost.setdefault(d, self.devs[d].get('mgmt_ip') or self.devs[d].get('zabbix_host') or d)

        links = []
        for ln in self.cfg['links']:
            a, b, ia, ib = ln['a'], ln['b'], ln['a_if'], ln['b_if']
            a_in, a_out, sa = end_rate(a, ia)
            b_in, b_out, sb = end_rate(b, ib)
            ab = a_out if a_out is not None else (b_in or 0.0)
            ba = b_out if b_out is not None else (a_in or 0.0)
            used = {s for s in (sa, sb) if s}
            ev = {k: None for k in EVIDENCE_ORDER}
            for src in ('SYSLOG', 'TRAP', 'POLL'):
                states = [seen[(src, d, norm_if(i))][1] for d, i in ((a, ia), (b, ib)) if (src, d, norm_if(i)) in seen]
                if states:
                    ev[src] = 'down' if 'down' in states else 'up'
            lost_ends = [d for d in (a, b) if d in lost]
            if lost_ends and ev['POLL'] != 'down':
                ev['POLL'] = 'lost'
            down_sources = [k for k in EVIDENCE_ORDER if ev[k] == 'down']
            down = bool(down_sources)
            speed = float(ln.get('speed') or self.cfg['link_speed_bps'])
            util = max(ab, ba) / speed if speed else 0.0
            key = (a, b)
            al = [x for x in self.analytics.alerts_between(now - window, now) if x['src'] in (a, b) or x['dst'] in (a, b)]
            links.append({
                'a': a, 'b': b, 'iface_a': ia, 'iface_b': ib, 'down': down, 'evidence': ev,
                'down_sources': down_sources, 'lost': [self.devs[d]['hostname'] for d in lost_ends],
                'host_a': self.devs[a]['hostname'], 'host_b': self.devs[b]['hostname'],
                'type_a': self.devs[a]['type'], 'type_b': self.devs[b]['type'],
                'ab_bps': round(ab), 'ba_bps': round(ba), 'util': util,
                'util_ab': ab / speed if speed else 0.0, 'util_ba': ba / speed if speed else 0.0,
                'flows_ok': 0, 'flows_ng': 0,
                'alerts': [{'rule': x['rule'], 'name': x['name'], 'severity': x['severity'], 't': x['t']} for x in al[-5:]],
                'state': 'lost' if lost_ends and not down else classify(util, down),
                'source': '+'.join(sorted(used)) if used else 'none',
                'series': self._series(key),
            })
        unreachable = [{'id': d, 'hostname': self.devs[d]['hostname'], 'instance': inst} for d, inst in lost.items()]
        return {'now': now, 'window': window, 'links': links, 'unreachable': unreachable,
                'oper': oper, 'zitems': zitems, 'counters': counters,
                'snmp_oper': sn['oper'], 'snmp_counters': sn['counters'],
                'sources_used': sorted({x for ln in links for x in ln['source'].split('+') if x != 'none'})}

    def _series(self, key, step=60):
        h = list(self.hist.get(key, ()))
        out, last = [], None
        for t, v in h:
            if last is None or t - last >= step:
                out.append(round(v))
                last = t
        return out[-60:]

    # ── 定期処理(ログ取り込み・アラート判定・履歴) ──
    def poll_valhalla_alerts(self):
        """⑤ ヴァルハラで発報中の警報を取り込む"""
        if not self.prom:
            return
        try:
            self.valhalla_alerts.run(V.firing_alerts(self.prom.url), self.analytics, self.dev_of)
            self.errors.pop('valhalla_alerts', None)
        except Exception as e:  # noqa: BLE001
            self.errors['valhalla_alerts'] = str(e)[:200]

    def tick(self):
        self.poll_logs()
        self.poll_zabbix_problems()
        self.poll_valhalla_alerts()
        snap = self.snapshot(60)
        now = snap['now']
        for ln in snap['links']:
            key = (ln['a'], ln['b'])
            self.hist[key].append((now, ln['ab_bps'] + ln['ba_bps']))
            was = self._prev_down.get(key)
            if ln['down'] and not was:
                self.analytics.fire('link_down', f"{ln['a']}|{ln['iface_a']}", ln['a'], ln['b'],
                                    f"{ln['host_a']} {ln['iface_a']} ⇄ {ln['host_b']} {ln['iface_b']} が down "
                                    f"(検知: {' / '.join(ln['down_sources'])})", now=now)
            self._prev_down[key] = ln['down']
        return snap

    def start(self):
        spec = (self.cfg.get('sources') or {}).get('snmp_trap', {}).get('listen')
        if spec:
            try:
                traps.start(spec)
            except OSError as e:
                self.errors['snmp_trap'] = str(e)

        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.errors['tick'] = str(e)[:200]
                self._stop.wait(float(self.cfg['poll_interval']))
        threading.Thread(target=loop, daemon=True, name='cvs-collector').start()
        self.snmp.start()
        self.sec.start()

    def stop(self):
        self._stop.set()
        self.snmp.stop()
        self.sec.stop()

    # ── 画面向けのまとめ ──
    def hosts(self):
        snap = self.snapshot(60)
        peers = defaultdict(set)
        for ln in self.cfg['links']:
            peers[ln['a']].add(ln['b'])
            peers[ln['b']].add(ln['a'])
        out = []
        for d in self.devs.values():
            ifs = []
            for i in d['interfaces']:
                if not i.get('ip'):
                    continue
                k = (d.get('mgmt_ip'), norm_if(i['name']))
                sk = (d['id'], norm_if(i['name']))
                st = (snap['oper'].get(k) or (snap['zitems'].get(d['id'], {}).get(norm_if(i['name'])) or {}).get('oper')
                      or snap['snmp_oper'].get(sk))
                cin, cout = snap['counters'].get(k) or snap['snmp_counters'].get(sk, (0, 0))
                ifs.append({'name': i['name'], 'ip': i['ip'], 'prefix': int(i['prefix']), 'desc': i.get('desc', ''),
                            'up': st != 'down', 'in_bytes': cin, 'out_bytes': cout})
            if ifs:
                out.append({'id': d['id'], 'hostname': d['hostname'], 'type': d['type'], 'interfaces': ifs,
                            'peers': sorted(peers[d['id']])})
        return out

    def health(self, window=300):
        now = time.time()
        snap = self.snapshot(60)
        rank = {'ok': 0, 'warn': 1, 'crit': 2}
        cats = []
        H = lambda d: self.devs.get(d, {}).get('hostname', d)  # noqa: E731
        links, unreachable = snap['links'], snap['unreachable']
        al = self.analytics.alerts_between(now - window, now)
        zp = [a for a in al if a['rule'] == 'zabbix_problem']
        down = [f"{x['host_a']} ⇄ {x['host_b']}" for x in links if x['down']]
        st = 'crit' if down or unreachable or any(a['severity'] == 'high' for a in zp) else 'warn' if zp else 'ok'
        summ = []
        if down:
            summ.append(f'リンク断 {len(down)}本')
        if unreachable:
            summ.append(f'応答なし {len(unreachable)}台')
        if zp:
            summ.append(f'Zabbix の問題 {len(zp)}件')
        cats.append({'key': 'network', 'label': 'ネットワーク', 'state': st,
                     'summary': ' ・ '.join(summ) if summ else f'リンク {len(links)}本 すべて up',
                     'unreachable': unreachable,
                     'details': ([f"応答なし: {u['hostname']} ({u['instance']})" for u in unreachable]
                                 + [f'断: {x}' for x in down][:5]
                                 + [f"検知: {x['host_a']} ⇄ {x['host_b']} = {' / '.join(x['down_sources'])}"
                                    for x in links if x['down']][:3]
                                 + [f"Zabbix: {H(a['src'])} {a['detail'][:60]}" for a in zp[-3:]])[:6]})
        peak = max(links, key=lambda x: x['util'], default=None)
        pu = peak['util'] if peak else 0.0
        st = 'crit' if pu >= UTIL_CRIT else 'warn' if pu >= UTIL_WARN else 'ok'
        cats.append({'key': 'traffic', 'label': 'トラフィック', 'state': st,
                     'summary': f'最大使用率 {pu * 100:.1f}%' + (f" ・ 取得元 {'+'.join(DISPLAY_NAME.get(x, x) for x in snap['sources_used'])}"
                                                            if snap['sources_used'] else ' ・ 取得元なし'),
                     'details': [f"最大使用率 {pu * 100:.1f}% ({peak['host_a']} ⇄ {peak['host_b']})"] if peak else []})
        sec = [a for a in al if a['source'] in ('IDS', 'FW')]
        cats.append({'key': 'security', 'label': 'セキュリティ',
                     'state': 'crit' if any(a['severity'] == 'high' for a in sec) else 'warn' if sec else 'ok',
                     'summary': f'アラート {len(sec)}件' if sec else 'アラートなし', 'details': []})
        logs = self.analytics.logs_between(now - window, now)
        bad = [x for x in logs if not RECOVERY.search(x['msg'])]
        err = [x for x in bad if x['sev'] <= 3]
        warn = [x for x in bad if x['sev'] == 4]
        flaps = [a for a in al if a['rule'] in FLAP_RULE_IDS]
        st = 'crit' if err else 'warn' if (warn or flaps) else 'ok'
        log_src = 'loki' if self.loki else 'none'
        cats.append({'key': 'syslog', 'label': 'syslog', 'state': st, 'source': log_src,
                     'source_error': self.errors.get('loki'),
                     'summary': (f'error以上 {len(err)} ・ warning {len(warn)} ・ 全 {len(logs)}件'
                                 + (f' ・ フラップ {len(flaps)}件' if flaps else '')),
                     'details': ([f"フラップ {H(a['src'])}: {a['name']} ({a['detail'][:60]})" for a in flaps[-3:]]
                                 + [f"{x['host']}: {x['msg'][:90]}" for x in (err + warn)[-4:]])[:5],
                     'flaps': len(flaps)})
        overall = max((c['state'] for c in cats), key=lambda v: rank[v])
        metrics = '+'.join(snap['sources_used']) or ('prometheus' if self.prom else 'zabbix' if self.zbx else 'none')
        return {'overall': overall, 'categories': cats, 'window': window, 'now': now,
                'sources': {'metrics': metrics, 'logs': log_src}, 'errors': dict(self.errors)}

    def logs(self, since=0, limit=30):
        now = time.time()
        start = since if since > 0 else now - 300
        rows = [r for r in self.analytics.logs_between(start, now) if r['t'] > since]
        return [{'t': r['t'], 'dev': r['dev'], 'host': r['host'], 'sev': r['sev'],
                 'recovery': bool(RECOVERY.search(r['msg'])), 'msg': r['msg'][:200]}
                for r in rows][-max(1, min(int(limit), 200)):]
