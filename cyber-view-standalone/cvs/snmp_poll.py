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
SNMPv3 なら v3: {user, auth: sha, auth_password, priv: aes, priv_password}(装置ごとは devices[].snmp_v3)。
v3 を書いた装置には community を使わない。パスワードは ${ENV} で渡す。
SNMP に応答しない装置(監視用の PC など)は devices[].snmp: false で問い合わせから外す。

流量(任意。Prometheus / Zabbix が無い環境向け):
  traffic: true                  # または traffic: {interval: 10}(秒。既定 10)
  IF-MIB の ifHCIn/OutOctets(64bit。無い機種は 32bit の ifIn/OutOctets)と ifOperStatus を直接読み、
  前回との差から bps を出す。リンクの流量・LINK DOWN の検知経路「POLL」・応答なし(2回続けて無応答)に使う。
  取得元は画面で「snmp」と出る(Prometheus → Zabbix → snmp → MRTG/Cacti の順に使う)。
"""
import ipaddress
import threading
import time

from . import snmp_client as SC
from . import snmp_ospf
from .link_evidence import norm_if
from .route_watch import CpuWatch, RouteWatcher

INET_ROUTE = '1.3.6.1.2.1.4.24.7.1'
OLD_ROUTE = '1.3.6.1.2.1.4.24.4.1'
ROUTE_NUM = ['1.3.6.1.2.1.4.24.6.0', '1.3.6.1.2.1.4.24.3.0']
RFC1213_ROUTE = '1.3.6.1.2.1.4.21.1'      # ipRouteTable(古い機種。宛先ごとに1行、経路数の OID は無い)
HR_CPU = '1.3.6.1.2.1.25.3.3.1.2'         # HOST-RESOURCES-MIB hrProcessorLoad(Cisco 以外の多くの機種)
IFNAME = '1.3.6.1.2.1.31.1.1.1.1'
IF_HC_IN, IF_HC_OUT = '1.3.6.1.2.1.31.1.1.1.6', '1.3.6.1.2.1.31.1.1.1.10'      # ifHCInOctets / ifHCOutOctets(64bit)
IF_IN, IF_OUT = '1.3.6.1.2.1.2.2.1.10', '1.3.6.1.2.1.2.2.1.16'                # ifInOctets / ifOutOctets(32bit)
IF_OPER = '1.3.6.1.2.1.2.2.1.8'                                               # ifOperStatus
LOST_AFTER = 2           # 流量の取得に続けて失敗した回数(これで「応答なし」にする)
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
    HIST_MAX = 400           # IF ごとに持つ積算カウンタの履歴数(10 秒間隔なら約 1 時間ぶん)

    def __init__(self, cfg, analytics, errors):
        src = (cfg.get('sources') or {}).get('snmp') or {}
        self.enabled = bool(src)
        self.community = src.get('community', 'public')
        self.v3 = src.get('v3') or None
        self.port = int(src.get('port', 161))
        self.interval = float(src.get('interval', 60))
        self.full_every = int(src.get('full_every', 5))
        self.timeout = float(src.get('timeout', 2))
        c = src.get('cpu') or {}
        self.cpu_high, self.cpu_jump, self.cpu_hold = float(c.get('high', 80)), float(c.get('jump', 20)), int(c.get('hold', 2))
        self.cpu_oid = c.get('oid')
        self.cpu_now = {}        # 装置 -> (CPU 使用率, 取った時刻)。画面の表示用
        self.ospf_now = {}       # 装置 -> snmp_ospf.read() の結果(OSPF 画面用)
        self.devs = [d for d in cfg['devices'] if d.get('mgmt_ip') and d.get('snmp') is not False]
        t = src.get('traffic')
        self.traffic_on = bool(t)
        self.traffic_interval = float(t.get('interval', 10)) if isinstance(t, dict) else 10.0
        self._acc = {}           # (装置, ifIndex) -> {'last': (in, out, 32bit か), 'tin', 'tout': 積算バイト, 'hist': [(時刻, tin, tout)]}
        self._keys = {}          # (装置, ifIndex) -> 正規化IF名
        self._oper = {}          # (装置, 正規化IF名) -> 'up' | 'down'
        self._counters = {}      # (装置, 正規化IF名) -> (in_bytes, out_bytes)
        self._fails = {}         # 装置 -> 続けて失敗した回数
        self._tlock = threading.Lock()
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
        v3 = d.get('snmp_v3') or (None if d.get('snmp_community') else self.v3)
        if v3:
            return SC.v3_user(v3)
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
        if not rows:
            return self._routes_rfc1213(d, host, comm, now)
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

    def _routes_rfc1213(self, d, host, comm, now):
        cols = {}
        for col in ('7', '9', '11', '2'):                     # 次ホップ・プロトコル・マスク・ifIndex
            for o, v in SC.walk(host, comm, f'{RFC1213_ROUTE}.{col}', **self._kw()):
                cols.setdefault(o[len(RFC1213_ROUTE) + len(col) + 2:], {})[col] = v
        if not cols:
            return []
        names = self.ifnames(d, now)
        out = []
        for dest, c in cols.items():
            try:
                plen = ipaddress.ip_network(f"0.0.0.0/{c.get('11', '255.255.255.255')}").prefixlen
            except ValueError:
                continue
            nh = c.get('7', '')
            out.append({'network': dest, 'prefix': plen, 'next_hop': '' if nh in ('', '0.0.0.0') else nh,
                        'source': PROTO.get(c.get('9'), 'other'), 'iface': names.get(c.get('2'), '')})
        return out

    def cpu(self, d, host, comm):
        """CPU 使用率。devices[].snmp_cpu_oid / sources.snmp.cpu.oid → Cisco の cpmCPUTotal5minRev → hrProcessorLoad の平均"""
        oid = d.get('snmp_cpu_oid') or self.cpu_oid
        for root, agg in ([(oid, max)] if oid else []) + [(CPU_5MIN, max), (HR_CPU, lambda xs: sum(xs) / len(xs))]:
            rows = SC.walk(host, comm, root, **self._kw())
            vals = [v for _, v in rows if isinstance(v, int)]
            if vals:
                return agg(vals)
        return None

    def poll_device(self, d, now):
        host, comm = d['mgmt_ip'], self._comm(d)
        got = SC.get(host, comm, ROUTE_NUM, **self._kw())
        num = got.get(ROUTE_NUM[0], got.get(ROUTE_NUM[1]))
        new = []
        # 経路数の OID が無い機種(RFC1213 のみ)は full_every 回ごとに表全体を見る
        if (num is not None and self._n.get(d['id']) != num) or self._tick % self.full_every == 0:
            new = self.watch.observe(d['id'], d['hostname'], self.routes(d, now), now=now)
            self._n[d['id']] = num
        try:                    # OSPF の隣接(OSPF 画面用)。OSPF-MIB が無い・OSPF が無い装置は None
            self.ospf_now[d['id']] = snmp_ospf.read(host, comm, **self._kw())
        except Exception:  # noqa: BLE001
            self.ospf_now[d['id']] = None
        v = self.cpu(d, host, comm)
        if v is not None:
            self.cpu_now[d['id']] = (v, now)
            new += self.observe_cpu(d, v, now)
        return new

    def cpu_of(self, dev, now=None, max_age=None):
        """最後に取れた CPU 使用率(古すぎる・取れていなければ None)"""
        got = self.cpu_now.get(dev)
        if not got:
            return None
        age = (now or time.time()) - got[1]
        return got[0] if age <= (max_age or self.interval * 3) else None

    def _fire_cpu(self, dev, host, v, detail, now):
        self.analytics.fire('cpu_high', dev, dev, None, detail, now=now)

    def observe_cpu(self, d, v, now):
        """CPU の変化を「情」(経路と同じ流れ)と警告(注)にする(判定は route_watch.CpuWatch)"""
        return self.cpuw.observe(d['id'], d['hostname'], v, now=now)

    # ── 流量(IF-MIB のカウンタを直接読む) ──
    def _counters_of(self, d):
        """({ifIndex: (in, out)}, 32bit か)。64bit(ifHC*)を先に、無ければ 32bit"""
        host, comm = d['mgmt_ip'], self._comm(d)
        for cin, cout, is32 in ((IF_HC_IN, IF_HC_OUT, False), (IF_IN, IF_OUT, True)):
            ins = {int(o.rsplit('.', 1)[1]): v for o, v in SC.walk(host, comm, cin, **self._kw()) if isinstance(v, int)}
            if not ins:
                continue
            outs = {int(o.rsplit('.', 1)[1]): v for o, v in SC.walk(host, comm, cout, **self._kw()) if isinstance(v, int)}
            return {i: (v, outs.get(i)) for i, v in ins.items()}, is32
        return {}, False

    def traffic_device(self, d, now):
        """1台ぶんの流量・状態を更新する。取れなければ例外(呼び出し側で数える)"""
        cnt, is32 = self._counters_of(d)
        if not cnt:
            raise SC.SnmpError('IF-MIB のカウンタが取れません(応答なし、または ifTable が空)')
        names = self.ifnames(d, now)
        oper = {}
        for o, v in SC.walk(d['mgmt_ip'], self._comm(d), IF_OPER, **self._kw()):
            idx = int(o.rsplit('.', 1)[1])
            if v == 1:
                oper[idx] = 'up'
            elif v in (2, 7):                      # Prometheus 版と同じ(それ以外は判定に使わない)
                oper[idx] = 'down'
        ops, byts = {}, {}
        for idx, (cin, cout) in cnt.items():
            nm = names.get(idx)
            if not nm:
                continue
            k = norm_if(nm)
            byts[(d['id'], k)] = (cin, cout or 0)
            if idx in oper:
                ops[(d['id'], k)] = oper[idx]
            with self._tlock:                  # 画面側(traffic_snapshot)が別スレッドで読むので、更新はロックの中で
                self._accumulate((d['id'], idx), cin, cout, is32, now)
                self._keys[(d['id'], idx)] = k
        with self._tlock:
            self._oper.update(ops)
            self._counters.update(byts)

    def _accumulate(self, key, cin, cout, is32, now):
        """カウンタの差を積算していく。32bit は折り返しを補正、64bit が減ったのは再起動なのでその回は捨てる
        (積算値は減らないので、あとで窓の両端の差を取れば再起動をまたいでも正しい)"""
        a = self._acc.get(key)
        if a is None or a['last'][2] != is32:
            a = self._acc[key] = {'last': (cin, cout, is32), 'tin': 0, 'tout': 0, 'hist': []}
        else:
            wrap = 2 ** 32 if is32 else 2 ** 64

            def delta(cur, old):
                if cur is None or old is None:
                    return 0
                if cur >= old:
                    return cur - old
                return cur + wrap - old if is32 else 0
            a['tin'] += delta(cin, a['last'][0])
            a['tout'] += delta(cout, a['last'][1])
            a['last'] = (cin, cout, is32)
        a['hist'].append((now, a['tin'], a['tout']))
        if len(a['hist']) > self.HIST_MAX:
            del a['hist'][:len(a['hist']) - self.HIST_MAX]

    def _rate_of(self, key, window):
        """window 秒ぶんの平均 bps(Prometheus の rate(...[window]) と同じ考え方)。2 点が無ければ None"""
        h = self._acc.get(key, {}).get('hist') or []
        if len(h) < 2:
            return None
        last = h[-1]
        pts = [x for x in h if x[0] >= last[0] - window]
        if len(pts) < 2:                       # window が取得間隔より短いときは直近の 2 点
            pts = h[-2:]
        dt = pts[-1][0] - pts[0][0]
        if dt <= 0:
            return None
        return (pts[-1][1] - pts[0][1]) * 8 / dt, (pts[-1][2] - pts[0][2]) * 8 / dt

    def traffic_tick(self, now=None):
        now = time.time() if now is None else now
        for d in self.devs:
            key = f"snmp-traffic:{d['id']}"
            try:
                self.traffic_device(d, now)
                self._fails[d['id']] = 0
                self.errors.pop(key, None)
            except Exception as e:  # noqa: BLE001 - 1台取れなくても他は続ける
                self._fails[d['id']] = self._fails.get(d['id'], 0) + 1
                self.errors[key] = str(e)[:200]

    def traffic_snapshot(self, window=60):
        """画面用: {'rates': {(装置, IF): (in_bps, out_bps)}, 'oper': ..., 'counters': ..., 'lost': {装置: mgmt_ip}}
        rates は直近 window 秒の平均(ping のような一瞬の通信が 0 と山を行き来しないように)"""
        with self._tlock:
            lost = {d['id']: d['mgmt_ip'] for d in self.devs if self._fails.get(d['id'], 0) >= LOST_AFTER}
            rates = {}
            for key, nm in self._keys.items():
                if key[0] in lost:             # 応答なしの装置の古い値は出さない
                    continue
                r = self._rate_of(key, window)
                if r is not None:
                    rates[(key[0], nm)] = r
            # 応答なしの装置の古い値は出さない
            keep = lambda m: {k: v for k, v in m.items() if k[0] not in lost}  # noqa: E731
            return {'rates': rates, 'oper': keep(self._oper), 'counters': keep(self._counters), 'lost': lost}

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
        if self.traffic_on:
            def tloop():
                while not self._stop.is_set():
                    self.traffic_tick()
                    self._stop.wait(self.traffic_interval)
            threading.Thread(target=tloop, daemon=True, name='cvs-snmp-traffic').start()

    def stop(self):
        self._stop.set()
