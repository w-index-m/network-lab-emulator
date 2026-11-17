"""
WORLD の都市の粒度: IP → 都市(DB-IP City Lite 形式)、自分に来た外部の相手、DShield の攻撃元、
RIPE Atlas の都市 → 都市の経路、Cloudflare Radar の L3 攻撃

外部へは出ない(本物の応答の形をした見本を偽の fetch で渡す)。
"""
import csv
import io
import os
import sys
import time

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as A  # noqa: E402
from engine import world_samples as ws  # noqa: E402
from engine.world_geo import (AtlasClient, DShieldClient, GeoCity, PeerTracker, normalize_ip,  # noqa: E402
                              outside_ip, parse_traceroute)
from engine.world_sources import RadarClient, WorldState, world  # noqa: E402


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def _sample_geo():
    g = GeoCity()
    g.load_rows(csv.reader(io.StringIO(ws.GEO_CSV)), 'sample')
    return g


# ── IP → 都市 ──────────────────────────────────
def test_geocity_lookup_ranges_ipv6_and_cache(tmp_path):
    rows = [['1.0.4.0', '1.0.7.255', 'OC', 'AU', 'Victoria', 'Melbourne', '-37.814', '144.963'],
            ['::', '::ffff', 'ZZ', 'ZZ', '', '', '0', '0'],                                   # IPv6 は飛ばす
            ['1.0.0.0', '1.0.0.255', 'OC', 'AU', 'Queensland', 'South Brisbane', '-27.48', '153.02'],  # 順不同でも読める
            ['1.0.1.0', '1.0.1.255', 'AS', 'CN', 'Fujian', 'Fuzhou', '26.06', '119.3'],
            ['bad', 'row']]
    g = GeoCity()
    assert g.load_rows(rows, 'x') == 3
    assert g.lookup('1.0.5.9')['city'] == 'Melbourne'
    assert g.lookup('1.0.0.1')['city'] == 'South Brisbane' and g.lookup('1.0.1.255')['cc'] == 'CN'
    assert g.lookup('1.0.2.1') is None and g.lookup('0.0.0.1') is None and g.lookup('nope') is None
    p = tmp_path / 'geo.pkl'
    g.save_cache(str(p))
    g2 = GeoCity()
    g2.load_cache(str(p))
    assert g2.lookup('1.0.5.9') == g.lookup('1.0.5.9') and g2.status['cached']
    # NETLAB_GEOIP_DB(手元の CSV / CSV.gz)
    import gzip
    f = tmp_path / 'db.csv.gz'
    f.write_bytes(gzip.compress(ws.GEO_CSV.encode()))
    g3 = GeoCity()
    assert g3.load_csv(str(f)) == len(ws._GEO) and g3.lookup('198.51.100.8')['city'] == 'Moscow'


def test_ip_helpers():
    assert normalize_ip('061.177.172.013') == '61.177.172.13'
    assert outside_ip('8.8.8.8') and outside_ip('198.51.100.7')
    for ip in ('10.1.1.1', '192.168.0.1', '172.20.1.1', '100.64.1.1', '127.0.0.1', '224.0.0.5', 'x'):
        assert not outside_ip(ip)


# ── 自分に来た外部の相手 ─────────────────────────
def test_peer_tracker_by_city_and_darknet_pull():
    g = _sample_geo()
    t = PeerTracker()
    assert not t.observe('10.0.0.5', 'flow')                    # ラボの中は数えない
    t.observe('198.51.100.4', 'login', now=time.time())
    t.observe('198.51.100.5', 'flow', 3, 900)
    t.observe('203.0.113.200', 'flow')                          # 都市DBに無い → 都市不明

    class DN:
        events = [{'seq': 1, 't': time.time(), 'src': '198.51.100.8', 'len': 60},
                  {'seq': 2, 't': time.time(), 'src': '198.51.100.8', 'len': 60}]
    t.pull_darknet(DN)
    t.pull_darknet(DN)                                         # 同じものは二度取り込まない
    snap = t.snapshot(g)
    by = {c['city']: c for c in snap['cities']}
    assert by['Shanghai']['weight'] == 4 and by['Shanghai']['kinds'] == {'login': 1, 'flow': 3}
    assert by['Moscow']['kinds'] == {'darknet': 2} and by['Moscow']['live']
    assert snap['unlocated'] == 1 and snap['ips'] == 4


# ── DShield ──────────────────────────────────────
def test_dshield_parse_padded_and_cities():
    g = _sample_geo()
    d = DShieldClient(fetch=ws.dshield_fetch).snapshot(g)
    assert d['status'] == 'ok' and d['ips'] == 15
    top = d['cities'][0]
    assert top['city'] == 'Beijing' and top['ips'] == 4 and top['weight'] == 48210 + 22840 + 7400 + 2100
    assert top['top'][0] == '198.51.100.1'                     # 0埋めを外している
    # 辞書で包まれた形・ラボ内のアドレス・都市DBなし
    d2 = DShieldClient(fetch=lambda *a, **k: {'topips': [{'ip': '010.0.0.1', 'count': 5}, {'ip': '198.051.100.007', 'count': 9}]}).snapshot(GeoCity())
    assert d2['ips'] == 1 and d2['cities'] == [] and d2['unlocated'] == 1 and '都市DB' in d2['detail']


# ── RIPE Atlas ───────────────────────────────────
def test_parse_traceroute_timeouts_and_min_rtt():
    hops = parse_traceroute({'result': [
        {'hop': 1, 'result': [{'from': '192.0.2.2', 'rtt': 1.5}, {'from': '192.0.2.2', 'rtt': 0.9}, {'x': '*'}]},
        {'hop': 2, 'result': [{'x': '*'}, {'x': '*'}]},
        {'hop': 3, 'result': [{'from': '192.0.2.3', 'rtt': 70.1}, {'from': '192.0.2.4', 'rtt': 71.0}]}]})
    assert hops[0] == {'hop': 1, 'ip': '192.0.2.2', 'ips': ['192.0.2.2'], 'rtt': 0.9, 'timeout': False}
    assert hops[1]['timeout'] and hops[1]['rtt'] is None
    assert hops[2]['ips'] == ['192.0.2.3', '192.0.2.4']


def test_atlas_city_to_city_paths():
    g = _sample_geo()
    urls = []

    def fetch(url, headers=None, timeout=15):
        urls.append(url)
        return ws.atlas_fetch(url)
    a = AtlasClient(fetch=fetch)
    snap = a.snapshot([{'from': 'New York', 'to': 'Moscow'}, {'from': 'tokyo', 'to': 'DE'},
                       {'from': 'Tokyo', 'to': 'Moscow'}, {'from': 'Beijing', 'to': 'Moscow'}], g)
    assert snap['status'] == 'ok' and snap['anchors'] == 6
    p = snap['paths'][0]
    assert [c['city'] for c in p['cities']] == ['New York', 'London', 'Frankfurt am Main', 'Stockholm', 'Helsinki', 'Moscow']
    assert p['rtt'] == 119.4 and p['reached'] and p['hop_count'] == 9 and p['msm_id'] == 90002
    assert p['cities'][0]['end'] == 'src' and p['cities'][-1]['end'] == 'dst'
    assert sum(1 for h in p['hops'] if h['timeout']) == 1
    # 都市名・国コードどちらでも。DB-IP の「Frankfurt am Main」はアンカーの「Frankfurt」と同じ場所としてまとめる
    q = snap['paths'][1]
    assert q['to']['city'] == 'Frankfurt' and [c['city'] for c in q['cities']][-1] == 'Frankfurt'
    assert len([c for c in q['cities'] if 'Frankfurt' in c['city']]) == 1
    assert snap['paths'][2]['error'] == '経路の計測結果がまだありません'
    assert snap['paths'][3]['error'].startswith('アンカーが見つかりません: Beijing')
    latest = [u for u in urls if '/latest/' in u]
    assert 'measurements/90002/latest/' in latest[0] and 'probe_ids=6001' in latest[0]
    assert a.anchor_list('mo') == [{'city': 'Moscow', 'cc': 'RU', 'fqdn': 'ru-mow-as64501.anchors.atlas.ripe.net'}]


# ── Radar L3 ─────────────────────────────────────
def test_radar_l3_origins_and_targets():
    r = RadarClient(token='t', fetch=ws.fake_fetch(ws.RADAR)).snapshot()
    l3 = r['l3']
    assert l3['origins'][0] == {'cc': 'CN', 'name': 'China', 'share': 18.4}
    assert l3['targets'][0]['cc'] == 'US' and l3['pairs'] == []        # 送信元だけの形なら組は作らない
    table = dict(ws.RADAR)
    table['/attacks/layer3/top/attacks'] = {'success': True, 'result': {'top_0': [
        {'originCountryAlpha2': 'CN', 'originCountryName': 'China', 'targetCountryAlpha2': 'US', 'targetCountryName': 'United States', 'value': '9.1'}]}}
    r2 = RadarClient(token='t', fetch=ws.fake_fetch(table)).snapshot()
    assert r2['l3']['pairs'] == [{'from': 'CN', 'to': 'US', 'share': 9.1}]


# ── まとめ(WorldState)と API ───────────────────────
def test_world_state_city_refresh_off_and_sample(monkeypatch):
    s = WorldState()
    monkeypatch.setenv('NETLAB_WORLD_FETCH', '0')
    s.refresh_city()
    assert s.dshield['status'] == 'off' and s.atlas['status'] == 'off'
    monkeypatch.setenv('NETLAB_WORLD_FETCH', '1')
    monkeypatch.setenv('NETLAB_WORLD_SAMPLE', '1')
    from engine.world_geo import load_sample_geo
    load_sample_geo(s.geo)

    def boom(*a, **k):
        raise OSError('blocked')
    s.refresh_city(dshield=DShieldClient(fetch=boom), atlas=AtlasClient(fetch=boom))
    assert s.dshield['status'] == 'sample' and 'blocked' in s.dshield['detail'] and s.dshield['cities']
    assert s.atlas['status'] == 'sample' and len(s.atlas['paths']) == 3
    assert s.anchor_list('york')[0][0]['city'] == 'New York'
    assert s.set_pairs([{'from': ' Tokyo ', 'to': 'Moscow'}, {'from': '', 'to': 'x'}]) == [{'from': 'Tokyo', 'to': 'Moscow'}]


def test_api_world_city_fields_and_hooks(client, monkeypatch):
    g = _sample_geo()
    monkeypatch.setattr(world, 'geo', g)
    monkeypatch.setattr(world, 'peers', PeerTracker())
    A._on_auth('dev-x', '198.51.100.4', False, 'admin', time.time())        # SSH/Telnet のログイン失敗
    A._on_flow({'src': '198.51.100.7', 'dst': '10.9.9.9', 'proto': 'tcp', 'packets': 2, 'bytes': 200})   # 宛先がラボでない → 数えない
    d = client.get('/api/world').json()
    assert {'geo', 'peers', 'dshield', 'atlas', 'pairs'} <= set(d)
    assert [c['city'] for c in d['peers']['cities']] == ['Shanghai'] and d['geo']['state'] == 'ready'
    assert client.get('/api/world/geo', params={'ip': '198.51.100.8'}).json()['geo']['city'] == 'Moscow'
    assert client.post('/api/world/paths', json={'pairs': []}).status_code == 400
    old = list(world.pairs)
    try:
        r = client.post('/api/world/paths', json={'pairs': [{'from': 'New York', 'to': 'Moscow'}]}).json()
        assert r['pairs'] == [{'from': 'New York', 'to': 'Moscow'}] and r['atlas']['status'] == 'off'   # テストは外へ出ない
    finally:
        world.pairs = old
    assert 'anchors' in client.get('/api/world/anchors').json()


def test_world_page_city_markup():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    for s in ('data-wl=', 'id="wAdd"', '/api/world/paths', 'ROUTE_COL', "'worldpanel'", 'DSHIELD'):
        assert s in html
