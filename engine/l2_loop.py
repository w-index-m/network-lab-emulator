"""
L2 ループの検知・嵐(ブロードキャストストーム)・機種ごとの止め方

スイッチ(catalyst / nexus / srs / apresia)どうしの生きているリンクで輪ができ、輪の上の
スイッチがすべて STP を動かしているわけではないとき「ループ発生中」とする。
(全台 STP なら実機は冗長リンクを Blocking にするのでループにならない)

ループ中に起きること:
- 輪の上のポートに大量のブロードキャスト(カウンタが毎秒 約 0.9Gbps 相当ずつ増える)・CPU 上昇
- Cisco 系スイッチは %SW_MATM-4-MACFLAP_NOTIF を出す
- 警報 l2_loop(1つのループにつき1件。止まると履歴に「どうやって止まったか」を残す)

止め方(設定されていれば実機と同じ順序・ログで動く):
- storm-control broadcast level <n> : 超えた分を捨てる(%STORM_CONTROL-3-FILTERED。ループ自体は続く)
  + storm-control action shutdown   : そのポートを err-disable(%PM-4-ERR_DISABLE: storm-control ...)
- spanning-tree bpduguard enable     : STP を動かしている相手から BPDU を受けたら err-disable
- errdisable recovery cause <cause> / errdisable recovery interval <s> : 時間が来たら自動で戻す
  (ループの原因が残っていればまた止まる。実機と同じ)
- APRESIA(D-Link 系 LBD): enable loopdetect / config loopdetect interval|recover_timer|ports
  : interval 秒ごとの検知フレームが戻ってきたらポートを Block、recover_timer 秒で戻す(0 は戻さない)

並列リンク(同じ2台の間の複数本)は EtherChannel と区別しないので1本の辺として数える(誤検知を避ける)。
副作用(ポートを落とす・ログ・警報)は app.py が actions を受け取って行う。
"""
import re
import time
from collections import deque

SWITCH_TYPES = frozenset({'catalyst', 'srs', 'apresia', 'nexus'})
CISCO_LIKE = frozenset({'catalyst', 'nexus', 'srs', 'cisco'})
STORM_PPS = 1_700_000          # 64 バイトで 1Gbps の約 9 割
STORM_SIZE = 64
MACFLAP_EVERY = 15             # 秒。MACFLAP を出し直す間隔
FILTERED_EVERY = 30            # 秒。STORM_CONTROL-3-FILTERED を出し直す間隔
DETECT_DELAY = 1.0             # 秒。storm-control / BPDU guard が効くまで
DEFAULT_RECOVERY_INTERVAL = 300
LBD_INTERVAL = 10
LBD_RECOVER = 60


def norm(ifname):
    s = (ifname or '').lower().strip()
    for p in ('tengigabitethernet', 'gigabitethernet', 'fastethernet', 'ethernet', 'port', 'te', 'gi', 'fa', 'eth', 'et'):
        if s.startswith(p) and len(s) > len(p) and (s[len(p)].isdigit()):
            return s[len(p):]
    return s


# ── 設定(装置ごと) ───────────────────────────────
def storm_cfg(state):
    if not isinstance(getattr(state, 'storm_control', None), dict):
        state.storm_control = {}
    return state.storm_control


def recovery_cfg(state):
    rc = getattr(state, 'errdisable_recovery', None)
    if not isinstance(rc, dict):
        rc = state.errdisable_recovery = {'causes': [], 'interval': DEFAULT_RECOVERY_INTERVAL}
    return rc


def lbd_cfg(state):
    c = getattr(state, 'loopdetect', None)
    if not isinstance(c, dict):
        c = state.loopdetect = {'enabled': False, 'interval': LBD_INTERVAL, 'recover': LBD_RECOVER, 'ports_off': []}
    return c


def _port_storm(state, ifname):
    n = norm(ifname)
    for k, v in storm_cfg(state).items():
        if norm(k) == n:
            return v
    return None


# ── CLI ──────────────────────────────────────────
def _ports_expand(spec):
    out = []
    for part in spec.split(','):
        m = re.match(r'^(\d+)-(\d+)$', part.strip())
        if m:
            out += [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        elif part.strip():
            out.append(part.strip())
    return out


def handle_cli(device_id, state, command, engine=None):
    """storm-control / errdisable recovery / APRESIA loopdetect の設定と show。該当しなければ None"""
    engine = engine or loops
    c = command.strip()
    cl = re.sub(r'\s+', ' ', c.lower())
    dt = getattr(state, 'device_type', '')
    mode = getattr(state, 'mode', '')
    if dt in CISCO_LIKE:
        if mode == 'config-if' and getattr(state, 'current_if', ''):
            ifn = state.current_if
            m = re.match(r'^storm-control broadcast level (\d+(?:\.\d+)?)(?: (\d+(?:\.\d+)?))?$', cl)
            if m:
                storm_cfg(state).setdefault(ifn, {'action': None})['level'] = float(m.group(1))
                if m.group(2):
                    storm_cfg(state)[ifn]['falling'] = float(m.group(2))
                return ''
            m = re.match(r'^storm-control action (shutdown|trap)$', cl)
            if m:
                storm_cfg(state).setdefault(ifn, {'level': None})['action'] = m.group(1)
                return ''
            if cl.startswith('no storm-control'):
                ent = storm_cfg(state).get(ifn)
                if ent is not None:
                    if 'action' in cl:
                        ent['action'] = None
                    else:
                        ent['level'] = None
                    if not ent.get('level') and not ent.get('action'):
                        storm_cfg(state).pop(ifn, None)
                return ''
        if mode == 'config':
            m = re.match(r'^(no )?errdisable recovery cause (bpduguard|storm-control|loopback|all)$', cl)
            if m:
                rc = recovery_cfg(state)
                causes = ['bpduguard', 'storm-control', 'loopback'] if m.group(2) == 'all' else [m.group(2)]
                for cz in causes:
                    if m.group(1) and cz in rc['causes']:
                        rc['causes'].remove(cz)
                    elif not m.group(1) and cz not in rc['causes']:
                        rc['causes'].append(cz)
                return ''
            m = re.match(r'^(no )?errdisable recovery interval(?: (\d+))?$', cl)
            if m:
                iv = DEFAULT_RECOVERY_INTERVAL if m.group(1) or not m.group(2) else int(m.group(2))
                if not 30 <= iv <= 86400:
                    return '% Invalid input detected (30-86400)'
                recovery_cfg(state)['interval'] = iv
                return ''
        if re.match(r'^show storm-control', cl):
            return show_storm_control(device_id, state, engine, cl)
        if re.match(r'^show errdisable recovery', cl):
            return show_errdisable_recovery(device_id, state, engine)
        if re.match(r'^show (l2 )?loop(s|-status)?$', cl):
            return show_loops(engine, device_id)
    if dt == 'apresia' and mode.startswith('config'):
        lb = lbd_cfg(state)
        if re.match(r'^enable loopdetect$', cl):
            lb['enabled'] = True
            return ''
        if re.match(r'^disable loopdetect$', cl):
            lb['enabled'] = False
            return ''
        m = re.match(r'^config loopdetect (interval|recover_timer) (\d+)$', cl)
        if m:
            v = int(m.group(2))
            if m.group(1) == 'interval':
                if not 1 <= v <= 32767:
                    return 'Fail! (interval: 1-32767)'
                lb['interval'] = v
            else:
                if v != 0 and not 60 <= v <= 1000000:
                    return 'Fail! (recover_timer: 0 or 60-1000000)'
                lb['recover'] = v
            return 'Success.'
        m = re.match(r'^config loopdetect ports (\S+) state (enabled|disabled)$', cl)
        if m:
            off = set(lb['ports_off'])
            for p in _ports_expand(m.group(1)):
                (off.discard if m.group(2) == 'enabled' else off.add)(norm(p))
            lb['ports_off'] = sorted(off)
            return 'Success.'
    if dt == 'apresia' and re.match(r'^show loopdetect', cl):
        return show_loopdetect(device_id, state, engine)
    return None


def show_storm_control(device_id, state, engine, cl=''):
    want = cl.split()[2] if len(cl.split()) > 2 and cl.split()[2] not in ('broadcast',) else None
    lines = ['Interface  Filter State   Upper        Lower        Current',
             '---------  -------------  -----------  -----------  ----------']
    for ifn, v in sorted(storm_cfg(state).items()):
        if want and norm(want) != norm(ifn):
            continue
        st = engine.port_state(device_id, ifn)
        if st and st.get('cause') == 'storm-control':
            fs = 'Shutdown'
        elif engine.storming(device_id, ifn):
            fs = 'Blocking'
        else:
            fs = 'Forwarding'
        cur = 100.0 if engine.storming(device_id, ifn) else 0.0
        lvl = v.get('level')
        low = v.get('falling', lvl)
        up_s = f'{lvl:.2f}%' if lvl is not None else '100.00%'
        lo_s = f'{low:.2f}%' if low is not None else '100.00%'
        lines.append(f"{_short(ifn):<11}{fs:<15}{up_s:<13}{lo_s:<13}{cur:.2f}%")
    if len(lines) == 2:
        lines.append('(storm-control の設定はありません)')
    return '\n'.join(lines)


def show_errdisable_recovery(device_id, state, engine):
    rc = recovery_cfg(state)
    lines = ['ErrDisable Reason            Timer Status', '-----------------            --------------']
    for cz in ('bpduguard', 'storm-control', 'loopback', 'link-flap', 'psecure-violation'):
        lines.append(f"{cz:<28}{'Enabled' if cz in rc['causes'] else 'Disabled'}")
    lines += ['', f"Timer interval: {rc['interval']} seconds", '', 'Interfaces that will be enabled at the next timeout:', '']
    rows = []
    for (d, ifn), v in sorted(engine.err.items()):
        if d != device_id or v['cause'] not in rc['causes']:
            continue
        left = max(0, int(v['since'] + rc['interval'] - time.time()))
        rows.append(f"{_short(ifn):<11}{v['cause']:<20}{left}")
    if rows:
        lines += ['Interface  Errdisable reason   Time left(sec)', '---------  -----------------   --------------'] + rows
    return '\n'.join(lines)


def show_loopdetect(device_id, state, engine):
    lb = lbd_cfg(state)
    lines = [' LBD Global Settings', ' ---------------------------',
             f" LBD Status            : {'Enabled' if lb['enabled'] else 'Disabled'}",
             ' LBD Mode              : Port-Based',
             f" LBD Interval          : {lb['interval']} sec",
             f" LBD Recover Time      : {lb['recover']} sec", '',
             ' Port        Loopdetect State  Loop Status', ' ----------- ----------------  -----------']
    nat = lambda x: [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', x)]  # noqa: E731
    for name in sorted(getattr(state, 'interfaces', {}) or {}, key=nat):
        if not name.lower().startswith('port'):
            continue
        on = lb['enabled'] and norm(name) not in lb['ports_off']
        blk = engine.lbd.get((device_id, norm(name)))
        lines.append(f" {name:<12}{('Enabled' if on else 'Disabled'):<18}{'Loop!' if blk else 'Normal'}")
    return '\n'.join(lines)


def show_loops(engine, device_id=None):
    act = [x for x in engine.active.values() if device_id is None or device_id in x['devices']]
    if not act:
        return 'L2 loop: none'
    out = []
    for x in act:
        out.append(f"L2 loop since {time.strftime('%H:%M:%S', time.localtime(x['since']))} "
                   f"({int(time.time() - x['since'])}s): " + ' - '.join(x['hostnames']))
        for e in x['edges']:
            out.append(f"  {e['ha']} {e['ia']}  <->  {e['hb']} {e['ib']}")
    return '\n'.join(out)


def _short(ifn):
    s = ifn
    for a, b in (('TenGigabitEthernet', 'Te'), ('GigabitEthernet', 'Gi'), ('FastEthernet', 'Fa'), ('Ethernet', 'Eth')):
        if s.startswith(a):
            return b + s[len(a):]
    return s


# ── 検知本体 ─────────────────────────────────────
def find_rings(vnet, types, stp_on):
    """スイッチどうしの生きているリンクのうち、輪の一部になっている辺(橋でない辺)を連結成分ごとに返す"""
    nodes = [d for d, t in types.items() if t in SWITCH_TYPES]
    nset = set(nodes)
    adj = {d: sorted(p for p in vnet.links.get(d, set()) if p in nset and vnet._edge_up(d, p)) for d in nodes}
    # 橋の検出(Tarjan、反復版)
    disc, low, bridges, t = {}, {}, set(), [0]
    for root in nodes:
        if root in disc:
            continue
        stack = [(root, None, iter(adj[root]))]
        disc[root] = low[root] = t[0]
        t[0] += 1
        while stack:
            u, parent, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                if parent is not None:
                    low[parent] = min(low[parent], low[u])
                    if low[u] > disc[parent]:
                        bridges.add(frozenset((parent, u)))
                continue
            if nxt == parent:
                continue
            if nxt in disc:
                low[u] = min(low[u], disc[nxt])
            else:
                disc[nxt] = low[nxt] = t[0]
                t[0] += 1
                stack.append((nxt, u, iter(adj[nxt])))
    edges = {frozenset((a, b)) for a in nodes for b in adj[a] if a < b} - bridges
    # 連結成分
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in edges:
        a, b = sorted(e)
        parent[find(a)] = find(b)
    comps = {}
    for e in edges:
        comps.setdefault(find(sorted(e)[0]), []).append(tuple(sorted(e)))
    rings = []
    for es in comps.values():
        devs = sorted({d for e in es for d in e})
        if all(stp_on(d) for d in devs):
            continue                      # 全台 STP: 冗長リンクは Blocking になりループしない
        rings.append({'key': '|'.join(f'{a}~{b}' for a, b in sorted(es)), 'devices': devs, 'edges': sorted(es)})
    return rings


def ring_order(edges):
    """輪を一周する順の装置列(単純な輪のとき。そうでなければ None)"""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    if any(len(v) != 2 for v in adj.values()):
        return None
    start = min(adj)
    order, prev, cur = [start], None, start
    while True:
        nxt = [x for x in adj[cur] if x != prev]
        nxt = nxt[0] if nxt else None
        if nxt is None or nxt == start:
            break
        order.append(nxt)
        prev, cur = cur, nxt
    return order if len(order) == len(adj) else None


class LoopEngine:
    def __init__(self):
        self.active = {}                   # key -> ループ
        self.history = deque(maxlen=50)    # 止まったループ
        self.err = {}                      # (dev, ifname) -> {'cause', 'since', 'prev'}
        self.lbd = {}                      # (dev, norm(if)) -> {'since', 'ifname'}
        self._storm_ports = set()          # 直近の tick でストーム中だった (dev, norm(if))
        self._last_log = {}                # (kind, dev, if) -> t

    def port_state(self, dev, ifname):
        return self.err.get((dev, ifname)) or next((v for (d, i), v in self.err.items() if d == dev and norm(i) == norm(ifname)), None)

    def storming(self, dev, ifname):
        return (dev, norm(ifname)) in self._storm_ports

    def snapshot(self, hostname=lambda d: d, now=None, recent=300, exists=None):
        now = time.time() if now is None else now
        act = [dict(x, age=round(now - x['since'], 1)) for x in self.active.values()
               if exists is None or all(exists(d) for d in x['devices'])]
        rec = [dict(x) for x in self.history if now - x['until'] <= recent]
        blocked = [{'dev': d, 'host': hostname(d), 'iface': i, 'cause': v['cause'], 'since': v['since']}
                   for (d, i), v in self.err.items()]
        blocked += [{'dev': d, 'host': hostname(d), 'iface': v['ifname'], 'cause': 'loopdetect', 'since': v['since']}
                    for (d, _), v in self.lbd.items()]
        return {'active': act, 'recent': rec, 'blocked': blocked, 'now': now}

    def _due(self, kind, dev, ifn, every, now):
        k = (kind, dev, ifn)
        if now - self._last_log.get(k, -1e9) >= every:
            self._last_log[k] = now
            return True
        return False

    def tick(self, vnet, states, stp_engine, now=None):
        """1秒ごとに呼ぶ。行うべき副作用の一覧を返す:
        {'do': 'log'|'errdisable'|'recover'|'storm'|'cpu'|'alert'|'cleared', ...}"""
        now = time.time() if now is None else now
        acts = []
        types = {d: getattr(s, 'device_type', '') for d, s in states.items()}
        host = lambda d: getattr(states.get(d), 'hostname', d)  # noqa: E731

        def stp_on(d):
            return bool((stp_engine.nodes.get(d) or {}).get('enabled'))

        # 自動復旧(errdisable recovery / LBD recover_timer)はループの有無に関係なく進める
        for (d, ifn), v in list(self.err.items()):
            st = states.get(d)
            rc = recovery_cfg(st) if st else {'causes': [], 'interval': DEFAULT_RECOVERY_INTERVAL}
            if v['cause'] in rc['causes'] and now - v['since'] >= rc['interval']:
                self.err.pop((d, ifn))
                acts.append({'do': 'log', 'dev': d, 'sev': 4,
                             'msg': f"%PM-4-ERR_RECOVER: Attempting to recover from {v['cause']} err-disable state on {ifn}"})
                acts.append({'do': 'recover', 'dev': d, 'iface': ifn, 'cause': v['cause'], 'prev': v.get('prev')})
        for (d, n), v in list(self.lbd.items()):
            st = states.get(d)
            rec = lbd_cfg(st)['recover'] if st else LBD_RECOVER
            if st is None or (rec and now - v['since'] >= rec):
                self.lbd.pop((d, n))
                if st is not None:
                    acts.append({'do': 'log', 'dev': d, 'sev': 4,
                                 'msg': f"Port {v['ifname']} LBD port recovered. Loop detection restarted"})
                    acts.append({'do': 'recover', 'dev': d, 'iface': v['ifname'], 'cause': 'loopdetect', 'prev': v.get('prev')})

        rings = find_rings(vnet, types, stp_on)
        seen = set()
        storm_ports = set()
        for r in rings:
            key = r['key']
            seen.add(key)
            edges = []
            for a, b in r['edges']:
                ia = vnet.interface_links.get(a, {}).get(b, b)
                ib = vnet.interface_links.get(b, {}).get(a, a)
                edges.append({'a': a, 'b': b, 'ia': ia, 'ib': ib, 'ha': host(a), 'hb': host(b)})
            lp = self.active.get(key)
            if lp is None:
                lp = self.active[key] = {'key': key, 'since': now, 'devices': r['devices'],
                                         'hostnames': [host(d) for d in r['devices']],
                                         'order': ring_order(r['edges']), 'edges': edges, 'events': []}
                path = ' - '.join(lp['hostnames'])
                acts.append({'do': 'alert', 'key': key, 'src': r['devices'][0],
                             'detail': f"L2ループ発生: {path}(輪 {len(edges)}本"
                                       f"{'、STP なし' if not any(stp_on(d) for d in r['devices']) else '、STP が一部だけ'})"})
            lp['edges'] = edges
            # 輪の上の各ポート: (装置, IF, 相手)
            ports = [(e['a'], e['ia'], e['b']) for e in edges] + [(e['b'], e['ib'], e['a']) for e in edges]
            elapsed = now - lp['since']
            for d, ifn, peer in ports:
                st = states.get(d)
                if st is None:
                    continue
                dt = types.get(d)
                sc = _port_storm(st, ifn)
                lvl = (sc or {}).get('level')
                storm_ports.add((d, norm(ifn)))
                acts.append({'do': 'storm', 'dev': d, 'iface': ifn,
                             'pps': int(STORM_PPS * (min(lvl, 100) / 100 if lvl is not None else 1))})
                if dt in ('catalyst', 'nexus', 'srs') and self._due('macflap', d, '', MACFLAP_EVERY, now):
                    pifs = [i for dd, i, _ in ports if dd == d]
                    if len(pifs) >= 2:
                        h = abs(hash(peer)) % (16 ** 6)
                        mac = f'001b.0d{(h >> 16) & 0xff:02x}.{(h >> 8) & 0xff:02x}{h & 0xff:02x}'
                        acts.append({'do': 'log', 'dev': d, 'sev': 4,
                                     'msg': f'%SW_MATM-4-MACFLAP_NOTIF: Host {mac} in vlan 1 is flapping between '
                                            f'port {_short(pifs[0])} and port {_short(pifs[1])}'})
                if elapsed < DETECT_DELAY:
                    continue
                # BPDU guard: STP を動かしている相手から BPDU が届く
                if dt in CISCO_LIKE and stp_on(peer) and _bpduguard(stp_engine, d, ifn) and not self.port_state(d, ifn):
                    acts += self._errdisable(d, ifn, 'bpduguard', now, st, lp,
                                             [f'%SPANTREE-2-BLOCK_BPDUGUARD: Received BPDU on port {ifn} with BPDU Guard enabled. Disabling port.'])
                    continue
                if sc and lvl is not None and dt in CISCO_LIKE and not self.port_state(d, ifn):
                    if sc.get('action') == 'shutdown':
                        acts += self._errdisable(d, ifn, 'storm-control', now, st, lp,
                                                 [f'%STORM_CONTROL-3-SHUTDOWN: A packet storm was detected on {ifn}. The interface has been disabled.'])
                        continue
                    if self._due('filtered', d, ifn, FILTERED_EVERY, now):
                        acts.append({'do': 'log', 'dev': d, 'sev': 3,
                                     'msg': f'%STORM_CONTROL-3-FILTERED: A Broadcast storm detected on {ifn}. '
                                            f'A packet filter action has been applied on the interface.'})
                        if sc.get('action') == 'trap':
                            acts.append({'do': 'trap', 'dev': d, 'iface': ifn})
            # APRESIA(LBD): interval 秒ごとの検知フレームが戻ってきたら、輪の2本目のポートを Block
            for d in r['devices']:
                st = states.get(d)
                if types.get(d) != 'apresia' or st is None:
                    continue
                lb = lbd_cfg(st)
                pifs = sorted({i for dd, i, _ in ports if dd == d and norm(i) not in lb['ports_off']})
                if not lb['enabled'] or len(pifs) < 2 or elapsed < lb['interval']:
                    continue
                ifn = pifs[-1]
                if (d, norm(ifn)) in self.lbd:
                    continue
                prev = (getattr(st, 'interfaces', {}).get(ifn) or {}).get('status')
                self.lbd[(d, norm(ifn))] = {'since': now, 'ifname': ifn, 'prev': prev}
                lp['events'].append({'t': now, 'dev': d, 'host': host(d), 'iface': ifn, 'cause': 'loopdetect'})
                acts.append({'do': 'log', 'dev': d, 'sev': 2, 'msg': f'Port {ifn} LBD loop occurred. Port blocked'})
                acts.append({'do': 'errdisable', 'dev': d, 'iface': ifn, 'cause': 'loopdetect', 'status': 'loop-blocked'})
            acts.append({'do': 'cpu', 'devs': r['devices']})
        self._storm_ports = storm_ports
        for key in [k for k in self.active if k not in seen]:
            lp = self.active.pop(key)
            how = lp['events'][-1] if lp['events'] else None
            stp_all = all(stp_on(d) for d in lp['devices'])
            by = (f"{how['host']} の {how['iface']} を {how['cause']} で遮断" if how
                  else 'STP が全台で有効になり冗長リンクを Blocking' if stp_all else 'リンクの切断(手動の shutdown 等)')
            rec = dict(lp, until=now, duration=round(now - lp['since'], 1), resolved_by=by)
            self.history.append(rec)
            acts.append({'do': 'cleared', 'loop': rec})
        return acts

    def _errdisable(self, d, ifn, cause, now, st, lp, pre_logs):
        prev = (getattr(st, 'interfaces', {}).get(ifn) or {}).get('status')
        self.err[(d, ifn)] = {'cause': cause, 'since': now, 'prev': prev}
        lp['events'].append({'t': now, 'dev': d, 'host': getattr(st, 'hostname', d), 'iface': ifn, 'cause': cause})
        out = [{'do': 'log', 'dev': d, 'sev': 2 if 'SPANTREE' in m else 3, 'msg': m} for m in pre_logs]
        out.append({'do': 'log', 'dev': d, 'sev': 4,
                    'msg': f'%PM-4-ERR_DISABLE: {cause} error detected on {ifn}, putting {ifn} in err-disable state'})
        out.append({'do': 'errdisable', 'dev': d, 'iface': ifn, 'cause': cause, 'status': 'err-disabled'})
        return out

    def forget_port(self, dev, ifname):
        """手動の shutdown → no shutdown で err-disable / LBD Block を解く(実機と同じ)"""
        for k in [k for k in self.err if k[0] == dev and norm(k[1]) == norm(ifname)]:
            self.err.pop(k)
        self.lbd.pop((dev, norm(ifname)), None)

    def reset(self):
        self.__init__()


def _bpduguard(stp_engine, dev, ifn):
    pc = getattr(stp_engine, 'port_config', {}).get(dev, {})
    n = norm(ifn)
    cfg = next((v for k, v in pc.items() if norm(k) == n), {}) or {}
    node = (getattr(stp_engine, 'nodes', {}) or {}).get(dev) or {}
    port = next((v for k, v in (node.get('ports') or {}).items() if norm(k) == n), {}) or {}
    if cfg.get('bpduguard') or port.get('bpduguard'):
        return True
    portfast = cfg.get('portfast') or port.get('portfast')
    return bool(portfast and (node.get('bpduguard_default') or getattr(stp_engine, 'bpduguard_default', {}).get(dev)))


loops = LoopEngine()


# ── running-config ────────────────────────────────
def running_config_global(state):
    rc = getattr(state, 'errdisable_recovery', None)
    if not isinstance(rc, dict):
        return []
    out = [f'errdisable recovery cause {c}' for c in rc.get('causes', [])]
    if rc.get('interval', DEFAULT_RECOVERY_INTERVAL) != DEFAULT_RECOVERY_INTERVAL:
        out.append(f"errdisable recovery interval {rc['interval']}")
    return out


def running_config_iface(state, ifname):
    v = _port_storm(state, ifname) if isinstance(getattr(state, 'storm_control', None), dict) else None
    if not v:
        return []
    out = []
    if v.get('level') is not None:
        out.append(f" storm-control broadcast level {v['level']:.2f}"
                   + (f" {v['falling']:.2f}" if v.get('falling') is not None else ''))
    if v.get('action'):
        out.append(f" storm-control action {v['action']}")
    return out
