"""
/cyber-view の OSPF 表示用スナップショット(GET /api/traffic/ospf)

OSPF エンジン(engine.protocols.ospf_engine)の状態から、画面に描くものだけを取り出す:
  - devices:      OSPF が動いている装置(Router-ID・エリア・ABR・LSDB 件数・SPF 回数)
  - adjacencies:  リンクごとの隣接状態(両端から見た state・Dead タイマー残り・DR/BDR・エリア)
  - destinations: 宛先として選べるサブネット(LAN を先、ルータ間リンクを後)
  - tree:         選んだ宛先への最短経路ツリー(各ルータの OSPF 経路の次ホップをたどったもの)

計算はしない(エンジンが持っている隣接・経路をそのまま見せる)。表示と show コマンドがずれないように。
"""
import ipaddress
import time

STATE_RANK = {'Down': 0, 'Init': 1, 'TwoWay': 2, 'ExStart': 3, 'Exchange': 4, 'Loading': 5, 'Full': 6}


def _norm_area(a):
    try:
        return str(ipaddress.ip_address(int(a)))
    except (ValueError, TypeError):
        return str(a or '0.0.0.0')


def _net(ip, prefix):
    try:
        return ipaddress.ip_network(f'{ip}/{prefix}', strict=False)
    except ValueError:
        return None


def snapshot(eng, vnet, icmp, links, hostnames, dest=None, now=None):
    """eng=OspfEngine, links=[{'a','b','iface_a','iface_b','down'}], hostnames={id: name}"""
    now = now or time.time()
    nodes = {d: n for d, n in eng.nodes.items() if n.get('enabled') and d in hostnames}
    rid_to_dev = {n['router_id']: d for d, n in nodes.items()}

    devices = []
    for d, n in sorted(nodes.items()):
        areas = sorted({_norm_area(a) for a in eng.area_networks.get(d, {})} or {_norm_area(n.get('area_id'))})
        devices.append({
            'id': d, 'hostname': hostnames[d], 'router_id': n['router_id'], 'process_id': n.get('process_id', 1),
            'areas': areas, 'abr': len(areas) > 1, 'priority': n.get('priority', 1),
            'hello_interval': n.get('hello_interval'), 'dead_interval': n.get('dead_interval'),
            'lsdb': len(n.get('lsdb') or {}), 'routes': len(n.get('routes') or []),
            'spf_runs': n.get('spf_runs', 0), 'last_spf': n.get('last_spf'),
            'neighbors_full': sum(1 for k, x in n['neighbors'].items() if k in nodes and x.state == 'Full'),
        })

    def _view(x, y):
        """x から見た y の隣接(state, dead 残り秒, y の役割)"""
        nb = nodes[x]['neighbors'].get(y)
        if not nb:
            return 'Down', None, None
        dead = None
        if nb.last_hello:
            dead = max(0.0, nodes[x]['dead_interval'] - (now - nb.last_hello))
        return nb.state, dead, getattr(nb, 'role', None)

    def _on_ospf(dev, ifn):
        ifs = eng.ospf_enabled_ifaces(dev)
        if ifs is None or not ifn:
            return True
        return vnet._norm_iface(ifn) in {vnet._norm_iface(i) for i in ifs}

    adjs = []
    for ln in links:
        a, b = ln['a'], ln['b']
        if a not in nodes or b not in nodes:
            continue
        if not (_on_ospf(a, ln.get('iface_a')) and _on_ospf(b, ln.get('iface_b'))):
            continue
        sa, da, role_b = _view(a, b)
        sb, db, role_a = _view(b, a)
        area_a = _norm_area(eng._area_for_peer(a, b) or nodes[a].get('area_id'))
        area_b = _norm_area(eng._area_for_peer(b, a) or nodes[b].get('area_id'))
        why = []
        if ln.get('down'):
            why.append('リンク断')
        if area_a != area_b:
            why.append(f'エリア不一致 {area_a} / {area_b}')
        if nodes[a].get('hello_interval') != nodes[b].get('hello_interval'):
            why.append('Hello間隔の不一致')
        if (nodes[a].get('auth_key') or '') != (nodes[b].get('auth_key') or '') and (nodes[a].get('auth_key') or nodes[b].get('auth_key')):
            why.append('認証キーの不一致')
        if sa == 'Full' and sb == 'Full':
            st = 'full'
        elif sa == 'Down' and sb == 'Down':
            st = 'down'
        else:
            st = 'partial'
        # 両端のインタフェースコスト(show ip ospf interface の Cost と同じ値)
        _cf = getattr(eng, 'cost_for_iface', None)
        cost_a = _cf(a, ln.get('iface_a') or '') if _cf else None
        cost_b = _cf(b, ln.get('iface_b') or '') if _cf else None
        adjs.append({'a': a, 'b': b, 'iface_a': ln.get('iface_a'), 'iface_b': ln.get('iface_b'),
                     'cost_a': cost_a, 'cost_b': cost_b,
                     'area': area_a, 'area_b': area_b, 'state': st, 'state_a': sa, 'state_b': sb,
                     'dead_a': da, 'dead_b': db, 'dead_interval': nodes[a]['dead_interval'],
                     'role_a': role_a, 'role_b': role_b, 'link_down': bool(ln.get('down')), 'why': why})

    # 宛先(OSPF に載っている IF のサブネット)と、それを直結で持つ装置
    owners = {}
    for d in nodes:
        ifs = eng.ospf_enabled_ifaces(d)
        for name, info in (icmp.device_ips.get(d, {}).get('interfaces') or {}).items():
            if not isinstance(info, dict) or not info.get('ip'):
                continue
            if ifs is not None and name not in ifs:
                continue
            nw = _net(info['ip'], info.get('prefix', 24))
            if nw and not nw.network_address.is_loopback:
                owners.setdefault(str(nw), set()).add(d)
    dests = sorted(({'net': k, 'owners': sorted(v), 'transit': len(v) > 1} for k, v in owners.items()),
                   key=lambda x: (x['transit'], ipaddress.ip_network(x['net'])))

    tree = None
    if dest is None and dests:
        dest = dests[0]['net']
    dn = None
    if dest:
        try:
            dn = ipaddress.ip_network(dest, strict=False)
        except ValueError:
            dn = None
    if dn is not None:
        root = sorted(owners.get(str(dn), set()))
        edges, unreachable = [], []
        for d, n in sorted(nodes.items()):
            if d in root:
                continue
            best = None
            for r in n.get('routes') or []:
                rn = _net(r.get('network'), r.get('prefix', 24))
                if rn == dn and (best is None or r.get('metric', 0) < best.get('metric', 0)):
                    best = r
            nh = best and (rid_to_dev.get(best.get('next_hop')) or rid_to_dev.get(best.get('via'))
                           or (best.get('next_hop') if best.get('next_hop') in nodes else None))
            if nh:
                edges.append({'from': d, 'to': nh, 'cost': best.get('metric'), 'type': best.get('type', 'O')})
            else:
                unreachable.append(d)
        tree = {'dest': str(dn), 'root': root, 'edges': edges, 'unreachable': unreachable}

    return {'enabled': bool(nodes), 'devices': devices, 'adjacencies': adjs,
            'destinations': dests, 'tree': tree, 'now': now}
