"""
スタンドアロン版のヴァルハラ連携(実機の監視データ = snmp_exporter の生のメトリクスから作る)

  ② replay  : rate(ifHC*Octets) と ifOperStatus の履歴から、各時刻のリンク状態を組み立てる
  ③ forecast: IF ごとの使用率(rate / ifHighSpeed)の予測・平常時とのずれを、リンク単位にまとめる
  ④ ospf    : OSPF-MIB の ospfNbrState / ospfRouterId から、設定ファイルのリンクごとの隣接状態を作る
  ⑤ alerts  : ヴァルハラで発報中の警報を、新しく出たものだけ警報(花びら VALHALLA)にする
"""
from . import valhalla as V
from .link_evidence import norm_if

OSPF_NAMES = {v: k for k, v in V.OSPF_STATE_NUM.items()}


def _ends(cfg):
    """(管理IP, IF名) -> (リンク番号, 'a'|'b')"""
    out = {}
    devs = {d['id']: d for d in cfg['devices']}
    for i, ln in enumerate(cfg['links']):
        for side in ('a', 'b'):
            d = devs[ln[side]]
            out[(d.get('mgmt_ip'), norm_if(ln[f'{side}_if']))] = (i, side)
    return out


def _link_base(cfg, i):
    ln = cfg['links'][i]
    devs = {d['id']: d for d in cfg['devices']}
    return {'a': ln['a'], 'b': ln['b'], 'host_a': devs[ln['a']]['hostname'], 'host_b': devs[ln['b']]['hostname'],
            'iface_a': ln['a_if'], 'iface_b': ln['b_if']}


def _speed(cfg, i):
    return float(cfg['links'][i].get('speed') or 1_000_000_000)


def replay(url, job, cfg, start, end, step, classify):
    ends = _ends(cfg)
    sel = f'{{job="{job}"}}'
    frames = {}

    def put(expr, field, conv=float):
        for lab, vals in V.query_range(url, expr, start, end, step):
            k = (lab.get('instance', '').split(':')[0], norm_if(lab.get('ifName', '')))
            if k not in ends:
                continue
            i, side = ends[k]
            for t, v in vals:
                frames.setdefault(t, {}).setdefault(i, {})[(side, field)] = conv(v)
    w = f'{max(60, int(step) * 2)}s'
    put(f'rate(ifHCOutOctets{sel}[{w}]) * 8', 'out')
    put(f'rate(ifHCInOctets{sel}[{w}]) * 8', 'in')
    put(f'ifOperStatus{sel}', 'oper', lambda v: int(v))
    out, events, prev = [], [], {}
    for t in sorted(frames):
        ls = []
        for i, v in frames[t].items():
            ab = v.get(('a', 'out'), v.get(('b', 'in'), 0.0)) or 0.0
            ba = v.get(('b', 'out'), v.get(('a', 'in'), 0.0)) or 0.0
            down = v.get(('a', 'oper')) in (2, 7) or v.get(('b', 'oper')) in (2, 7)
            util = max(ab, ba) / _speed(cfg, i)
            st = classify(util, down)
            ls.append({**_link_base(cfg, i), 'state': st, 'down': down, 'ab_bps': ab, 'ba_bps': ba, 'util': util})
            if i in prev and prev[i] != st:
                b = _link_base(cfg, i)
                events.append({'t': t, 'a': b['a'], 'b': b['b'], 'from': prev[i], 'to': st,
                               'text': f"{b['host_a']} ⇄ {b['host_b']}: {prev[i]} → {st}"})
            prev[i] = st
        out.append({'t': t, 'links': ls})
    return {'frames': out, 'events': events}


def forecast(url, job, cfg, fit_window='1h', base_window='1d', horizon_days=7):
    ends = _ends(cfg)
    sel = f'{{job="{job}"}}'
    expr = (f'max by (instance, ifName) ((rate(ifHCOutOctets{sel}[2m]) > rate(ifHCInOctets{sel}[2m]) '
            f'or rate(ifHCInOctets{sel}[2m])) * 8 / (ifHighSpeed{sel} * 1e6))')
    rows = V.forecast(url, expr, ('instance', 'ifName'), horizon_days, fit_window, base_window)
    best = {}
    for r in rows:
        k = (r['instance'].split(':')[0], norm_if(r['ifName']))
        if k not in ends:
            continue
        i = ends[k][0]
        if i not in best or r['util'] > best[i]['util']:
            best[i] = r
    out = []
    for i, r in best.items():
        b = _link_base(cfg, i)
        out.append({**{k: v for k, v in r.items() if k not in ('instance', 'ifName')}, 'a': b['a'], 'b': b['b'],
                    'host_a': b['host_a'], 'host_b': b['host_b']})
    return out


def ospf(url, job, cfg, dev_of):
    """ラボ版の /api/traffic/ospf と同じ形(最短経路ツリーと Dead タイマーは SNMP では取れないので無し)"""
    sel = f'{{job="{job}"}}'
    devs = {d['id']: d for d in cfg['devices']}
    rid = {}
    for lab, v in V.query(url, f'ospfRouterId{sel}'):
        dev = dev_of(lab.get('instance'))
        if dev:
            rid[dev] = lab.get('ospfRouterId', '')
    seen = {}            # (自分, 相手) -> 状態名
    for lab, v in V.query(url, f'ospfNbrState{sel}'):
        me = dev_of(lab.get('instance'))
        peer = dev_of(lab.get('ospfNbrIpAddr'), lab.get('ospfNbrRtrId'))
        if me and peer:
            seen[(me, peer)] = OSPF_NAMES.get(int(v), 'Down')
    devices = [{'id': d, 'hostname': devs[d]['hostname'], 'router_id': r, 'process_id': None, 'areas': [],
                'abr': False, 'lsdb': None, 'routes': None, 'spf_runs': None,
                'neighbors_full': sum(1 for (m, _p), st in seen.items() if m == d and st == 'Full')}
               for d, r in sorted(rid.items())]
    adjs = []
    for ln in cfg['links']:
        a, b = ln['a'], ln['b']
        if a not in rid or b not in rid:
            continue
        sa, sb = seen.get((a, b), 'Down'), seen.get((b, a), 'Down')
        st = 'full' if sa == sb == 'Full' else 'down' if sa == sb == 'Down' else 'partial'
        adjs.append({'a': a, 'b': b, 'iface_a': ln['a_if'], 'iface_b': ln['b_if'], 'area': '', 'area_b': '',
                     'state': st, 'state_a': sa, 'state_b': sb, 'dead_a': None, 'dead_b': None, 'dead_interval': None,
                     'role_a': None, 'role_b': None, 'link_down': False, 'why': []})
    return {'enabled': bool(rid), 'devices': devices, 'adjacencies': adjs, 'destinations': [], 'tree': None,
            'source': 'valhalla'}


class AlertImporter:
    """発報中の警報を、新しく出たものだけ取り込む(発報し続けている間は二重にしない)"""

    SEV = {'critical': 'high', 'high': 'high', 'warning': 'warning', 'info': 'low', 'low': 'low', 'medium': 'medium'}

    def __init__(self):
        self.seen = set()

    def run(self, alerts, analytics, dev_of, now=None):
        cur, out = set(), []
        for a in alerts:
            fp = a['name'] + '|' + '|'.join(f'{k}={v}' for k, v in sorted(a['labels'].items()))
            cur.add(fp)
            if fp in self.seen:
                continue
            lab = a['labels']
            src = dev_of(lab.get('device'), lab.get('host_a'), lab.get('router'), lab.get('instance'))
            dst = dev_of(lab.get('neighbor'), lab.get('host_b'), lab.get('ospfNbrIpAddr'))
            detail = a['annotations'].get('summary') or a['annotations'].get('description') or a['name']
            r = analytics.fire('valhalla', fp, src, dst, f"{a['name']}: {detail}"[:200], now=now,
                               severity=self.SEV.get(str(a['severity']).lower(), 'medium'), name=f"ヴァルハラ: {a['name']}")
            if r:
                out.append(r)
        self.seen = cur
        return out
