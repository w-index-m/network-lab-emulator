"""
WORLD(地球儀)を都市の粒度にする

- GeoCity      : IP アドレス → 都市(緯度経度)。DB-IP の「IP to City Lite」(CC BY 4.0、毎月更新、キー不要)の CSV を
                 読む。列は ip_start, ip_end, continent, country, stateprov, city, latitude, longitude。
                 精度は都市単位でおおむね5〜8割(ロシア・中国はもっと粗い)。表示には必ず出典を出す
- PeerTracker  : 自分が観測した外部の相手(ダークネットに届いた送信元・NetFlow/sFlow の外の相手・SSH/Telnet の
                 ログイン試行元)を都市ごとにまとめる。行き先は自分の拠点
- DShieldClient: SANS Internet Storm Center(DShield)の公開 API。世界中のセンサーに届いた攻撃の送信元 IP の上位。
                 行き先は DShield のセンサー(場所は非公開)なので、送信元の都市だけを点で出す
- AtlasClient  : RIPE Atlas の公開 API(結果の閲覧はキー不要)。アンカー(各都市に置かれた計測用サーバー)どうしが
                 常に測っている traceroute(Anchoring Mesh)から、都市 → 都市の本物の経路と遅延を取る。
                 通過したルーターの IP を GeoCity で都市にして「どの都市を通ったか」を描く

応答の形: RIPE Atlas の traceroute 結果は ripe-atlas-sagan(RIPE-NCC 公式の解析ライブラリ)の読み方に合わせた
({prb_id, msm_id, dst_addr, result: [{hop, result: [{from, rtt} | {x: '*'}]}]})。
API が取れない環境では status に理由を書き、作り物は出さない(NETLAB_WORLD_SAMPLE=1 のときだけ見本で埋める)。
"""
import bisect
import csv
import gzip
import io
import ipaddress
import os
import pickle
import re
import threading
import time
import urllib.parse
import urllib.request
from array import array
from collections import Counter
from datetime import datetime, timezone

UA = 'network-lab-emulator (nirvana world view)'
DBIP_URL = 'https://download.db-ip.com/free/dbip-city-lite-{ym}.csv.gz'
ATTRIBUTION = 'IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0'
_NOT_PUBLIC = [ipaddress.ip_network(n) for n in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                                                  '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/3')]


def outside_ip(ip):
    """ラボ・社内の外のアドレスか(私設・CGNAT・ループバック・マルチキャストは除く。文書用の帯は見本で使うので通す)"""
    try:
        a = ipaddress.IPv4Address(normalize_ip(ip))
    except (ValueError, ipaddress.AddressValueError):
        return False
    return not any(a in n for n in _NOT_PUBLIC)


def normalize_ip(ip):
    """'061.177.172.013'(DShield は0埋め)→ '61.177.172.13'"""
    s = str(ip or '').strip()
    if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', s):
        return '.'.join(str(int(p)) for p in s.split('.'))
    return s


# ── IP → 都市 ──────────────────────────────────
class GeoCity:
    def __init__(self):
        self.starts = array('I')
        self.ends = array('I')
        self.loc = array('I')
        self.locs = []                    # [(city, region, cc, lat, lon)]
        self.status = {'state': 'empty', 'rows': 0, 'source': None}
        self._lock = threading.Lock()

    def ready(self):
        return len(self.starts) > 0

    def load_rows(self, rows, source):
        """rows: (ip_start, ip_end, continent, country, stateprov, city, lat, lon) の繰り返し(IPv6 の行は飛ばす)"""
        starts, ends, loc, locs, idx = array('I'), array('I'), array('I'), [], {}
        for r in rows:
            if len(r) < 8 or ':' in r[0]:
                continue
            try:
                s, e = int(ipaddress.IPv4Address(r[0])), int(ipaddress.IPv4Address(r[1]))
                key = (r[5], r[4], r[3], round(float(r[6]), 3), round(float(r[7]), 3))
            except (ValueError, ipaddress.AddressValueError):
                continue
            i = idx.get(key)
            if i is None:
                i = idx[key] = len(locs)
                locs.append(key)
            starts.append(s)
            ends.append(e)
            loc.append(i)
        if any(starts[k] > starts[k + 1] for k in range(len(starts) - 1)):
            order = sorted(range(len(starts)), key=starts.__getitem__)
            starts, ends, loc = (array('I', (a[k] for k in order)) for a in (starts, ends, loc))
        with self._lock:
            self.starts, self.ends, self.loc, self.locs = starts, ends, loc, locs
            self.status = {'state': 'ready', 'rows': len(starts), 'cities': len(locs), 'source': source,
                           'attribution': ATTRIBUTION}
        return len(starts)

    def load_csv(self, path):
        opener = gzip.open if open(path, 'rb').read(2) == b'\x1f\x8b' else open
        with opener(path, 'rt', encoding='utf-8', newline='') as f:
            return self.load_rows(csv.reader(f), os.path.basename(path))

    def lookup(self, ip):
        try:
            v = int(ipaddress.IPv4Address(normalize_ip(ip)))
        except (ValueError, ipaddress.AddressValueError):
            return None
        with self._lock:
            i = bisect.bisect_right(self.starts, v) - 1
            if i < 0 or v > self.ends[i]:
                return None
            city, region, cc, lat, lon = self.locs[self.loc[i]]
        return {'city': city, 'region': region, 'cc': cc, 'lat': lat, 'lon': lon}

    # 一度読んだら pickle で保存(次の起動は数秒で読める)
    def save_cache(self, path):
        with self._lock:
            blob = {'v': 1, 'starts': self.starts.tobytes(), 'ends': self.ends.tobytes(), 'loc': self.loc.tobytes(),
                    'locs': self.locs, 'source': self.status.get('source')}
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    def load_cache(self, path):
        with open(path, 'rb') as f:
            blob = pickle.load(f)       # 自分で書いたファイルだけを読む(data/world/ の下)
        starts, ends, loc = array('I'), array('I'), array('I')
        starts.frombytes(blob['starts'])
        ends.frombytes(blob['ends'])
        loc.frombytes(blob['loc'])
        with self._lock:
            self.starts, self.ends, self.loc, self.locs = starts, ends, loc, blob['locs']
            self.status = {'state': 'ready', 'rows': len(starts), 'cities': len(blob['locs']),
                           'source': blob.get('source'), 'attribution': ATTRIBUTION, 'cached': True}
        return len(starts)

    def ensure(self, cache_dir, fetch=True, now=None):
        """NETLAB_GEOIP_DB(手元の CSV)→ 保存済み → DB-IP から今月・先月の版をダウンロード、の順に試す"""
        local = os.environ.get('NETLAB_GEOIP_DB', '').strip()
        try:
            if local:
                self.status = {'state': 'parsing', 'source': os.path.basename(local)}
                self.load_csv(local)
                return self.status
            cache = os.path.join(cache_dir, 'geoip-city.pkl') if cache_dir else None
            if cache and os.path.exists(cache):
                self.load_cache(cache)
                if time.time() - os.path.getmtime(cache) < 35 * 86400 or not fetch:
                    return self.status
            if not fetch:
                if not self.ready():
                    self.status = {'state': 'off', 'detail': 'NETLAB_GEOIP_FETCH=0(都市DBを取りに行かない)'}
                return self.status
            now = now or datetime.now(timezone.utc)
            months = [now.strftime('%Y-%m'), (now.replace(day=1) - _one_day()).strftime('%Y-%m')]
            last = None
            for ym in months:
                url = DBIP_URL.format(ym=ym)
                try:
                    self.status = {'state': 'downloading', 'source': url, 'bytes': 0}
                    os.makedirs(cache_dir, exist_ok=True)
                    path = os.path.join(cache_dir, f'dbip-city-lite-{ym}.csv.gz')
                    _download(url, path, self.status)
                    self.status = {'state': 'parsing', 'source': os.path.basename(path)}
                    self.load_csv(path)
                    if cache:
                        self.save_cache(cache)
                    os.remove(path)
                    return self.status
                except Exception as e:  # noqa: BLE001
                    last = e
            if not self.ready():
                self.status = {'state': 'error', 'detail': f'都市DBを取得できません: {str(last)[:160]}'}
        except Exception as e:  # noqa: BLE001
            if not self.ready():
                self.status = {'state': 'error', 'detail': str(e)[:200]}
        return self.status


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def _download(url, path, status):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            status['bytes'] = status.get('bytes', 0) + len(b)
    os.replace(path + '.part', path)


def _by_city(items, geo, weight):
    """[(ip, info)] を都市ごとにまとめる。都市が引けないものは数だけ数える"""
    cities, unknown = {}, 0
    for ip, info in items:
        g = geo.lookup(ip) if geo.ready() else None
        if not g:
            unknown += 1
            continue
        k = (g['cc'], g['city'], g['lat'], g['lon'])
        c = cities.setdefault(k, {'city': g['city'], 'region': g['region'], 'cc': g['cc'], 'lat': g['lat'],
                                  'lon': g['lon'], 'ips': 0, 'weight': 0, 'top': [], 'kinds': Counter()})
        c['ips'] += 1
        c['weight'] += weight(info)
        c['top'].append((weight(info), ip))
        for kd, n in (info.get('kinds') or {}).items():
            c['kinds'][kd] += n
    out = []
    for c in cities.values():
        c['top'] = [ip for _, ip in sorted(c['top'], reverse=True)[:3]]
        c['kinds'] = dict(c['kinds'])
        out.append(c)
    return sorted(out, key=lambda x: -x['weight']), unknown


# ── 自分が観測した外部の相手 ─────────────────────────
class PeerTracker:
    """外部の送信元(ダークネット・フロー・ログイン試行)を IP ごとに数える。行き先は自分の拠点"""

    def __init__(self, maxn=5000):
        self.peers = {}
        self.maxn = maxn
        self._lock = threading.Lock()
        self._darknet_seq = 0

    def observe(self, ip, kind, n=1, nbytes=0, now=None):
        ip = normalize_ip(ip)
        if not outside_ip(ip):
            return False
        now = now or time.time()
        with self._lock:
            p = self.peers.get(ip)
            if p is None:
                if len(self.peers) >= self.maxn:     # 古いものから捨てる
                    old = min(self.peers, key=lambda k: self.peers[k]['last'])
                    self.peers.pop(old)
                p = self.peers[ip] = {'n': 0, 'bytes': 0, 'first': now, 'last': now, 'kinds': Counter()}
            p['n'] += n
            p['bytes'] += nbytes
            p['last'] = now
            p['kinds'][kind] += n
        return True

    def pull_darknet(self, darknet):
        """ダークネットに届いた送信元を取り込む(まだ取り込んでいない分だけ)"""
        evs = [e for e in list(darknet.events) if e['seq'] > self._darknet_seq]
        for e in evs:
            self.observe(e['src'], 'darknet', 1, e.get('len', 0) or 0, now=e['t'])
        if evs:
            self._darknet_seq = max(e['seq'] for e in evs)

    def snapshot(self, geo, window=86400, now=None):
        now = now or time.time()
        with self._lock:
            items = [(ip, dict(p, kinds=dict(p['kinds']))) for ip, p in self.peers.items() if now - p['last'] <= window]
        cities, unknown = _by_city(items, geo, lambda p: p['n'])
        for c in cities:
            c['live'] = any(now - self.peers.get(ip, {}).get('last', 0) < 120 for ip in c['top'])
        return {'cities': cities[:60], 'ips': len(items), 'unlocated': unknown}


# ── SANS ISC / DShield ─────────────────────────────
class DShieldClient:
    BASE = 'https://isc.sans.edu/api'

    def __init__(self, base=None, fetch=None, n=200):
        self.base = base or self.BASE
        self._fetch = fetch
        self.n = n

    def snapshot(self, geo):
        from .world_sources import http_json
        d = (self._fetch or http_json)(f'{self.base}/topips/records/{self.n}?json')
        rows = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), []) if isinstance(d, dict) else []
        items = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ip = normalize_ip(r.get('source') or r.get('ip') or '')
            if not outside_ip(ip):
                continue
            items.append((ip, {'reports': int(r.get('reports') or r.get('count') or r.get('attacks') or 0),
                               'targets': int(r.get('targets') or 0)}))
        out = {'status': 'ok', 'fetched': time.time(), 'ips': len(items)}
        if not geo.ready():
            out.update(cities=[], unlocated=len(items), detail='都市DBがまだ無いので都市に置けません')
            return out
        cities, unknown = _by_city(items, geo, lambda p: p['reports'])
        out.update(cities=cities[:80], unlocated=unknown)
        return out


# ── RIPE Atlas(都市 → 都市の経路) ────────────────────
DEFAULT_PAIRS = [{'from': 'New York', 'to': 'Moscow'}, {'from': 'Hong Kong', 'to': 'Los Angeles'},
                 {'from': 'Tokyo', 'to': 'Frankfurt'}]


def parse_traceroute(res):
    """RIPE Atlas の traceroute 結果1件 → hops [{hop, ip, rtt, ips, timeout}]"""
    hops = []
    for h in res.get('result') or []:
        reps = [x for x in (h.get('result') or []) if isinstance(x, dict)]
        ips = []
        for x in reps:
            if x.get('from') and x['from'] not in ips:
                ips.append(x['from'])
        rtts = [float(x['rtt']) for x in reps if isinstance(x.get('rtt'), (int, float))]
        hops.append({'hop': h.get('hop'), 'ip': ips[0] if ips else None, 'ips': ips,
                     'rtt': round(min(rtts), 2) if rtts else None, 'timeout': not ips})
    return hops


def km(a_lat, a_lon, b_lat, b_lon):
    import math
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def _same_place(a, b, within=60):
    return km(a['lat'], a['lon'], b['lat'], b['lon']) <= within


def _msm_id(v):
    if isinstance(v, int):
        return v
    m = re.search(r'(\d+)/?$', str(v or ''))
    return int(m.group(1)) if m else None


class AtlasClient:
    BASE = 'https://atlas.ripe.net/api/v2'

    def __init__(self, base=None, fetch=None):
        self.base = base or self.BASE
        self._fetch = fetch
        self.anchors = []
        self.anchors_at = 0
        self._mesh = {}                     # 宛先アンカー id -> [msm id]

    def _get(self, path, **params):
        from .world_sources import http_json
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = path if path.startswith('http') else f'{self.base}{path}'
        return (self._fetch or http_json)(url + (('&' if '?' in url else '?') + q if q else ''), timeout=30)

    def load_anchors(self, max_pages=10):
        out, url, page = [], '/anchors/', 0
        params = {'page_size': 500}
        while url and page < max_pages:
            d = self._get(url, **params)
            params = {}
            for a in d.get('results', []) if isinstance(d, dict) else d:
                g = (a.get('geometry') or {}).get('coordinates') or [None, None]
                probe = a.get('probe')
                probe = probe.get('id') if isinstance(probe, dict) else probe
                if a.get('is_disabled') or g[0] is None or not probe:
                    continue
                out.append({'id': a.get('id'), 'probe': probe, 'fqdn': a.get('fqdn'), 'city': a.get('city') or '',
                            'cc': a.get('country') or a.get('country_code') or '', 'lat': g[1], 'lon': g[0],
                            'ip': a.get('ip_v4')})
            url = d.get('next') if isinstance(d, dict) else None
            page += 1
        self.anchors, self.anchors_at = out, time.time()
        return out

    def find_anchor(self, spec):
        """'New York' / 'Moscow' / 'RU' / 'nl-ams-as3333' のどれでも。都市名は部分一致、国コードは完全一致"""
        s = str(spec or '').strip().lower()
        if not s:
            return None
        for test in (lambda a: a['city'].lower() == s, lambda a: s in a['city'].lower(),
                     lambda a: a['cc'].lower() == s, lambda a: s in (a['fqdn'] or '').lower()):
            hit = [a for a in self.anchors if test(a)]
            if hit:
                return sorted(hit, key=lambda a: a['id'] or 0)[0]
        return None

    def mesh_measurements(self, target):
        if target['id'] in self._mesh:
            return self._mesh[target['id']]
        ids = []
        try:                                # アンカーごとの計測一覧(Anchoring Mesh の traceroute)
            d = self._get('/anchor-measurements/', target=target['id'], page_size=100)
            for m in d.get('results', []) if isinstance(d, dict) else []:
                if m.get('is_mesh') is False:
                    continue
                if m.get('type') and m['type'] != 'traceroute':
                    continue
                mid = _msm_id(m.get('measurement') or m.get('measurement_id'))
                if mid:
                    ids.append(mid)
        except Exception:  # noqa: BLE001 - 一覧の形が違えば計測の検索で探す
            pass
        if not ids and target.get('fqdn'):
            d = self._get('/measurements/', target=target['fqdn'], type='traceroute', status=2, page_size=50)
            for m in d.get('results', []) if isinstance(d, dict) else []:
                if 'mesh' in str(m.get('description', '')).lower() or 'anchoring' in str(m.get('tags', '')).lower():
                    ids.append(m.get('id'))
        self._mesh[target['id']] = ids[:8]
        return self._mesh[target['id']]

    def path(self, src, dst, geo):
        for mid in self.mesh_measurements(dst):
            rows = self._get(f'/measurements/{mid}/latest/', probe_ids=src['probe'])
            rows = rows if isinstance(rows, list) else (rows or {}).get('results', [])
            for r in rows:
                if r.get('type', 'traceroute') != 'traceroute' or r.get('af', 4) != 4:
                    continue
                if r.get('prb_id') not in (None, src['probe']):
                    continue
                hops = parse_traceroute(r)
                return self._describe(src, dst, mid, r, hops, geo)
        return None

    @staticmethod
    def _describe(src, dst, mid, r, hops, geo):
        for h in hops:
            g = geo.lookup(h['ip']) if (h['ip'] and geo.ready() and outside_ip(h['ip'])) else None
            h['geo'] = g
        # 通過した都市(同じ都市が続くものは1つに)。両端はアンカーの本当の場所
        cities = [{'city': src['city'], 'cc': src['cc'], 'lat': src['lat'], 'lon': src['lon'], 'hops': [0], 'rtt': 0, 'end': 'src'}]
        for h in hops:
            g = h.get('geo')
            if not g or not g['city']:
                continue
            last = cities[-1]
            if _same_place(g, last):
                last['hops'].append(h['hop'])
                if h['rtt'] is not None:
                    last['rtt'] = h['rtt']
                continue
            cities.append({'city': g['city'], 'cc': g['cc'], 'lat': g['lat'], 'lon': g['lon'], 'hops': [h['hop']], 'rtt': h['rtt']})
        last_rtt = next((h['rtt'] for h in reversed(hops) if h['rtt'] is not None), None)
        reached = any(r.get('dst_addr') and r.get('dst_addr') in h['ips'] for h in hops)
        if len(cities) > 1 and _same_place(cities[-1], dst):
            cities[-1].update(end='dst', city=dst['city'], lat=dst['lat'], lon=dst['lon'])
        else:
            cities.append({'city': dst['city'], 'cc': dst['cc'], 'lat': dst['lat'], 'lon': dst['lon'], 'hops': [],
                           'rtt': last_rtt, 'end': 'dst'})
        return {'from': src, 'to': dst, 'msm_id': mid, 'timestamp': r.get('timestamp'), 'dst_addr': r.get('dst_addr'),
                'hops': [{k: h[k] for k in ('hop', 'ip', 'rtt', 'timeout')} | {'city': (h['geo'] or {}).get('city'),
                          'cc': (h['geo'] or {}).get('cc')} for h in hops],
                'cities': cities, 'rtt': last_rtt, 'reached': reached, 'hop_count': len(hops),
                'url': f'https://atlas.ripe.net/measurements/{mid}/'}

    def snapshot(self, pairs, geo):
        if not self.anchors or time.time() - self.anchors_at > 86400:
            self.load_anchors()
        out = {'status': 'ok', 'fetched': time.time(), 'anchors': len(self.anchors), 'paths': []}
        for p in pairs:
            src, dst = self.find_anchor(p.get('from')), self.find_anchor(p.get('to'))
            item = {'pair': p}
            if not src or not dst:
                item['error'] = f"アンカーが見つかりません: {p.get('from') if not src else p.get('to')}"
            else:
                try:
                    got = self.path(src, dst, geo)
                    item.update(got or {'from': src, 'to': dst, 'error': '経路の計測結果がまだありません'})
                except Exception as e:  # noqa: BLE001
                    item.update({'from': src, 'to': dst, 'error': str(e)[:160]})
            out['paths'].append(item)
        return out

    def anchor_list(self, q='', limit=50):
        q = q.lower()
        hit = [a for a in self.anchors if not q or q in a['city'].lower() or q == a['cc'].lower()]
        return [{'city': a['city'], 'cc': a['cc'], 'fqdn': a['fqdn']} for a in hit[:limit]]


def load_sample_geo(geo):
    """見本の都市DB(見本の DShield・RIPE Atlas の IP にだけ当たる小さな表)"""
    from . import world_samples as ws
    geo.load_rows(csv.reader(io.StringIO(ws.GEO_CSV)), 'sample')
    geo.status['state'] = 'sample'
    return geo
