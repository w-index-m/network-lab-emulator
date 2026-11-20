"""
ICMP Redirect(type 5)の発生記録・ホストの経路キャッシュ・CLI

実機の流れ:
  1. ホスト H(既定ゲートウェイ R1)が宛先 D へ送る
  2. R1 の D への次ホップ R2 が、H と同じセグメントにいる(= 受けたのと同じ IF から出す)
  3. R1 はパケットを R2 へ転送しつつ、H に「D へは R2 へ直接送れ」という Redirect を返す
  4. Linux ホストは Redirect を受け入れ(accept_redirects=1)、D への経路を例外キャッシュに入れる
     (`ip route get D` に `cache <redirected>`、既定で約300秒)。以後のパケットは R2 へ直接行く
  5. ルータのその IF に `no ip redirects` があれば Redirect は送られない(ホストは R1 経由のまま)

ここでは 1〜5 を icmp_engine の経路計算(trace_path)と ping に組み込み、発生ごとに記録する。
/cyber-view は GET /api/traffic/redirects でこれを読んで描く。
"""
import ipaddress
import re
import threading
import time
from collections import deque

CACHE_TTL = 300.0           # Linux の net.ipv4.route.gc_timeout の既定(秒)
HOST_TYPES = ('pc',)         # Redirect を受け入れて経路を変えるのはホストだけ(ルータは無視する)
ROUTER_TYPES = ('cisco', 'catalyst', 'wlc', 'nexus', 'arista')

_lock = threading.Lock()
events = deque(maxlen=500)  # 発生記録(新しいものが後ろ)
seq = 0
_disabled = {}              # {router_id: {正規化IF名}}  … no ip redirects
_cache = {}                 # {(host_id, dest_ip): {'gw_ip','gw_dev','router','expires','t'}}


def _norm(ifn):
    from engine.protocols import vnet
    return vnet._norm_iface(ifn or '')


def set_disabled(router_id, iface, disabled=True):
    with _lock:
        s = _disabled.setdefault(router_id, set())
        (s.add if disabled else s.discard)(_norm(iface))


def is_disabled(router_id, iface):
    return bool(iface) and _norm(iface) in _disabled.get(router_id, set())


def restore_from_state(router_id, state):
    """保存設定の復元時: interfaces[*]['no_redirects'] から作り直す"""
    with _lock:
        _disabled.pop(router_id, None)
    for name, info in (getattr(state, 'interfaces', {}) or {}).items():
        if isinstance(info, dict) and info.get('no_redirects'):
            set_disabled(router_id, name, True)


def cached_gateway(host_id, dest_ip, now=None):
    """ホストの Redirect キャッシュ(期限内なら {'gw_ip','gw_dev',...})"""
    now = now or time.time()
    with _lock:
        c = _cache.get((host_id, dest_ip))
        if c and c['expires'] < now:
            _cache.pop((host_id, dest_ip), None)
            return None
        return dict(c) if c else None


def drop_cache(host_id=None, dest_ip=None):
    with _lock:
        for k in [k for k in _cache if (host_id is None or k[0] == host_id)
                  and (dest_ip is None or k[1] == dest_ip)]:
            _cache.pop(k, None)


def record(router, router_if, host, host_ip, dest_ip, gw_ip, gw_dev, sent, count, hostnames=None):
    """1回の Redirect(または no ip redirects で送らなかったこと)を記録する"""
    global seq
    from engine.protocols import vnet
    now = time.time()
    cached = False
    if sent and vnet.device_types.get(host) in HOST_TYPES:
        with _lock:
            _cache[(host, dest_ip)] = {'gw_ip': gw_ip, 'gw_dev': gw_dev, 'router': router,
                                       't': now, 'expires': now + CACHE_TTL}
        cached = True
    with _lock:
        seq += 1
        ev = {'seq': seq, 't': now, 'router': router, 'iface': router_if, 'host': host, 'host_ip': host_ip,
              'dest': dest_ip, 'gw_ip': gw_ip, 'gw_dev': gw_dev, 'sent': bool(sent), 'count': int(count),
              'cached': cached}
        if hostnames:
            ev.update({k + '_name': hostnames.get(ev[k], ev[k]) for k in ('router', 'host', 'gw_dev') if ev[k]})
        events.append(ev)
    return ev


def since(s=0, limit=100):
    with _lock:
        out = [e for e in events if e['seq'] > s]
    return out[-limit:]


def reset():
    global seq
    with _lock:
        events.clear()
        _cache.clear()
        _disabled.clear()
        seq = 0


# ── CLI ───────────────────────────────────────────
_RE_IF = re.compile(r'^(no\s+)?ip\s+redirects\s*$', re.I)
_RE_GET = re.compile(r'^ip\s+route\s+get\s+([\d.]+)\s*$', re.I)
_RE_FLUSH = re.compile(r'^ip\s+route\s+flush\s+cache\s*$', re.I)


def handle_cli(device_id, state, command):
    """専用コマンドだけに反応する(それ以外は None で下の層へ)。
      ルータ config-if: no ip redirects / ip redirects(記録して下の層にも流す → None)
      PC: ip route get <dest>(Redirect キャッシュがあれば cache <redirected>)/ ip route flush cache
    """
    c = (command or '').strip()
    dtype = getattr(state, 'device_type', '')
    m = _RE_IF.match(c)
    if m and dtype in ROUTER_TYPES and str(getattr(state, 'mode', '')).startswith('config-if'):
        ifn = getattr(state, 'current_if', '') or ''
        if ifn:
            off = bool(m.group(1))
            set_disabled(device_id, ifn, off)
            info = state.interfaces.setdefault(ifn, {}) if isinstance(state.interfaces, dict) else {}
            if off:
                info['no_redirects'] = True
            else:
                info.pop('no_redirects', None)
            return ''
        return None
    if dtype in HOST_TYPES:
        if _RE_FLUSH.match(c):
            drop_cache(device_id)
            return ''
        m = _RE_GET.match(c)
        if m:
            return _route_get(device_id, state, m.group(1))
    return None


def _route_get(device_id, state, dest):
    """Linux の `ip route get <dest>` の形。Redirect キャッシュがあれば実機と同じく cache <redirected>"""
    try:
        ipaddress.ip_address(dest)
    except ValueError:
        return f'Error: any valid prefix is expected rather than "{dest}".'
    src_ip, dev = '', 'eth0'
    for name, info in (state.interfaces or {}).items():
        if isinstance(info, dict) and info.get('ip') and not str(info['ip']).startswith('127.'):
            src_ip, dev = info['ip'], name
            try:
                if ipaddress.ip_address(dest) in ipaddress.ip_network(f"{info['ip']}/{info.get('prefix', 24)}", strict=False):
                    return f'{dest} dev {name} src {info["ip"]} uid 0 \n    cache '
            except ValueError:
                pass
            break
    c = cached_gateway(device_id, dest)
    if c:
        left = max(0, int(c['expires'] - time.time()))
        return f'{dest} via {c["gw_ip"]} dev {dev} src {src_ip} uid 0 \n    cache <redirected> expires {left}sec '
    gw = getattr(state, 'gateway', '') or ''
    if not gw:
        return 'RTNETLINK answers: Network is unreachable'
    return f'{dest} via {gw} dev {dev} src {src_ip} uid 0 \n    cache '
