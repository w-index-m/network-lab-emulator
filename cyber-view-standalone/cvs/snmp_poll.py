"""
実機から SNMP で直接、経路表と CPU を定期的に取る(スタンドアロン版)

  経路: IP-FORWARD-MIB の inetCidrRouteTable(無ければ古い ipCidrRouteTable)。毎回は経路数
        (inetCidrRouteNumber)だけを見て、数が変わった時と full_every 回に1回だけ表を取り直す
        (数が同じで次ホップだけ変わる場合も拾うため)。前回との差を route_watch で「情」にする。
  CPU:  CISCO-PROCESS-MIB の cpmCPUTotal5minRev。急に上がったら「情」、しきい値を hold 回続けて
        超えたら警告(注)、戻ったら「情」。

設定(config.yaml の sources.snmp):
  community / port / interval(秒, 既定 60)/ full_every(既定 5)/ timeout
  cpu: {high: 80, jump: 20, hold: 2}
装置ごとに devices[].snmp_community で上書きできる。
"""
import ipaddress
import threading
import time

from . import snmp_client as SC
from .route_watch import CpuWatch, RouteWatcher

INET_ROUTE = '1.3.6.1.2.1.4.24.7.1'
OLD_ROUTE = '1.3.6.1.2.1.4.24.4.1'
ROUTE_NUM = ['1.3.6.1.2.1.4.24.6.0', '1.3.6.1.2.1.4.24.3.0']
IFNAME = '1.3.6.1.2.1.31.1.1.1.1'
CPU_5MIN = '1.3.6.1.4.1.9.9.109.1.1.1.1.8'
PROTO = {1: 'other', 2: 'connected', 3: 'static', 8: 'rip', 9: 'isis', 13: 'ospf', 14: 'bgp', 16: 'eigrp'}


def parse_inet_index(idx):
    """'1.4.a.b.c.d.<len>.<policy...>.1.4.w.x.y.z' → (network, prefix, next_hop)。IPv4 以外は None"""
    p = [int(x) for x in idx.split('.')]
    if len(p) < 6 or p[0] != 1 or p[1] != 4:
        return None
    dest, plen = '.'.join(map(str, p[2:6])), p[6]
    rest = p[7:]
    # 方針(OID)は長さが先頭に付く。末尾の 6 個が 次ホップ種別(1).長さ(4).次ホップ
    if len(rest) < 6 or rest[-6] != 1 or rest[-5] != 4:
        return None
    nh = '.'.join(map(str, rest[-4:]))
    return dest, plen, nh


def parse_old_index(idx):
    """'a.b.c.d.<mask4>.<tos>.w.x.y.z' → (network, prefix, next_hop)"""
    p = idx.split('.')
    if len(p) != 13:
        return None
    dest, mask, nh = '.'.join(p[0:4]), '.'.join(p[4:8]), '.'.join(p[9:13])
    try:
        plen = ipaddress.ip_network(f'0.0.0.0/{mask}').prefixlen
    except ValueError:
        return None
    return dest, plen, nh


class SnmpPoller:
    def __init__(self, cfg, analytics, errors):
        src = (cfg.get('sources') or {}).get('snmp') or {}
        self.enabled = bool(src)
        self.community = src.get('community', 'public')
        self.port = int(src.get('port', 161))
        self.interval = float(src.get('interval', 60))
        self.full_every = int(src.get('full_every', 5))
        self.timeout = float(src.get('timeout', 2))
        c = src.get('cpu') or {}
        self.cpu_high, self.cpu_jump, self.cpu_hold = float(c.get('high', 80)), float(c.get('jump', 20)), int(c.get('hold', 2))
        self.devs = [d for d in cfg['devices'] if d.get('mgmt_ip')]
        self.analytics, self.errors = analytics, errors
        self.watch = RouteWatcher()
        self._n = {}             # 装置 -> 前回の経路数
        self._tick = 0
        self._ifnames = {}       # 装置 -> (取得時刻, {ifIndex: 名前})
        self.cpuw = CpuWatch(self.watch, self._fire_cpu, self.cpu_high, self.cpu_jump, self.cpu_hold)
        self._stop = threading.Event()

    def _kw(self):
        return {'port': self.port, 'timeout': self.timeout, 'retries': 1}

    def _comm(self, d):
        return d.get('snmp_community') or self.community

    def ifnames(self, d, now):
        hit = self._ifnames.get(d['id'])
        if hit and now - hit[0] < 600:
            return hit[1]
        names = {int(o.rsplit('.', 1)[1]): v for o, v in SC.walk(d['mgmt_ip'], self._comm(d), IFNAME, **self._kw())}
        self._ifnames[d['id']] = (now, names)
        return names

    def routes(self, d, now):
        """[{network, prefix, next_hop, iface, source}]"""
        host, comm = d['mgmt_ip'], self._comm(d)
        out = {}
        rows = SC.walk(host, comm, INET_ROUTE + '.9', **self._kw())          # inetCidrRouteProto
        parse, base = parse_inet_index, INET_ROUTE
        if not rows:
            rows = SC.walk(host, comm, OLD_ROUTE + '.7', **self._kw())       # ipCidrRouteProto
            parse, base = parse_old_index, OLD_ROUTE
        col_if = '.7' if base == INET_ROUTE else '.5'
        for o, v in rows:
            idx = o[len(base) + 3:]
            r = parse(idx)
            if r:
                out[idx] = {'network': r[0], 'prefix': r[1], 'next_hop': '' if r[2] == '0.0.0.0' else r[2],
                            'source': PROTO.get(v, 'other'), 'iface': ''}
        if out:
            names = self.ifnames(d, now)
            for o, v in SC.walk(host, comm, base + col_if, **self._kw()):
                idx = o[len(base) + len(col_if) + 1:]
                if idx in out:
                    out[idx]['iface'] = names.get(v, '')
        return list(out.values())

    def poll_device(self, d, now):
        host, comm = d['mgmt_ip'], self._comm(d)
        got = SC.get(host, comm, ROUTE_NUM, **self._kw())
        num = got.get(ROUTE_NUM[0], got.get(ROUTE_NUM[1]))
        new = []
        if num is not None and (self._n.get(d['id']) != num or self._tick % self.full_every == 0):
            new = self.watch.observe(d['id'], d['hostname'], self.routes(d, now), now=now)
            self._n[d['id']] = num
        cpu_rows = SC.walk(host, comm, CPU_5MIN, **self._kw())
        if cpu_rows:
            new += self.observe_cpu(d, max(v for _, v in cpu_rows), now)
        return new

    def _fire_cpu(self, dev, host, v, detail, now):
        self.analytics.fire('cpu_high', dev, dev, None, detail, now=now)

    def observe_cpu(self, d, v, now):
        """CPU の変化を「情」(経路と同じ流れ)と警告(注)にする(判定は route_watch.CpuWatch)"""
        return self.cpuw.observe(d['id'], d['hostname'], v, now=now)

    def tick(self, now=None):
        now = time.time() if now is None else now
        new = []
        for d in self.devs:
            try:
                new += self.poll_device(d, now)
                self.errors.pop(f"snmp:{d['id']}", None)
            except Exception as e:  # noqa: BLE001 - 1台取れなくても他は続ける
                self.errors[f"snmp:{d['id']}"] = str(e)[:200]
        self._tick += 1
        return new

    def start(self):
        if not self.enabled:
            return

        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.interval)
        threading.Thread(target=loop, daemon=True, name='cvs-snmp').start()

    def stop(self):
        self._stop.set()
