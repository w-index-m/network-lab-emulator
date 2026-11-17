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
