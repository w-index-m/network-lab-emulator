"""
/cyber-view の WORLD(地球儀)に載せる外部データ

- CloudRanges   : AWS / Google Cloud が公開している IP アドレス帯の一覧(リージョン付き)で、
                  自分の通信の宛先が「どのクラウドのどのリージョンか」を引く。有料の GeoIP は使わない
- RadarClient   : Cloudflare Radar API(要 API トークン、CLOUDFLARE_API_TOKEN)。国ごとの HTTP 流量の割合、
                  L7 攻撃の送信元国→標的国、通信の異常(障害)
- IhrClient     : IIJ 研究所の Internet Health Report API(認証不要)。AS の遅延の異常、障害(disco)

応答の形は公式の定義に合わせた:
  Radar  … github.com/cloudflare/api-schemas の openapi.json(/radar/http/top/locations、
           /radar/attacks/layer7/top/attacks、/radar/traffic_anomalies)
  IHR    … github.com/InternetHealthReport/ihr-django(serializers.py / views.py。DRF のページ形式
           {count, next, results}、network_delay/alarms は timebin__gte が必須)
外部へ出られない環境では status に理由が入り、何も描かない(作り物は出さない)。
"""
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = 'network-lab-emulator (nirvana world view)'

# ── クラウドのリージョンの場所(公開されている所在地の都市。表示用の近似) ──
AWS_REGIONS = {
    'us-east-1': ('バージニア北部', 38.9, -77.4), 'us-east-2': ('オハイオ', 40.0, -83.0),
    'us-west-1': ('北カリフォルニア', 37.4, -121.9), 'us-west-2': ('オレゴン', 45.6, -121.2),
    'ca-central-1': ('カナダ中部', 45.5, -73.6), 'ca-west-1': ('カルガリー', 51.0, -114.1),
    'mx-central-1': ('メキシコ', 20.6, -100.4), 'sa-east-1': ('サンパウロ', -23.5, -46.6),
    'eu-west-1': ('アイルランド', 53.3, -6.3), 'eu-west-2': ('ロンドン', 51.5, -0.1), 'eu-west-3': ('パリ', 48.9, 2.35),
    'eu-central-1': ('フランクフルト', 50.1, 8.7), 'eu-central-2': ('チューリッヒ', 47.4, 8.5),
    'eu-north-1': ('ストックホルム', 59.3, 18.1), 'eu-south-1': ('ミラノ', 45.5, 9.2), 'eu-south-2': ('スペイン', 41.6, -0.9),
    'il-central-1': ('テルアビブ', 32.1, 34.8), 'me-south-1': ('バーレーン', 26.1, 50.6), 'me-central-1': ('UAE', 25.2, 55.3),
    'af-south-1': ('ケープタウン', -33.9, 18.4), 'ap-east-1': ('香港', 22.3, 114.2), 'ap-east-2': ('台北', 25.0, 121.5),
    'ap-south-1': ('ムンバイ', 19.1, 72.9), 'ap-south-2': ('ハイデラバード', 17.4, 78.5),
    'ap-northeast-1': ('東京', 35.7, 139.7), 'ap-northeast-2': ('ソウル', 37.6, 127.0), 'ap-northeast-3': ('大阪', 34.7, 135.5),
    'ap-southeast-1': ('シンガポール', 1.35, 103.8), 'ap-southeast-2': ('シドニー', -33.9, 151.2),
    'ap-southeast-3': ('ジャカルタ', -6.2, 106.8), 'ap-southeast-4': ('メルボルン', -37.8, 145.0),
    'ap-southeast-5': ('マレーシア', 3.1, 101.7), 'ap-southeast-7': ('タイ', 13.7, 100.5),
}
GCP_REGIONS = {
    'asia-northeast1': ('東京', 35.68, 139.69), 'asia-northeast2': ('大阪', 34.69, 135.5), 'asia-northeast3': ('ソウル', 37.57, 126.98),
    'asia-east1': ('台湾', 24.1, 120.7), 'asia-east2': ('香港', 22.3, 114.2), 'asia-south1': ('ムンバイ', 19.1, 72.9),
    'asia-south2': ('デリー', 28.6, 77.2), 'asia-southeast1': ('シンガポール', 1.35, 103.8), 'asia-southeast2': ('ジャカルタ', -6.2, 106.8),
    'australia-southeast1': ('シドニー', -33.9, 151.2), 'australia-southeast2': ('メルボルン', -37.8, 145.0),
    'europe-west1': ('ベルギー', 50.4, 3.8), 'europe-west2': ('ロンドン', 51.5, -0.1), 'europe-west3': ('フランクフルト', 50.1, 8.7),
    'europe-west4': ('オランダ', 53.4, 6.8), 'europe-west6': ('チューリッヒ', 47.4, 8.5), 'europe-west8': ('ミラノ', 45.5, 9.2),
    'europe-west9': ('パリ', 48.9, 2.35), 'europe-west10': ('ベルリン', 52.5, 13.4), 'europe-west12': ('トリノ', 45.1, 7.7),
    'europe-north1': ('フィンランド', 60.6, 27.2), 'europe-central2': ('ワルシャワ', 52.2, 21.0), 'europe-southwest1': ('マドリード', 40.4, -3.7),
    'me-west1': ('テルアビブ', 32.1, 34.8), 'me-central1': ('ドーハ', 25.3, 51.5), 'me-central2': ('ダンマーム', 26.4, 50.1),
    'africa-south1': ('ヨハネスブルグ', -26.2, 28.0), 'northamerica-northeast1': ('モントリオール', 45.5, -73.6),
    'northamerica-northeast2': ('トロント', 43.7, -79.4), 'southamerica-east1': ('サンパウロ', -23.5, -46.6),
    'southamerica-west1': ('サンティアゴ', -33.4, -70.6), 'us-central1': ('アイオワ', 41.3, -95.9), 'us-east1': ('サウスカロライナ', 33.2, -80.0),
    'us-east4': ('バージニア北部', 38.9, -77.4), 'us-east5': ('コロンバス', 39.96, -83.0), 'us-south1': ('ダラス', 32.8, -96.8),
    'us-west1': ('オレゴン', 45.6, -121.2), 'us-west2': ('ロサンゼルス', 34.05, -118.24), 'us-west3': ('ソルトレイクシティ', 40.76, -111.9),
    'us-west4': ('ラスベガス', 36.17, -115.14),
}
SOURCES = {
    'aws': 'https://ip-ranges.amazonaws.com/ip-ranges.json',
    'gcp': 'https://www.gstatic.com/ipranges/cloud.json',
}


def http_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


class CloudRanges:
    """クラウドの IPv4 アドレス帯 → (provider, region, service)。一番細かい帯(最長一致)を返す"""

    def __init__(self):
        self.by_len = {}            # prefixlen -> {network int: (provider, region, service)}
        self.lens = []
        self.loaded = {}            # provider -> {'count', 'created', 'error'}

    def _add(self, provider, prefix, region, service):
        try:
            n = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            return
        if n.version != 4:
            return
        tbl = self.by_len.setdefault(n.prefixlen, {})
        key = int(n.network_address)
        old = tbl.get(key)
        # 同じ帯が AMAZON と EC2 等に重複して載っている。サービス名の具体的なほうを残す
        if old is None or old[2] in ('AMAZON', 'Google Cloud', ''):
            tbl[key] = (provider, region, service)

    def load_aws(self, d):
        for p in d.get('prefixes', []):
            self._add('aws', p.get('ip_prefix', ''), p.get('region', ''), p.get('service', ''))
        self.loaded['aws'] = {'count': len(d.get('prefixes', [])), 'created': d.get('createDate'), 'error': None}
        self.lens = sorted(self.by_len, reverse=True)

    def load_gcp(self, d):
        n = 0
        for p in d.get('prefixes', []):
            if p.get('ipv4Prefix'):
                self._add('gcp', p['ipv4Prefix'], p.get('scope', ''), p.get('service', 'Google Cloud'))
                n += 1
        self.loaded['gcp'] = {'count': n, 'created': d.get('creationTime'), 'error': None}
        self.lens = sorted(self.by_len, reverse=True)

    def lookup(self, ip):
        try:
            v = int(ipaddress.IPv4Address(ip))
        except (ValueError, ipaddress.AddressValueError):
            return None
        for plen in self.lens:
            key = v & ((0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF) if plen else 0
            hit = self.by_len[plen].get(key)
            if hit:
                return hit
        return None

    def fetch(self, providers=('aws', 'gcp'), cache_dir=None):
        for p in providers:
            try:
                d = http_json(SOURCES[p], timeout=30)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(os.path.join(cache_dir, f'{p}-ranges.json'), 'w', encoding='utf-8') as f:
                        json.dump(d, f)
            except Exception as e:  # noqa: BLE001 - 取れなければ前回の保存分を使う
                d = None
                cached = os.path.join(cache_dir or '', f'{p}-ranges.json')
                if cache_dir and os.path.exists(cached):
                    d = json.load(open(cached, encoding='utf-8'))
                if d is None:
                    self.loaded[p] = {'count': 0, 'created': None, 'error': str(e)[:160]}
                    continue
            (self.load_aws if p == 'aws' else self.load_gcp)(d)
        return self.loaded


def region_place(provider, region):
    t = (AWS_REGIONS if provider == 'aws' else GCP_REGIONS).get(region)
    return {'name': t[0], 'lat': t[1], 'lon': t[2]} if t else None


# ── Cloudflare Radar ─────────────────────────────
class RadarClient:
    BASE = 'https://api.cloudflare.com/client/v4/radar'

    def __init__(self, token=None, base=None, fetch=None):
        self.token = token if token is not None else os.environ.get('CLOUDFLARE_API_TOKEN', '').strip()
        self.base = base or self.BASE
        self._fetch = fetch or http_json

    def _get(self, path, **params):
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        d = self._fetch(f'{self.base}{path}?{q}', headers={'Authorization': f'Bearer {self.token}'})
        if isinstance(d, dict) and d.get('success') is False:
            raise RuntimeError('; '.join(str(e.get('message', e)) for e in d.get('errors', []))[:200] or 'Radar error')
        return d.get('result', d)

    def snapshot(self, date_range='1d', limit=20):
        if not self.token:
            return {'status': 'no_token', 'detail': 'CLOUDFLARE_API_TOKEN が未設定(Radar は無料の API トークンが必要)'}
        out = {'status': 'ok', 'date_range': date_range, 'fetched': time.time()}
        loc = self._get('/http/top/locations', dateRange=date_range, limit=limit, format='json')
        out['locations'] = [{'cc': x.get('clientCountryAlpha2'), 'name': x.get('clientCountryName'),
                             'share': float(x.get('value', 0) or 0)} for x in loc.get('top_0', [])]
        atk = self._get('/attacks/layer7/top/attacks', dateRange=date_range, limit=limit, format='json')
        out['attacks'] = [{'from': x.get('originCountryAlpha2'), 'from_name': x.get('originCountryName'),
                           'to': x.get('targetCountryAlpha2'), 'to_name': x.get('targetCountryName'),
                           'share': float(x.get('value', 0) or 0)} for x in atk.get('top_0', [])]
        # L3(DDoS): 送信元の国・標的の国(top/attacks は送信元だけを返す版があるので、標的があれば組にする)
        try:
            l3 = self._get('/attacks/layer3/top/attacks', dateRange=date_range, limit=limit, format='json')
            org = self._get('/attacks/layer3/top/locations/origin', dateRange=date_range, limit=limit, format='json')
            tgt = self._get('/attacks/layer3/top/locations/target', dateRange=date_range, limit=limit, format='json')
            out['l3'] = {
                'pairs': [{'from': x.get('originCountryAlpha2'), 'to': x.get('targetCountryAlpha2'),
                           'share': float(x.get('value', 0) or 0)} for x in l3.get('top_0', []) if x.get('targetCountryAlpha2')],
                'origins': [{'cc': x.get('originCountryAlpha2'), 'name': x.get('originCountryName'),
                             'share': float(x.get('value', 0) or 0)} for x in org.get('top_0', [])],
                'targets': [{'cc': x.get('targetCountryAlpha2'), 'name': x.get('targetCountryName'),
                             'share': float(x.get('value', 0) or 0)} for x in tgt.get('top_0', [])]}
        except Exception as e:  # noqa: BLE001 - L3 だけ取れないことはある
            out['l3'] = {'pairs': [], 'origins': [], 'targets': [], 'error': str(e)[:160]}
        try:
            an = self._get('/traffic_anomalies', dateRange='7d', limit=10, format='json')
            out['anomalies'] = [{'cc': (x.get('locationDetails') or {}).get('code')
                                 or ((x.get('asnDetails') or {}).get('location') or {}).get('code'),
                                 'asn': (x.get('asnDetails') or {}).get('asn'), 'name': (x.get('asnDetails') or {}).get('name'),
                                 'type': x.get('type'), 'status': x.get('status'), 'start': x.get('startDate'),
                                 'end': x.get('endDate')} for x in an.get('trafficAnomalies', [])]
        except Exception as e:  # noqa: BLE001 - 異常の一覧だけ取れないことはある(権限など)
            out['anomalies'], out['anomalies_error'] = [], str(e)[:160]
        return out


# ── IIJ Internet Health Report ───────────────────
JP_ASNS = {2497: 'IIJ', 2914: 'NTT Communications', 4713: 'OCN', 17676: 'SoftBank', 2516: 'KDDI', 2518: 'BIGLOBE',
           7500: 'WIDE', 2907: 'SINET'}


class IhrClient:
    # 2026 年に ihr.iijlab.net から www.ihr.live へ移転(旧 URL は 301)。新しい API は知らない項目
    # (format など)を 400 invalid_query_params で断り、使える項目の一覧(allowed)を返す
    BASE = 'https://www.ihr.live/ihr/api'

    def __init__(self, base=None, fetch=None, asns=None):
        self.base = base or self.BASE
        self._fetch = fetch or http_json
        self.asns = asns or JP_ASNS

    def _get(self, path, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for _ in range(2):
            try:
                d = self._fetch(f'{self.base}{path}?{urllib.parse.urlencode(params)}')
            except urllib.error.HTTPError as e:
                # 400 invalid_query_params なら、断られた項目を外して1回だけやり直す(API の項目が変わっても止まらない)
                try:
                    body = json.loads(e.read().decode('utf-8', 'replace'))
                except Exception:  # noqa: BLE001
                    raise e from None
                bad = [k for k in body.get('unexpected') or [] if k in params] if isinstance(body, dict) else []
                if e.code != 400 or not bad:
                    raise RuntimeError(f'HTTP {e.code}: {body}') from None
                for k in bad:
                    params.pop(k)
                continue
            return d.get('results', []) if isinstance(d, dict) else d
        return []

    def snapshot(self, hours=24, now=None):
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%S')
        out = {'status': 'ok', 'fetched': time.time(), 'hours': hours}
        rows = self._get('/network_delay/alarms/', timebin__gte=since, startpoint_type='AS',
                         startpoint_name='|'.join(str(a) for a in self.asns), ordering='-timebin')
        out['delay_alarms'] = [{'t': r.get('timebin'), 'asn': r.get('startpoint_name'),
                                'as_name': self.asns.get(int(r['startpoint_name'])) if str(r.get('startpoint_name', '')).isdigit() else None,
                                'to': f"{r.get('endpoint_type', '')}{r.get('endpoint_name', '')}",
                                'deviation': float(r.get('deviation') or 0)} for r in rows][:50]
        ev = self._get('/disco/events/', starttime__gte=since, streamname='JP', ordering='-starttime')
        ev += [e for a in self.asns for e in self._get('/disco/events/', starttime__gte=since, streamname=str(a),
                                                       ordering='-starttime')][:20]
        seen, uniq = set(), []
        for e in ev:                      # 国(JP)と AS の両方で引くので、同じ事象を1つにまとめる
            k = (e.get('streamname'), e.get('starttime'))
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        ev = uniq
        out['outages'] = [{'stream': e.get('streamname'), 'type': e.get('streamtype'), 'start': e.get('starttime'),
                           'end': e.get('endtime'), 'level': float(e.get('avglevel') or 0),
                           'probes': e.get('nbdiscoprobes'), 'total': e.get('totalprobes'), 'ongoing': e.get('ongoing')}
                          for e in ev][:30]
        return out


# ── まとめて持つ(画面はこれを読む) ──────────────────
class WorldState:
    REFRESH = 600

    def __init__(self):
        self.cloud = CloudRanges()
        self.radar = {'status': 'off'}
        self.ihr = {'status': 'off'}
        self._lock = threading.Lock()
        self._last = 0
        self.cloud_seen = {}     # (provider, region) -> {'bytes', 'flows', 'last', 'service'}
        from .world_geo import DEFAULT_PAIRS, AtlasClient, GeoCity, PeerTracker
        self.geo = GeoCity()
        self.peers = PeerTracker()
        self.dshield = {'status': 'off'}
        self.atlas = {'status': 'off', 'paths': []}
        self.atlas_client = AtlasClient()
        self.pairs = [dict(p) for p in DEFAULT_PAIRS]
        self._geo_thread = None

    def geo_fetch_enabled(self):
        return self.enabled() and os.environ.get('NETLAB_GEOIP_FETCH', '1') != '0'

    def ensure_geo(self, cache_dir=None, wait=False):
        """都市DB(DB-IP City Lite)を裏で読む。数十秒〜数分かかるので画面を止めない"""
        if self.geo.ready() or (self._geo_thread and self._geo_thread.is_alive()):
            return self.geo.status
        if not self.enabled() and not os.environ.get('NETLAB_GEOIP_DB'):
            return self.geo.status

        def run():
            self.geo.ensure(cache_dir, fetch=self.geo_fetch_enabled())
            if not self.geo.ready() and os.environ.get('NETLAB_WORLD_SAMPLE', '') == '1':
                from .world_geo import load_sample_geo
                why = self.geo.status.get('detail', '')
                load_sample_geo(self.geo)
                self.geo.status['detail'] = f'見本の都市DB(本物は {why})'[:200]
            if self.geo.ready() and self.enabled():
                self.refresh_city()           # 都市DBが揃ったので、都市に置き直す
        self._geo_thread = threading.Thread(target=run, daemon=True, name='geoip')
        self._geo_thread.start()
        if wait:
            self._geo_thread.join()
        return self.geo.status

    def refresh_city(self, dshield=None, atlas=None):
        """都市の粒度: DShield の攻撃元、RIPE Atlas の都市間経路"""
        from .world_geo import DShieldClient
        if not self.enabled() and dshield is None and atlas is None:
            self.dshield = {'status': 'off', 'detail': 'NETLAB_WORLD_FETCH=0', 'cities': []}
            self.atlas = {'status': 'off', 'detail': 'NETLAB_WORLD_FETCH=0', 'paths': []}
            return
        sample = os.environ.get('NETLAB_WORLD_SAMPLE', '') == '1'
        for name, run in (('dshield', lambda c: c.snapshot(self.geo)), ('atlas', lambda c: c.snapshot(self.pairs, self.geo))):
            client = (dshield or DShieldClient()) if name == 'dshield' else (atlas or self.atlas_client)
            try:
                val = run(client)
            except Exception as e:  # noqa: BLE001
                val = {'status': 'error', 'detail': str(e)[:200], 'paths': [], 'cities': []}
            if sample and val.get('status') != 'ok':
                from . import world_geo as wg
                from . import world_samples as ws
                real = val
                c = wg.DShieldClient(fetch=ws.dshield_fetch) if name == 'dshield' else wg.AtlasClient(fetch=ws.atlas_fetch)
                val = run(c)
                val.update(status='sample', detail=f"見本データ(本物は {real.get('status')}: {real.get('detail', '')})"[:200])
                if name == 'atlas':
                    self._atlas_sample = c
            setattr(self, name, val)

    def anchor_list(self, q=''):
        c = self.atlas_client if self.atlas_client.anchors else getattr(self, '_atlas_sample', self.atlas_client)
        return c.anchor_list(q), len(c.anchors)

    def set_pairs(self, pairs):
        clean = []
        for p in pairs or []:
            a, b = str(p.get('from', '')).strip()[:60], str(p.get('to', '')).strip()[:60]
            if a and b:
                clean.append({'from': a, 'to': b})
        self.pairs = clean[:8]
        return self.pairs

    def enabled(self):
        return os.environ.get('NETLAB_WORLD_FETCH', '1') != '0'

    def refresh(self, cache_dir=None, radar=None, ihr=None):
        """外部から取り直す(10分ごと。失敗は status に書く)"""
        if not self.enabled():
            self.radar = self.ihr = {'status': 'off', 'detail': 'NETLAB_WORLD_FETCH=0'}
            return
        self.cloud.fetch(cache_dir=cache_dir)
        sample = os.environ.get('NETLAB_WORLD_SAMPLE', '') == '1'
        for name, client in (('radar', radar or RadarClient()), ('ihr', ihr or IhrClient())):
            try:
                val = client.snapshot()
            except Exception as e:  # noqa: BLE001
                val = {'status': 'error', 'detail': str(e)[:200]}
            if sample and val.get('status') != 'ok':
                # 見本データ(本物の応答の形のまま同じ読み取り処理を通す)。画面には SAMPLE と出す
                from . import world_samples as ws
                real = val
                val = (RadarClient(token='sample', fetch=ws.fake_fetch(ws.RADAR)) if name == 'radar'
                       else IhrClient(fetch=ws.fake_fetch(ws.IHR))).snapshot()
                val.update(status='sample', detail=f"見本データ(本物は {real.get('status')}: {real.get('detail', '')})"[:200])
            setattr(self, name, val)
        self.ensure_geo(cache_dir)
        self.refresh_city()
        self._last = time.time()

    def due(self, now=None):
        return (now or time.time()) - self._last >= self.REFRESH

    def observe_flow(self, dst_ip, nbytes, now=None):
        hit = self.cloud.lookup(dst_ip)
        if not hit:
            return None
        provider, region, service = hit
        with self._lock:
            s = self.cloud_seen.setdefault((provider, region), {'bytes': 0, 'flows': 0, 'last': 0, 'service': service, 'win': []})
            now = now or time.time()
            s['bytes'] += nbytes
            s['flows'] += 1
            s['last'] = now
            s['win'].append((now, nbytes))
            s['win'] = [w for w in s['win'] if now - w[0] <= 60]
        return hit

    def cloud_snapshot(self, now=None):
        now = now or time.time()
        out = []
        with self._lock:
            for (provider, region), s in self.cloud_seen.items():
                bps = sum(b for t, b in s['win'] if now - t <= 60) * 8 / 60
                out.append({'provider': provider, 'region': region, 'place': region_place(provider, region),
                            'service': s['service'], 'bytes': s['bytes'], 'flows': s['flows'], 'bps': round(bps),
                            'age': round(now - s['last'], 1)})
        return sorted(out, key=lambda x: -x['bytes'])

    def snapshot(self, darknet=None):
        """darknet: ダークネットの観測(ラボは engine.darknet.darknet、単体版は SiteSecurity.darknet)"""
        if darknet is not None:
            try:
                self.peers.pull_darknet(darknet)
            except Exception:  # noqa: BLE001
                pass
        return {'radar': self.radar, 'ihr': self.ihr, 'cloud': {'loaded': self.cloud.loaded, 'regions': self.cloud_snapshot()},
                'geo': self.geo.status, 'peers': self.peers.snapshot(self.geo), 'dshield': self.dshield,
                'atlas': self.atlas, 'pairs': self.pairs, 'fetch': self.enabled(), 'last_refresh': self._last or None}


world = WorldState()
