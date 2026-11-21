"""
OSPF の隣接を SNMP で直接読む(スタンドアロン版。ヴァルハラ=snmp_exporter が無い構成向け)

読むもの(OSPF-MIB RFC 1850):
  ospfRouterId 1.3.6.1.2.1.14.1.1.0 / ospfAdminStat 1.3.6.1.2.1.14.1.2.0
  ospfNbrTable 1.3.6.1.2.1.14.10.1(.1 相手の IP / .3 相手の Router-ID / .6 状態 1=down … 8=full)
  ospfIfAreaId 1.3.6.1.2.1.14.7.1.3(index = 自分の IF の IP.0) → エリア
  ipAddrTable 1.3.6.1.2.1.4.20.1(.2 IfIndex / .3 NetMask)+ ifName → 相手の IP がどの IF の先か

設定(devices)に無い相手(別の会社のルータ、同じ装置の VRF など)も、Router-ID の名前で1台として画面に置く。
Cisco の OSPF-MIB はふつうグローバルの OSPF プロセスだけを見せる(VRF のプロセスは SNMP のコンテキストが別)。
"""
import ipaddress

from . import snmp_client as SC

RID, ADMIN = '1.3.6.1.2.1.14.1.1.0', '1.3.6.1.2.1.14.1.2.0'
NBR = '1.3.6.1.2.1.14.10.1'
IF_AREA = '1.3.6.1.2.1.14.7.1.3'
IPADDR = '1.3.6.1.2.1.4.20.1'
IFNAME = '1.3.6.1.2.1.31.1.1.1.1'
STATE = {1: 'Down', 2: 'Attempt', 3: 'Init', 4: 'TwoWay', 5: 'ExStart', 6: 'Exchange', 7: 'Loading', 8: 'Full'}


def _s(v):
    return v.decode('utf-8', 'replace') if isinstance(v, (bytes, bytearray)) else str(v)


def _ip_of(v):
    if isinstance(v, (bytes, bytearray)) and len(v) == 4:
        return '.'.join(str(b) for b in v)
    return _s(v)


def read(host, comm, **kw):
    """1台分の OSPF の様子。OSPF が無ければ None"""
    got = SC.get(host, comm, [RID, ADMIN], **kw)
    if got.get(ADMIN) != 1 or RID not in got:
        return None
    nbrs = {}
    for oid, v in SC.walk(host, comm, NBR, **kw):
        col, idx = oid[len(NBR) + 1:].split('.', 1)
        nbrs.setdefault(idx, {})[col] = v
    ifname = {oid.rsplit('.', 1)[1]: _s(v) for oid, v in SC.walk(host, comm, IFNAME, **kw)}
    addrs = {}                       # 自分の IP -> {ifindex, mask}
    for oid, v in SC.walk(host, comm, IPADDR + '.2', **kw):
        addrs.setdefault(oid[len(IPADDR) + 3:], {})['if'] = str(v)
    for oid, v in SC.walk(host, comm, IPADDR + '.3', **kw):
        addrs.setdefault(oid[len(IPADDR) + 3:], {})['mask'] = _ip_of(v)
    area = {}
    for oid, v in SC.walk(host, comm, IF_AREA, **kw):
        ip = oid[len(IF_AREA) + 1:].rsplit('.', 1)[0]
        area[ip] = _ip_of(v)
    out = []
    for n in nbrs.values():
        nip = _ip_of(n.get('1', ''))
        iface = ar = None
        for ip, a in addrs.items():
            try:
                if a.get('mask') and ipaddress.ip_address(nip) in ipaddress.ip_network(f"{ip}/{a['mask']}", strict=False):
                    iface, ar = ifname.get(a.get('if'), None), area.get(ip)
                    break
            except ValueError:
                continue
        out.append({'ip': nip, 'rid': _ip_of(n.get('3', '')), 'state': STATE.get(int(n.get('6', 1) or 1), 'Down'),
                    'iface': iface, 'area': ar})
    return {'rid': _ip_of(got[RID]), 'neighbors': out, 'areas': sorted({a for a in area.values() if a})}


def snapshot(cfg, ospf_now, dev_of):
    """ラボ版 /api/traffic/ospf と同じ形。ospf_now = {装置: read() の結果}"""
    devs = {d['id']: d for d in cfg['devices']}
    nodes, seen, adjs = {}, {}, []
    rid_dev = {o['rid']: d for d, o in ospf_now.items() if o}
    for d, o in ospf_now.items():
        if o:
            nodes[d] = {'id': d, 'hostname': devs.get(d, {}).get('hostname', d), 'router_id': o['rid'], 'areas': o['areas']}
    for d, o in ospf_now.items():
        if not o:
            continue
        for n in o['neighbors']:
            peer = rid_dev.get(n['rid']) or dev_of(n['ip'], n['rid'])
            if not peer:
                peer = f"ospf:{n['rid']}"
                nodes.setdefault(peer, {'id': peer, 'hostname': f"RID {n['rid']}", 'router_id': n['rid'], 'areas': []})
            if n.get('area') and n['area'] not in nodes[peer]['areas'] and peer.startswith('ospf:'):
                nodes[peer]['areas'].append(n['area'])
            seen[(d, peer)] = n
    done = set()
    for (a, b), n in seen.items():
        if (b, a) in done:
            continue
        done.add((a, b))
        back = seen.get((b, a))
        sa, sb = n['state'], (back['state'] if back else None)
        full = sa == 'Full' and sb in ('Full', None)
        st = 'full' if full else 'down' if sa == 'Down' and sb in ('Down', None) else 'partial'
        adjs.append({'a': a, 'b': b, 'iface_a': n.get('iface'), 'iface_b': back.get('iface') if back else None,
                     'area': n.get('area') or '', 'area_b': (back or {}).get('area') or '',
                     'state': st, 'state_a': sa, 'state_b': sb, 'dead_a': None, 'dead_b': None, 'dead_interval': None,
                     'role_a': None, 'role_b': None, 'link_down': False, 'why': []})
    devices = [{**v, 'process_id': None, 'abr': len(v['areas']) > 1, 'lsdb': None, 'routes': None, 'spf_runs': None,
                'neighbors_full': sum(1 for (m, _p), n in seen.items() if m == k and n['state'] == 'Full')}
               for k, v in sorted(nodes.items())]
    return {'enabled': bool(nodes), 'devices': devices, 'adjacencies': adjs, 'destinations': [], 'tree': None,
            'source': 'snmp'}
