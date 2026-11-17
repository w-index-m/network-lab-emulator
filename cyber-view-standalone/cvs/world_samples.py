"""
WORLD の見本データ(NETLAB_WORLD_SAMPLE=1 のときだけ使う。画面には「見本 SAMPLE」と出す)

形は本物の API の応答そのまま(Cloudflare Radar の openapi.json と IHR の serializers.py に合わせた)。
RadarClient / IhrClient に偽の fetch として渡し、本物と同じ読み取り処理を通す。値は作り物。
"""

RADAR = {
    '/http/top/locations': {'success': True, 'result': {'top_0': [
        {'clientCountryAlpha2': 'US', 'clientCountryName': 'United States', 'value': '22.1'},
        {'clientCountryAlpha2': 'IN', 'clientCountryName': 'India', 'value': '7.4'},
        {'clientCountryAlpha2': 'DE', 'clientCountryName': 'Germany', 'value': '5.2'},
        {'clientCountryAlpha2': 'BR', 'clientCountryName': 'Brazil', 'value': '4.6'},
        {'clientCountryAlpha2': 'JP', 'clientCountryName': 'Japan', 'value': '3.9'},
        {'clientCountryAlpha2': 'GB', 'clientCountryName': 'United Kingdom', 'value': '3.5'},
        {'clientCountryAlpha2': 'FR', 'clientCountryName': 'France', 'value': '3.1'},
        {'clientCountryAlpha2': 'ID', 'clientCountryName': 'Indonesia', 'value': '2.8'},
        {'clientCountryAlpha2': 'SG', 'clientCountryName': 'Singapore', 'value': '2.2'},
        {'clientCountryAlpha2': 'AU', 'clientCountryName': 'Australia', 'value': '1.6'}]}},
    '/attacks/layer7/top/attacks': {'success': True, 'result': {'top_0': [
        {'originCountryAlpha2': 'US', 'originCountryName': 'United States', 'targetCountryAlpha2': 'JP', 'targetCountryName': 'Japan', 'value': '9.8'},
        {'originCountryAlpha2': 'DE', 'originCountryName': 'Germany', 'targetCountryAlpha2': 'US', 'targetCountryName': 'United States', 'value': '7.1'},
        {'originCountryAlpha2': 'ID', 'originCountryName': 'Indonesia', 'targetCountryAlpha2': 'SG', 'targetCountryName': 'Singapore', 'value': '5.5'},
        {'originCountryAlpha2': 'BR', 'originCountryName': 'Brazil', 'targetCountryAlpha2': 'US', 'targetCountryName': 'United States', 'value': '4.9'},
        {'originCountryAlpha2': 'NL', 'originCountryName': 'Netherlands', 'targetCountryAlpha2': 'JP', 'targetCountryName': 'Japan', 'value': '4.0'},
        {'originCountryAlpha2': 'VN', 'originCountryName': 'Vietnam', 'targetCountryAlpha2': 'JP', 'targetCountryName': 'Japan', 'value': '3.3'},
        {'originCountryAlpha2': 'IN', 'originCountryName': 'India', 'targetCountryAlpha2': 'GB', 'targetCountryName': 'United Kingdom', 'value': '2.7'}]}},
    '/traffic_anomalies': {'success': True, 'result': {'trafficAnomalies': [
        {'asnDetails': {'asn': '2516', 'name': 'KDDI', 'location': {'code': 'JP', 'name': 'Japan'}},
         'startDate': '2026-11-16T21:10:00Z', 'endDate': '2026-11-16T22:40:00Z', 'status': 'VERIFIED', 'type': 'AS',
         'uuid': 'sample-1'}]}},
}

IHR = {
    '/network_delay/alarms/': {'count': 2, 'next': None, 'results': [
        {'timebin': '2026-11-17T01:00:00Z', 'startpoint_type': 'AS', 'startpoint_name': '4713', 'startpoint_af': 4,
         'endpoint_type': 'CT', 'endpoint_name': 'Osaka, Osaka, JP', 'endpoint_af': 4, 'deviation': 23.4},
        {'timebin': '2026-11-17T00:45:00Z', 'startpoint_type': 'AS', 'startpoint_name': '17676', 'startpoint_af': 4,
         'endpoint_type': 'AS', 'endpoint_name': '2914', 'endpoint_af': 4, 'deviation': 12.1}]},
    '/disco/events/': {'count': 1, 'next': None, 'results': [
        {'id': 1, 'streamtype': 'asn', 'streamname': '2518', 'starttime': '2026-11-16T23:30:00Z',
         'endtime': '2026-11-17T00:05:00Z', 'avglevel': 12.0, 'nbdiscoprobes': 9, 'totalprobes': 21, 'ongoing': False}]},
}


def fake_fetch(table):
    def fetch(url, headers=None, timeout=15):
        path = url.split('/radar', 1)[1] if '/radar' in url else url.split('/ihr/api', 1)[1]
        path = path.split('?', 1)[0]
        if path == '/disco/events/' and 'streamname=2518' not in url:
            return {'count': 0, 'next': None, 'results': []}
        return table[path]
    return fetch

# ── 都市の粒度の見本(NETLAB_WORLD_SAMPLE=1 のときだけ)。IP は文書用の帯(192.0.2.0/24・198.51.100.0/24)だけを使う ──
_CITIES = {   # 記号: (国, 州など, 都市, 緯度, 経度)
    'BJ': ('CN', 'Beijing', 'Beijing', 39.904, 116.407), 'SH': ('CN', 'Shanghai', 'Shanghai', 31.230, 121.474),
    'SZ': ('CN', 'Guangdong', 'Shenzhen', 22.543, 114.058), 'MOW': ('RU', 'Moscow', 'Moscow', 55.756, 37.617),
    'LED': ('RU', 'Saint Petersburg', 'Saint Petersburg', 59.939, 30.316), 'AMS': ('NL', 'North Holland', 'Amsterdam', 52.370, 4.895),
    'SAO': ('BR', 'Sao Paulo', 'Sao Paulo', -23.551, -46.633), 'HAN': ('VN', 'Hanoi', 'Hanoi', 21.028, 105.834),
    'IAD': ('US', 'Virginia', 'Ashburn', 39.044, -77.488), 'DEL': ('IN', 'Delhi', 'New Delhi', 28.614, 77.209),
    'NYC': ('US', 'New York', 'New York', 40.713, -74.006), 'LON': ('GB', 'England', 'London', 51.507, -0.128),
    'FRA': ('DE', 'Hesse', 'Frankfurt am Main', 50.110, 8.682), 'STO': ('SE', 'Stockholm', 'Stockholm', 59.329, 18.069),
    'HEL': ('FI', 'Uusimaa', 'Helsinki', 60.170, 24.938), 'HKG': ('HK', 'Hong Kong', 'Hong Kong', 22.320, 114.170),
    'TYO': ('JP', 'Tokyo', 'Tokyo', 35.690, 139.692), 'SEA': ('US', 'Washington', 'Seattle', 47.606, -122.332),
    'LAX': ('US', 'California', 'Los Angeles', 34.052, -118.244), 'SJC': ('US', 'California', 'San Jose', 37.339, -121.895),
}
# 見本の IP → 都市
_GEO = {f'198.51.100.{i}': c for i, c in enumerate(
    ['BJ', 'BJ', 'BJ', 'SH', 'SH', 'SZ', 'MOW', 'MOW', 'LED', 'AMS', 'SAO', 'HAN', 'IAD', 'DEL', 'BJ', 'SH'], start=1)}
_GEO.update({f'192.0.2.{i}': c for i, c in enumerate(
    ['NYC', 'NYC', 'LON', 'LON', 'FRA', 'STO', 'HEL', 'MOW', 'MOW',          # 1-9 ニューヨーク → モスクワ
     'HKG', 'HKG', 'TYO', 'SEA', 'SJC', 'LAX',                              # 10-15 香港 → ロサンゼルス
     'TYO', 'TYO', 'LAX', 'NYC', 'LON', 'AMS', 'FRA'], start=1)})           # 16-22 東京 → フランクフルト
GEO_CSV = ''.join(f'{ip},{ip},XX,{_CITIES[c][0]},{_CITIES[c][1]},{_CITIES[c][2]},{_CITIES[c][3]},{_CITIES[c][4]}\n'
                  for ip, c in sorted(_GEO.items(), key=lambda kv: tuple(int(p) for p in kv[0].split('.'))))

# SANS ISC / DShield の topips(IP は0埋め、本物と同じ)
DSHIELD = [{'rank': i + 1, 'source': '.'.join(p.zfill(3) for p in ip.split('.')), 'reports': r, 'targets': t}
           for i, (ip, r, t) in enumerate([
               ('198.51.100.1', 48210, 2210), ('198.51.100.4', 39120, 1890), ('198.51.100.7', 30555, 1502),
               ('198.51.100.2', 22840, 980), ('198.51.100.6', 19870, 1210), ('198.51.100.10', 15010, 801),
               ('198.51.100.11', 12002, 640), ('198.51.100.12', 9800, 511), ('198.51.100.9', 8700, 455),
               ('198.51.100.3', 7400, 402), ('198.51.100.8', 6900, 380), ('198.51.100.13', 5100, 260),
               ('198.51.100.14', 4300, 230), ('198.51.100.5', 3900, 199), ('198.51.100.15', 2100, 120)])]

# RIPE Atlas: アンカー一覧・アンカーごとの計測・最新の traceroute 結果
ATLAS_ANCHORS = {'count': 6, 'next': None, 'results': [
    {'id': 1001, 'fqdn': 'us-nyc-as64500.anchors.atlas.ripe.net', 'probe': 6001, 'city': 'New York', 'country': 'US',
     'ip_v4': '192.0.2.1', 'geometry': {'type': 'Point', 'coordinates': [-74.006, 40.713]}, 'is_disabled': False},
    {'id': 1002, 'fqdn': 'ru-mow-as64501.anchors.atlas.ripe.net', 'probe': 6002, 'city': 'Moscow', 'country': 'RU',
     'ip_v4': '192.0.2.9', 'geometry': {'type': 'Point', 'coordinates': [37.617, 55.756]}, 'is_disabled': False},
    {'id': 1003, 'fqdn': 'hk-hkg-as64502.anchors.atlas.ripe.net', 'probe': 6003, 'city': 'Hong Kong', 'country': 'HK',
     'ip_v4': '192.0.2.10', 'geometry': {'type': 'Point', 'coordinates': [114.17, 22.32]}, 'is_disabled': False},
    {'id': 1004, 'fqdn': 'us-lax-as64503.anchors.atlas.ripe.net', 'probe': 6004, 'city': 'Los Angeles', 'country': 'US',
     'ip_v4': '192.0.2.15', 'geometry': {'type': 'Point', 'coordinates': [-118.244, 34.052]}, 'is_disabled': False},
    {'id': 1005, 'fqdn': 'jp-tyo-as64504.anchors.atlas.ripe.net', 'probe': 6005, 'city': 'Tokyo', 'country': 'JP',
     'ip_v4': '192.0.2.16', 'geometry': {'type': 'Point', 'coordinates': [139.692, 35.69]}, 'is_disabled': False},
    {'id': 1006, 'fqdn': 'de-fra-as64505.anchors.atlas.ripe.net', 'probe': 6006, 'city': 'Frankfurt', 'country': 'DE',
     'ip_v4': '192.0.2.22', 'geometry': {'type': 'Point', 'coordinates': [8.682, 50.11]}, 'is_disabled': False}]}
_MESH = {1002: 90002, 1004: 90004, 1006: 90006}       # 宛先アンカー → Anchoring Mesh の traceroute
_PATHS = {   # (msm, 送信元 probe) → [(hop の IP, rtt)]
    (90002, 6001): [('192.0.2.2', 0.8), ('192.0.2.3', 71.2), ('192.0.2.4', 72.0), ('192.0.2.5', 79.5),
                    ('192.0.2.6', 96.3), ('192.0.2.7', 104.1), (None, None), ('192.0.2.8', 118.7), ('192.0.2.9', 119.4)],
    (90004, 6003): [('192.0.2.11', 1.1), ('192.0.2.12', 48.9), ('192.0.2.13', 139.0), ('192.0.2.14', 151.2),
                    ('192.0.2.15', 155.8)],
    (90006, 6005): [('192.0.2.17', 0.9), ('192.0.2.18', 101.5), ('192.0.2.19', 168.0), ('192.0.2.20', 236.4),
                    ('192.0.2.21', 243.0), ('192.0.2.22', 247.7)],
}


def atlas_fetch(url, headers=None, timeout=15):
    """RIPE Atlas の見本(本物の URL の形で答える)"""
    path = url.split('/api/v2', 1)[1]
    q = dict(x.split('=', 1) for x in path.split('?', 1)[1].split('&')) if '?' in path else {}
    path = path.split('?', 1)[0]
    if path == '/anchors/':
        return ATLAS_ANCHORS
    if path == '/anchor-measurements/':
        mid = _MESH.get(int(q.get('target', 0)))
        return {'count': 1 if mid else 0, 'next': None, 'results': [
            {'id': 1, 'target': f"https://atlas.ripe.net/api/v2/anchors/{q.get('target')}/", 'is_mesh': True,
             'type': 'traceroute', 'measurement': f'https://atlas.ripe.net/api/v2/measurements/{mid}/'}] if mid else []}
    if path.startswith('/measurements/') and path.endswith('/latest/'):
        mid, prb = int(path.split('/')[2]), int(q.get('probe_ids', 0))
        hops = _PATHS.get((mid, prb))
        if not hops:
            return []
        dst = hops[-1][0]
        return [{'fw': 5080, 'af': 4, 'type': 'traceroute', 'msm_id': mid, 'prb_id': prb, 'dst_addr': dst,
                 'from': '192.0.2.1', 'timestamp': 1795400000,
                 'result': [{'hop': i + 1, 'result': ([{'from': ip, 'rtt': rtt, 'size': 68, 'ttl': 60 - i}] * 3 if ip
                                                      else [{'x': '*'}] * 3)} for i, (ip, rtt) in enumerate(hops)]}]
    if path == '/measurements/':
        return {'count': 0, 'next': None, 'results': []}
    raise OSError(f'見本に無い URL: {url}')


# Cloudflare Radar の L3(DDoS)攻撃: 送信元の国・標的の国
RADAR.update({
    '/attacks/layer3/top/attacks': {'success': True, 'result': {'top_0': [
        {'originCountryAlpha2': 'CN', 'originCountryName': 'China', 'value': '18.4'},
        {'originCountryAlpha2': 'US', 'originCountryName': 'United States', 'value': '12.9'},
        {'originCountryAlpha2': 'BR', 'originCountryName': 'Brazil', 'value': '7.7'},
        {'originCountryAlpha2': 'RU', 'originCountryName': 'Russia', 'value': '6.1'}]}},
    '/attacks/layer3/top/locations/origin': {'success': True, 'result': {'top_0': [
        {'originCountryAlpha2': 'CN', 'originCountryName': 'China', 'rank': 1, 'value': '18.4'},
        {'originCountryAlpha2': 'US', 'originCountryName': 'United States', 'rank': 2, 'value': '12.9'},
        {'originCountryAlpha2': 'BR', 'originCountryName': 'Brazil', 'rank': 3, 'value': '7.7'},
        {'originCountryAlpha2': 'RU', 'originCountryName': 'Russia', 'rank': 4, 'value': '6.1'},
        {'originCountryAlpha2': 'VN', 'originCountryName': 'Vietnam', 'rank': 5, 'value': '4.2'}]}},
    '/attacks/layer3/top/locations/target': {'success': True, 'result': {'top_0': [
        {'targetCountryAlpha2': 'US', 'targetCountryName': 'United States', 'rank': 1, 'value': '31.0'},
        {'targetCountryAlpha2': 'DE', 'targetCountryName': 'Germany', 'rank': 2, 'value': '9.4'},
        {'targetCountryAlpha2': 'JP', 'targetCountryName': 'Japan', 'rank': 3, 'value': '6.6'},
        {'targetCountryAlpha2': 'SG', 'targetCountryName': 'Singapore', 'rank': 4, 'value': '5.0'}]}},
})


def dshield_fetch(url, headers=None, timeout=15):
    return DSHIELD
