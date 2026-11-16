"""
ヴァルハラ(Prometheus)とニルヴァーナ(Cyber View)をつなぐ部品

  ① render_metrics      ニルヴァーナの判断を Prometheus の形式で出す(GET /metrics)
  ② replay_frames       ヴァルハラの履歴から、過去の各時刻のリンク状態を組み立てる(3D 再生)
  ③ forecast            ヴァルハラの predict_linear / 平常時との差で、先の使用率と「いつもと違う」を出す
  ⑤ firing_alerts       ヴァルハラ側で発報中の警報を読む(ニルヴァーナの花に VALHALLA の花びらで出す)

Prometheus への問い合わせは標準ライブラリの urllib だけで行う(スタンドアロン版にもそのままコピーして使う)。
ラボ版と同じ内容を cyber-view-standalone/cvs/valhalla.py に置いている(ずれはテストで検出)。
"""
import json
import time
import urllib.parse
import urllib.request

# リンク状態 ⇔ 数値(メトリクスにする時の値)
STATE_NUM = {'ok': 0, 'warn': 1, 'crit': 2, 'down': 3, 'lost': 4}
NUM_STATE = {v: k for k, v in STATE_NUM.items()}
HEALTH_NUM = {'ok': 0, 'warn': 1, 'crit': 2}
OSPF_STATE_NUM = {'Down': 1, 'Attempt': 2, 'Init': 3, 'TwoWay': 4, 'ExStart': 5, 'Exchange': 6, 'Loading': 7, 'Full': 8}

UTIL_ALERT = 0.8          # 予測で「ここに達するまで何日」を出す使用率


# ── Prometheus への問い合わせ ─────────────────────────
# 問い合わせに使う opener。スタンドアロン版は構内の監視基盤へ直接行くためプロキシを通さないものに差し替える
OPENER = None


def _get(url, path, params, timeout=5):
    q = urllib.parse.urlencode(params)
    op = OPENER or urllib.request.build_opener()
    with op.open(url.rstrip('/') + path + '?' + q, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if d.get('status') != 'success':
        raise RuntimeError(d.get('error') or 'prometheus error')
    return d['data']


def query(url, expr, at=None):
    """instant query → [(labels, value)]"""
    p = {'query': expr}
    if at is not None:
        p['time'] = f'{float(at):.3f}'
    out = []
    for r in _get(url, '/api/v1/query', p).get('result', []):
        try:
            out.append((r['metric'], float(r['value'][1])))
        except (KeyError, ValueError, IndexError):
            continue
    return out


def query_range(url, expr, start, end, step):
    """range query → [(labels, [(t, value), ...])]"""
    d = _get(url, '/api/v1/query_range', {'query': expr, 'start': f'{start:.3f}', 'end': f'{end:.3f}',
                                         'step': f'{max(1, int(step))}s'}, timeout=15)
    out = []
    for r in d.get('result', []):
        vals = []
        for t, v in r.get('values', []):
            try:
                vals.append((float(t), float(v)))
            except ValueError:
                continue
        out.append((r['metric'], vals))
    return out


def firing_alerts(url):
    """ヴァルハラで発報中(firing)の警報 → [{'name','labels','annotations','active_at','severity'}]"""
    d = _get(url, '/api/v1/alerts', {})
    out = []
    for a in d.get('alerts', []):
        if a.get('state') != 'firing':
            continue
        lab = a.get('labels', {})
        out.append({'name': lab.get('alertname', '?'), 'labels': lab, 'annotations': a.get('annotations', {}),
                    'active_at': a.get('activeAt'), 'severity': lab.get('severity', 'warning')})
    return out


# ── ① ニルヴァーナの判断をメトリクスに ──────────────────
def _esc(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def _labels(d):
    return '{' + ','.join(f'{k}="{_esc(v)}"' for k, v in d.items()) + '}' if d else ''


def render_metrics(links=(), health=None, ospf=None, alert_counts=None, route_counts=None, now=None):
    """Prometheus のテキスト形式(text/plain; version=0.0.4)を返す。

    links:  /api/traffic/links の links(a, b, host_a, host_b, state, ab_bps, ba_bps, util, down)
    health: /api/traffic/health の結果
    ospf:   /api/traffic/ospf の結果
    alert_counts: {(rule, severity, source): 件数}(起動からの累計)
    route_counts: {(device, kind): 件数}(起動からの累計)
    """
    out = []

    def head(name, typ, help_):
        out.append(f'# HELP {name} {help_}')
        out.append(f'# TYPE {name} {typ}')

    head('netlab_link_state', 'gauge', 'Link state judged by the Cyber View (0=ok 1=warn 2=crit 3=down 4=lost)')
    for ln in links:
        lab = {'a': ln['a'], 'b': ln['b'], 'host_a': ln.get('host_a', ln['a']), 'host_b': ln.get('host_b', ln['b'])}
        out.append(f'netlab_link_state{_labels(lab)} {STATE_NUM.get(ln.get("state"), 0)}')
    head('netlab_link_bps', 'gauge', 'Link traffic in bits per second by direction')
    for ln in links:
        base = {'a': ln['a'], 'b': ln['b'], 'host_a': ln.get('host_a', ln['a']), 'host_b': ln.get('host_b', ln['b'])}
        out.append(f'netlab_link_bps{_labels({**base, "dir": "ab"})} {float(ln.get("ab_bps") or 0):g}')
        out.append(f'netlab_link_bps{_labels({**base, "dir": "ba"})} {float(ln.get("ba_bps") or 0):g}')
    head('netlab_link_util', 'gauge', 'Link utilization ratio (max of both directions)')
    for ln in links:
        lab = {'a': ln['a'], 'b': ln['b'], 'host_a': ln.get('host_a', ln['a']), 'host_b': ln.get('host_b', ln['b'])}
        out.append(f'netlab_link_util{_labels(lab)} {float(ln.get("util") or 0):g}')

    if health:
        head('netlab_health_state', 'gauge', 'Status board per viewpoint (0=ok 1=warn 2=crit)')
        for c in health.get('categories', []):
            out.append(f'netlab_health_state{_labels({"category": c["key"]})} {HEALTH_NUM.get(c["state"], 0)}')
        out.append(f'netlab_health_state{_labels({"category": "overall"})} {HEALTH_NUM.get(health.get("overall"), 0)}')

    if ospf and ospf.get('enabled'):
        head('netlab_ospf_adjacency_state', 'gauge', 'OSPF neighbor state seen from device (1=Down ... 8=Full)')
        head('netlab_ospf_dead_remaining_seconds', 'gauge', 'OSPF dead timer remaining for the neighbor')
        hn = {d['id']: d['hostname'] for d in ospf.get('devices', [])}
        for a in ospf.get('adjacencies', []):
            for me, peer, st, dead in ((a['a'], a['b'], a['state_a'], a['dead_a']), (a['b'], a['a'], a['state_b'], a['dead_b'])):
                lab = {'device': hn.get(me, me), 'neighbor': hn.get(peer, peer), 'area': a.get('area', '')}
                out.append(f'netlab_ospf_adjacency_state{_labels(lab)} {OSPF_STATE_NUM.get(st, 1)}')
                if dead is not None:
                    out.append(f'netlab_ospf_dead_remaining_seconds{_labels(lab)} {dead:.1f}')
        head('netlab_ospf_spf_runs_total', 'counter', 'SPF calculations run on the device')
        for d in ospf.get('devices', []):
            out.append(f'netlab_ospf_spf_runs_total{_labels({"device": d["hostname"], "router_id": d["router_id"]})} {d.get("spf_runs", 0)}')

    head('netlab_alerts_total', 'counter', 'Alerts raised by the Cyber View since start')
    for (rule, sev, src), n in sorted((alert_counts or {}).items()):
        out.append(f'netlab_alerts_total{_labels({"rule": rule, "severity": sev, "source": src})} {n}')
    head('netlab_route_changes_total', 'counter', 'Routing table changes seen by the Cyber View since start')
    for (dev, kind), n in sorted((route_counts or {}).items()):
        out.append(f'netlab_route_changes_total{_labels({"device": dev, "kind": kind})} {n}')
    return '\n'.join(out) + '\n'


# ── ② 再生 ───────────────────────────────────────
def _key(lab):
    return lab.get('a', ''), lab.get('b', '')


def replay_frames_lab(url, start, end, step):
    """① で記録したニルヴァーナ自身の判断(netlab_link_*)から、各時刻のリンク状態を組み立てる"""
    frames = {}
    links = {}
    for lab, vals in query_range(url, 'netlab_link_state', start, end, step):
        k = _key(lab)
        links[k] = {'a': k[0], 'b': k[1], 'host_a': lab.get('host_a', k[0]), 'host_b': lab.get('host_b', k[1])}
        for t, v in vals:
            frames.setdefault(t, {}).setdefault(k, {})['state'] = NUM_STATE.get(int(v), 'ok')
    for lab, vals in query_range(url, 'netlab_link_bps', start, end, step):
        k = _key(lab)
        for t, v in vals:
            frames.setdefault(t, {}).setdefault(k, {})['ab_bps' if lab.get('dir') == 'ab' else 'ba_bps'] = v
    for lab, vals in query_range(url, 'netlab_link_util', start, end, step):
        k = _key(lab)
        for t, v in vals:
            frames.setdefault(t, {}).setdefault(k, {})['util'] = v
    return _assemble(frames, links)


def _assemble(frames, links):
    out = []
    prev = {}
    events = []
    for t in sorted(frames):
        ls = []
        for k, v in frames[t].items():
            if k not in links:
                continue
            st = v.get('state', 'ok')
            ls.append({**links[k], 'state': st, 'down': st == 'down', 'ab_bps': v.get('ab_bps', 0.0),
                       'ba_bps': v.get('ba_bps', 0.0), 'util': v.get('util', 0.0)})
            if k in prev and prev[k] != st:
                events.append({'t': t, 'a': k[0], 'b': k[1], 'from': prev[k], 'to': st,
                               'text': f"{links[k]['host_a']} ⇄ {links[k]['host_b']}: {prev[k]} → {st}"})
            prev[k] = st
        out.append({'t': t, 'links': ls})
    return {'frames': out, 'events': events}


# ── ③ 予測・いつもと違う ──────────────────────────────
def forecast(url, util_expr, label_keys=('a', 'b'), horizon_days=7, fit_window='1h', base_window='1d', at=None):
    """util_expr(リンクごとの使用率 0..1 の系列)から、
    - 今の値・傾き(1日あたり)・horizon 日後の予測・UTIL_ALERT に達するまでの日数
    - 平常時(base_window)の平均・標準偏差に対する今の値のずれ(z)
    を返す。データが短い間は fit_window を短くして呼ぶ。"""
    def by_key(expr):
        return {tuple(lab.get(k, '') for k in label_keys): v for lab, v in query(url, expr, at)}
    cur = by_key(util_expr)
    slope = by_key(f'deriv(({util_expr})[{fit_window}:])')
    pred = by_key(f'predict_linear(({util_expr})[{fit_window}:], {int(horizon_days * 86400)})')
    avg = by_key(f'avg_over_time(({util_expr})[{base_window}:])')
    sd = by_key(f'stddev_over_time(({util_expr})[{base_window}:])')
    out = []
    for k, u in cur.items():
        s = slope.get(k, 0.0) * 86400                # 1日あたりの増加
        days = None
        if u < UTIL_ALERT and s > 1e-9:
            days = (UTIL_ALERT - u) / s
        z = None
        if k in sd and sd[k] > 1e-9:
            z = (u - avg.get(k, u)) / sd[k]
        out.append({**dict(zip(label_keys, k)), 'util': u, 'slope_per_day': s,
                    'predicted': min(1.0, max(0.0, pred.get(k, u))), 'horizon_days': horizon_days,
                    'days_to_alert': days, 'z': z, 'baseline': avg.get(k)})
    return out


def now():
    return time.time()
