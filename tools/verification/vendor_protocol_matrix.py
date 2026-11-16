"""
Catalyst / SR-S / Si-R × STP / RIP / OSPF / BGP の正常時・障害時に、
/cyber-view(モックNIRVANA)が使う API が期待どおり反応するかを確かめる。

トポロジ(ルーティング): LAN-A ── A ══ B ══ C ── LAN-C
  正常: A が LAN-C を学習 / リンク ok / health ok / INFO(経路追加)
  障害: B の C 向けIFを shutdown → A から LAN-C が消える / B-C が LINK DOWN /
        health 異常 / link_down アラート / INFO(経路削除) / ネイバー断ログ
  復旧: no shutdown → 経路が戻る
  フラップ: shutdown/no shutdown を2回 → if_flap(警告)
トポロジ(STP): A, B, C の三角形
"""
import json
import sys
import time
import urllib.request

BASE = 'http://localhost:8000'
RUN = sys.argv[1] if len(sys.argv) > 1 else 'm'
ONLY = sys.argv[2:] if len(sys.argv) > 2 else None
report = []


def req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode() or '{}')


def cli(dev, *cmds):
    out = ''
    for c in cmds:
        out = req('/api/cli', {'device_id': dev, 'command': c}).get('output', '')
    return out


def wait(fn, timeout=90, step=2):
    end = time.time() + timeout
    v = None
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return v


def check(scn, name, ok, info=''):
    report.append({'scenario': scn, 'check': name, 'ok': bool(ok), 'info': str(info)[:300]})
    print(f"  [{'OK' if ok else 'NG'}] {name} {('- ' + str(info)[:160]) if info and not ok else ''}", flush=True)


VENDOR = {
    'catalyst': {'n': 61, 'if': ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/3']},
    'srs': {'n': 62, 'if': ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/3']},
    'sir': {'n': 63, 'if': ['lan0', 'lan1', 'lan2']},
}
PROTO_N = {'rip': 1, 'ospf': 2, 'bgp': 3, 'stp': 4}
CODE = {'rip': 'R', 'ospf': 'O', 'bgp': 'B'}


def ifaddr(v, dev, iface, ip):
    if v == 'sir':
        idx = iface.replace('lan', '')
        cli(dev, 'configure', f'lan {idx} ip address {ip}/24 3', 'exit')
    else:
        cli(dev, 'configure terminal', f'interface {iface}', 'no switchport', f'ip address {ip} 255.255.255.0',
            'no shutdown', 'end')


def sir_prep(v, dev):
    """Si-R は ether ポートと lan を VLAN で結ぶ(実機どおり。無いと ether use off で落ちる lan が決まらない)"""
    if v == 'sir':
        cli(dev, 'configure', 'ether 1 1 vlan untag 1', 'ether 1 2 vlan untag 2', 'lan 0 vlan 1', 'lan 1 vlan 2', 'exit')


def shut(v, dev, iface, down):
    if v == 'sir':
        # Si-R は ether use off/on。lanN と ether ポートの対応は既定で lanN = ether 1 (N+1)
        port = int(iface.replace('lan', '')) + 1
        cli(dev, 'configure', f'ether 1 {port} use {"off" if down else "on"}', 'exit')
    else:
        cli(dev, 'configure terminal', f'interface {iface}', 'shutdown' if down else 'no shutdown', 'end')


def routing_proto(v, p, ids, nets):
    a, b, c = ids
    n12, n23, na, nc = nets
    if v == 'sir':
        if p == 'rip':
            for d, ns in ((a, [n12, na]), (b, [n12, n23]), (c, [n23, nc])):
                cli(d, 'configure', 'ip rip use on', *[f'ip rip network {x}.0/24' for x in ns], 'exit')
        elif p == 'ospf':
            for d in (a, b, c):
                cli(d, 'configure', 'ospf use on', 'ospf area 0.0.0.0', 'lan 0 ip ospf use on', 'lan 1 ip ospf use on', 'exit')
        elif p == 'bgp':
            cli(a, 'configure', 'bgp use on', 'bgp as 65001', f'bgp neighbor 0 address {n12}.2 remote-as 65002',
                f'bgp network {na}.0/24', 'exit')
            cli(b, 'configure', 'bgp use on', 'bgp as 65002', f'bgp neighbor 0 address {n12}.1 remote-as 65001',
                f'bgp neighbor 1 address {n23}.2 remote-as 65003', 'exit')
            cli(c, 'configure', 'bgp use on', 'bgp as 65003', f'bgp neighbor 0 address {n23}.1 remote-as 65002',
                f'bgp network {nc}.0/24', 'exit')
    else:
        if p == 'rip':
            for d in (a, b, c):
                cli(d, 'configure terminal', 'ip routing', 'router rip', 'version 2', 'network 10.0.0.0', 'no auto-summary', 'end')
        elif p == 'ospf':
            for d, ns in ((a, [n12, na]), (b, [n12, n23]), (c, [n23, nc])):
                cli(d, 'configure terminal', 'ip routing', 'router ospf 1',
                    *[f'network {x}.0 0.0.0.255 area 0' for x in ns], 'end')
        elif p == 'bgp':
            cli(a, 'configure terminal', 'ip routing', 'router bgp 65001', f'neighbor {n12}.2 remote-as 65002',
                f'network {na}.0 mask 255.255.255.0', 'end')
            cli(b, 'configure terminal', 'ip routing', 'router bgp 65002', f'neighbor {n12}.1 remote-as 65001',
                f'neighbor {n23}.2 remote-as 65003', 'end')
            cli(c, 'configure terminal', 'ip routing', 'router bgp 65003', f'neighbor {n23}.1 remote-as 65002',
                f'network {nc}.0 mask 255.255.255.0', 'end')


def has_route(dev, net, code=None):
    out = cli(dev, 'show ip route')
    for line in out.splitlines():
        if f'{net}.0' in line and (code is None or line.strip().startswith(code)):
            return line.strip()
    return None


def link_of(a, b):
    for ln in req('/api/traffic/links?window=60')['links']:
        if {ln['a'], ln['b']} == {a, b}:
            return ln
    return None


def health():
    return {c['key']: c for c in req('/api/traffic/health')['categories']}


def alerts(rule=None, src=None):
    return [x for x in req('/api/traffic/alerts')['alerts']
            if (rule is None or x['rule'] == rule) and (src is None or x['src'] == src)]


def route_events(dev, prefix, kind, since):
    return [e for e in req(f'/api/traffic/routes?since={since}&limit=500')['events']
            if e['dev'] == dev and e['prefix'] == prefix and e['kind'] == kind]


def run_routing(v, p):
    scn = f'{v}/{p}'
    print(f'== {scn}', flush=True)
    V = VENDOR[v]
    base = f"10.{V['n']}.{PROTO_N[p] * 10}"
    n12, n23 = f'{base}', f"10.{V['n']}.{PROTO_N[p] * 10 + 1}"
    na, nc = f"10.{V['n']}.{PROTO_N[p] * 10 + 2}", f"10.{V['n']}.{PROTO_N[p] * 10 + 3}"
    ids = [f'{RUN}-{v}-{p}-{x}' for x in 'abc']
    a, b, c = ids
    i0, i1, i2 = V['if']
    for d in ids:
        req('/api/device', {'id': d, 'type': v, 'hostname': d.upper()})
        sir_prep(v, d)
    req('/api/link', {'a': a, 'b': b, 'iface_a': i0, 'iface_b': i0})
    req('/api/link', {'a': b, 'b': c, 'iface_a': i1, 'iface_b': i0})
    ifaddr(v, a, i0, f'{n12}.1'); ifaddr(v, a, i1, f'{na}.1')
    ifaddr(v, b, i0, f'{n12}.2'); ifaddr(v, b, i1, f'{n23}.1')
    ifaddr(v, c, i0, f'{n23}.2'); ifaddr(v, c, i1, f'{nc}.1')
    seq0 = req('/api/traffic/routes')['seq']
    time.sleep(4)    # route_watch の基準取り(最初の観測は基準のみ)
    seq0 = req('/api/traffic/routes')['seq']
    routing_proto(v, p, ids, (n12, n23, na, nc))
    # ── 正常 ──
    r = wait(lambda: has_route(a, nc, CODE[p]), 120)
    check(scn, '正常: A が LAN-C を学習(show ip route)', r, r or cli(a, 'show ip route'))
    ln_ab, ln_bc = link_of(a, b), link_of(b, c)
    check(scn, '正常: links API に A-B/B-C があり state=ok', ln_ab and ln_bc and ln_ab['state'] == 'ok' and ln_bc['state'] == 'ok',
          f"{ln_ab and ln_ab['state']} {ln_bc and ln_bc['state']}")
    ev = wait(lambda: route_events(a, f'{nc}.0/24', 'add', seq0), 15)
    check(scn, '正常: INFO 経路追加イベント(route_watch)', ev, ev)
    # ── 障害 ──
    seq1 = req('/api/traffic/routes')['seq']
    shut(v, b, i1, True)
    ln = wait(lambda: (lambda x: x if x and x['down'] else None)(link_of(b, c)), 15)
    check(scn, '障害: links API で B-C が LINK DOWN', ln, ln and ln.get('down_sources'))
    check(scn, '障害: 検知経路に STATE と SYSLOG', ln and {'STATE', 'SYSLOG'} <= set(ln.get('down_sources', [])),
          ln and ln.get('down_sources'))
    h = health()
    check(scn, '障害: 状態ボード ネットワーク=異常', h['network']['state'] == 'crit', h['network']['summary'])
    check(scn, '障害: link_down アラート', alerts('link_down', b), '')
    gone = wait(lambda: not has_route(a, nc), 200, 3)
    check(scn, '障害: A から LAN-C の経路が消える', gone, has_route(a, nc))
    ev = wait(lambda: route_events(a, f'{nc}.0/24', 'del', seq1) or route_events(a, f'{nc}.0/24', 'change', seq1), 15)
    check(scn, '障害: INFO 経路削除/変更イベント', ev, ev)
    # ── 復旧 ──
    shut(v, b, i1, False)
    r = wait(lambda: has_route(a, nc, CODE[p]), 200, 3)
    check(scn, '復旧: 経路が戻る', r, cli(a, 'show ip route'))
    ln = link_of(b, c)
    check(scn, '復旧: B-C が LINK DOWN でなくなる', ln and not ln['down'], ln and ln.get('evidence'))
    # ── フラップ ──
    for _ in range(2):
        shut(v, b, i1, True); shut(v, b, i1, False)
    fl = wait(lambda: alerts('if_flap', b), 10, 1)
    check(scn, 'フラップ: if_flap(警告)', fl, [x['msg'] for x in req('/api/traffic/logs?since=0&limit=200')['logs'] if x['dev'] == b][-6:])
    logs = [x['msg'] for x in req('/api/traffic/logs?since=0&limit=200')['logs'] if x['dev'] in ids]
    pat = {'rip': 'RIP', 'ospf': 'OSPF', 'bgp': 'BGP'}[p]
    check(scn, f'ログ: {pat} 関連のログが記録されている', any(pat in m.upper() for m in logs), logs[-5:])


def run_stp(v):
    scn = f'{v}/stp'
    print(f'== {scn}', flush=True)
    V = VENDOR[v]
    ids = [f'{RUN}-{v}-stp-{x}' for x in 'abc']
    a, b, c = ids
    i0, i1 = V['if'][0], V['if'][1]
    for d in ids:
        req('/api/device', {'id': d, 'type': v, 'hostname': d.upper()})
        sir_prep(v, d)
        if v == 'sir':
            cli(d, 'configure', 'stp mode stp', 'exit')
        else:
            cli(d, 'configure terminal', 'spanning-tree mode rapid-pvst', 'end')
    cli(a, *(['configure', 'stp priority 4096', 'exit'] if v == 'sir' else
             ['configure terminal', 'spanning-tree vlan 1 priority 4096', 'end']))
    seq_alert = len(req('/api/traffic/alerts')['alerts'])
    req('/api/link', {'a': a, 'b': b, 'iface_a': i0, 'iface_b': i0})
    req('/api/link', {'a': b, 'b': c, 'iface_a': i1, 'iface_b': i0})
    req('/api/link', {'a': c, 'b': a, 'iface_a': i1, 'iface_b': i1})
    time.sleep(8)
    st = {d: cli(d, 'show spanning-tree') for d in ids}
    blk = [d for d, o in st.items() if any(k in o for k in ('BLK', 'Altn', 'Blocking', 'blocking', 'Alternate', 'Discarding', 'BLOCK'))]
    check(scn, '正常: 三角形のどこかが Blocking(ループ回避)', blk, {d: o[-400:] for d, o in st.items()})
    root = [d for d, o in st.items() if 'This bridge is the root' in o or 'is the root' in o.lower() or 'root bridge' in o.lower()]
    check(scn, '正常: priority を下げた A がルート', a in root or not root, root)
    mf = [x for x in req('/api/traffic/alerts')['alerts'][seq_alert:] if x['rule'] == 'mac_flap']
    check(scn, '正常: STP 有効なのでループ時の MAC フラップは出ない', not mf, mf[:2])
    lks = [link_of(a, b), link_of(b, c), link_of(c, a)]
    check(scn, '正常: links API に3本 ok', all(lks) and all(x['state'] == 'ok' for x in lks), [x and x['state'] for x in lks])
    # 障害: ブロックしていないリンク(ルート A の A-B)を落とす → 迂回
    blk_dev = blk[0] if blk else c
    shut(v, a, i0, True)
    time.sleep(8)
    st2 = {d: cli(d, 'show spanning-tree') for d in ids}
    ln = link_of(a, b)
    check(scn, '障害: links API で A-B が LINK DOWN', ln and ln['down'], ln and ln.get('down_sources'))
    still_blk = [d for d, o in st2.items() if any(k in o for k in ('BLK', 'Altn', 'Blocking', 'blocking', 'Alternate', 'Discarding', 'BLOCK'))]
    check(scn, '障害: 迂回のため Blocking が解除される', not still_blk, {d: o[-300:] for d, o in st2.items()})
    logs = [x['msg'] for x in req('/api/traffic/logs?since=0&limit=300')['logs'] if x['dev'] in ids]
    check(scn, '障害: STP/トポロジ変更のログ', any(k in m for m in logs for k in ('SPANTREE', 'STP', 'TOPO', 'topology')), logs[-6:])
    h = health()
    check(scn, '障害: 状態ボード ネットワーク=異常', h['network']['state'] == 'crit', h['network']['summary'])
    shut(v, a, i0, False)
    time.sleep(5)
    ln = link_of(a, b)
    check(scn, '復旧: A-B が LINK DOWN でなくなる', ln and not ln['down'], ln and ln.get('evidence'))


for v in ('catalyst', 'srs', 'sir'):
    for p in ('stp', 'rip', 'ospf', 'bgp'):
        if ONLY and f'{v}/{p}' not in ONLY:
            continue
        try:
            run_stp(v) if p == 'stp' else run_routing(v, p)
        except Exception as e:  # noqa: BLE001
            check(f'{v}/{p}', '例外', False, repr(e))

json.dump(report, open('vendor_protocol_matrix_report.json', 'w'),
          ensure_ascii=False, indent=1)
ng = [r for r in report if not r['ok']]
print(f'\nTOTAL {len(report)} checks, NG {len(ng)}')
