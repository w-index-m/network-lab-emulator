"""
WORLD(地球儀)と外部データ: クラウドの IP 帯・Cloudflare Radar・IIJ Internet Health Report・MAWI の再生

外部へは出ない(Radar / IHR は本物の応答の形をした見本を偽の fetch で渡す。AWS / Google Cloud は小さな見本)。
"""
import gzip
import json
import os
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as A  # noqa: E402
from engine import world_samples as ws  # noqa: E402
from engine.mawi import aggregate, iter_packets, mawi_url, open_source, replayer, write_pcap  # noqa: E402
from engine.world_sources import CloudRanges, IhrClient, RadarClient, WorldState, region_place, world  # noqa: E402

AWS = {'createDate': '2026-11-17-00-00-00', 'prefixes': [
    {'ip_prefix': '13.112.0.0/14', 'region': 'ap-northeast-1', 'service': 'AMAZON'},
    {'ip_prefix': '13.112.0.0/14', 'region': 'ap-northeast-1', 'service': 'EC2'},
    {'ip_prefix': '13.113.0.0/16', 'region': 'ap-northeast-1', 'service': 'S3'},
    {'ip_prefix': '52.94.0.0/22', 'region': 'us-east-1', 'service': 'AMAZON'}]}
GCP = {'creationTime': '2026-11-17T00:00:00', 'prefixes': [
    {'ipv4Prefix': '34.84.0.0/16', 'service': 'Google Cloud', 'scope': 'asia-northeast1'},
    {'ipv6Prefix': '2600:1900::/35', 'service': 'Google Cloud', 'scope': 'us-central1'}]}


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def _cli(c, d, *cmds):
    for x in cmds:
        c.post('/api/cli', json={'device_id': d, 'command': x})


# ── クラウドの IP 帯 ─────────────────────────────
def test_cloud_ranges_longest_match_and_places():
    c = CloudRanges()
    c.load_aws(AWS)
    c.load_gcp(GCP)
    assert c.lookup('13.112.5.5') == ('aws', 'ap-northeast-1', 'EC2')          # AMAZON より具体的なサービス
    assert c.lookup('13.113.9.9') == ('aws', 'ap-northeast-1', 'S3')           # /16 が /14 より優先
    assert c.lookup('34.84.1.1') == ('gcp', 'asia-northeast1', 'Google Cloud')
    assert c.lookup('8.8.8.8') is None and c.lookup('not-an-ip') is None
    assert c.loaded['aws']['count'] == 4 and c.loaded['gcp']['count'] == 1      # IPv6 は数えない
    assert region_place('aws', 'ap-northeast-1')['name'] == '東京'
    assert region_place('gcp', 'us-central1')['name'] == 'アイオワ'
    assert region_place('aws', 'cn-north-1') is None


def test_cloud_traffic_is_counted_per_region_and_mawi_is_not(client, monkeypatch):
    ws_ = WorldState()
    ws_.cloud.load_aws(AWS)
    monkeypatch.setattr(world, 'cloud', ws_.cloud)
    monkeypatch.setattr(world, 'cloud_seen', {})
    r = f'wv-r-{uuid.uuid4().hex[:5]}'
    client.post('/api/device', json={'id': r, 'type': 'cisco', 'hostname': 'WV-R'})
    _cli(client, r, 'ping 13.112.1.1 repeat 100 size 1000')
    A._on_flow({'src': '10.9.9.9', 'dst': '52.94.0.10', 'proto': 'tcp', 'packets': 10, 'bytes': 15000, 'via': 'netflow'})
    A._on_flow({'src': '10.9.9.9', 'dst': '52.94.0.11', 'proto': 'tcp', 'packets': 10, 'bytes': 99999, 'via': 'mawi'})
    d = client.get('/api/world').json()
    reg = {(x['provider'], x['region']): x for x in d['cloud']['regions']}
    assert reg[('aws', 'ap-northeast-1')]['bytes'] == 100 * 1000 and reg[('aws', 'ap-northeast-1')]['place']['name'] == '東京'
    assert reg[('aws', 'us-east-1')]['bytes'] == 15000                             # MAWI の 99999 は数えない
    assert reg[('aws', 'ap-northeast-1')]['bps'] > 0
    assert d['site']['lat'] == 35.68 and d['site']['name']


# ── Cloudflare Radar ─────────────────────────────
def test_radar_without_token_and_parse_real_shape():
    assert RadarClient(token='').snapshot()['status'] == 'no_token'
    urls = []

    def fetch(url, headers=None, timeout=15):
        urls.append((url, headers))
        return ws.fake_fetch(ws.RADAR)(url, headers)
    r = RadarClient(token='tkn', fetch=fetch).snapshot()
    assert r['status'] == 'ok'
    assert r['locations'][0] == {'cc': 'US', 'name': 'United States', 'share': 22.1}
    assert r['attacks'][0]['from'] == 'US' and r['attacks'][0]['to'] == 'JP' and r['attacks'][0]['share'] == 9.8
    assert r['anomalies'][0]['cc'] == 'JP' and r['anomalies'][0]['asn'] == '2516'
    assert all(h == {'Authorization': 'Bearer tkn'} for _, h in urls)
    assert any('/radar/attacks/layer7/top/attacks?' in u for u, _ in urls)


def test_radar_api_error_is_reported():
    def fetch(url, headers=None, timeout=15):
        return {'success': False, 'errors': [{'message': 'Invalid API Token'}]}
    with pytest.raises(RuntimeError, match='Invalid API Token'):
        RadarClient(token='x', fetch=fetch).snapshot()


# ── IIJ Internet Health Report ───────────────────
def test_ihr_parse_and_required_timebin():
    urls = []

    def fetch(url, headers=None, timeout=15):
        urls.append(url)
        return ws.fake_fetch(ws.IHR)(url)
    r = IhrClient(fetch=fetch).snapshot()
    assert r['status'] == 'ok'
    assert r['delay_alarms'][0]['asn'] == '4713' and r['delay_alarms'][0]['as_name'] == 'OCN'
    assert r['delay_alarms'][0]['to'] == 'CTOsaka, Osaka, JP' and r['delay_alarms'][0]['deviation'] == 23.4
    assert len(r['outages']) == 1 and r['outages'][0]['stream'] == '2518'           # 同じ事象は1つに
    delay_url = next(u for u in urls if '/network_delay/alarms/' in u)
    assert 'timebin__gte=' in delay_url and 'startpoint_type=AS' in delay_url and '2497%7C' in delay_url


def test_world_refresh_off_sample_and_alerts(monkeypatch):
    s = WorldState()
    monkeypatch.setenv('NETLAB_WORLD_FETCH', '0')
    s.refresh()
    assert s.radar['status'] == 'off' and s.ihr['status'] == 'off'
    monkeypatch.setenv('NETLAB_WORLD_FETCH', '1')
    monkeypatch.setenv('NETLAB_WORLD_SAMPLE', '1')
    monkeypatch.setattr(s.cloud, 'fetch', lambda **k: {})

    def boom(*a, **k):
        raise OSError('blocked')
    s.refresh(radar=RadarClient(token=''), ihr=IhrClient(fetch=boom))
    assert s.radar['status'] == 'sample' and 'no_token' in s.radar['detail'] and s.radar['attacks']
    assert s.ihr['status'] == 'sample' and 'blocked' in s.ihr['detail'] and s.ihr['outages']
    # 見本からは警報を作らない。本物(ok)なら新しいものを1回だけ
    monkeypatch.setattr(world, 'radar', s.radar)
    monkeypatch.setattr(world, 'ihr', s.ihr)
    A._world_alerted.clear()
    assert A._world_alerts() == []
    monkeypatch.setattr(world, 'ihr', dict(s.ihr, status='ok'))
    monkeypatch.setattr(world, 'radar', dict(s.radar, status='ok'))
    got = A._world_alerts()
    rules = sorted(a['rule'] for a in got)
    assert rules == ['ihr_delay', 'ihr_delay', 'ihr_outage', 'radar_anomaly']
    assert all(a['source'] == 'WORLD' for a in got)
    assert A._world_alerts() == []


# ── MAWI ─────────────────────────────────────────
PKTS = [{'t': 1000.0 + i * 0.25, 'src': f'133.1.0.{i % 5 + 1}', 'dst': f'203.0.113.{i % 3 + 1}',
         'proto': ['tcp', 'udp', 'icmp'][i % 3], 'dport': [443, 53, 0][i % 3], 'flags': 0x18, 'length': 100 + i}
        for i in range(24)]


def test_pcap_round_trip_ethernet_raw_vlan_gzip(tmp_path):
    for lt in (1, 101):
        p = tmp_path / f'x{lt}.pcap'
        p.write_bytes(write_pcap(PKTS, linktype=lt))
        got = list(iter_packets(open_source(str(p))))
        assert len(got) == 24 and got[0]['src'] == '133.1.0.1' and got[0]['proto'] == 'tcp' and got[0]['dport'] == 443
        assert got[1]['proto'] == 'udp' and got[2]['proto'] == 'icmp' and got[5]['length'] == 105
    raw = write_pcap(PKTS[:2])                     # VLAN タグ付きにして読めること
    hdr, rec = raw[:24], raw[24:]
    frame = rec[16:]
    tagged = frame[:12] + b'\x81\x00\x00\x0a' + frame[12:]
    import struct
    rec2 = struct.pack('<IIII', 1000, 0, len(tagged), len(tagged)) + tagged
    (tmp_path / 'v.pcap').write_bytes(hdr + rec2)
    assert list(iter_packets(open_source(str(tmp_path / 'v.pcap'))))[0]['dst'] == '203.0.113.1'
    gz = tmp_path / 'x.pcap.gz'
    gz.write_bytes(gzip.compress(write_pcap(PKTS)))
    assert len(list(iter_packets(open_source(str(gz)), limit=10))) == 10
    secs = aggregate(iter_packets(open_source(str(gz))))
    assert [s for s, _ in secs] == [1000, 1001, 1002, 1003, 1004, 1005]
    assert sum(f['packets'] for _, fl in secs for f in fl) == 24
    assert mawi_url('202411151400') == 'https://mawi.wide.ad.jp/mawi/samplepoint-F/2024/202411151400.pcap.gz'
    with pytest.raises(ValueError):                # pcap ではないファイル
        list(iter_packets(open(__file__, 'rb')))


def test_mawi_replay_api_feeds_flows(client, tmp_path):
    p = tmp_path / 'm.pcap.gz'
    p.write_bytes(gzip.compress(write_pcap(PKTS)))
    assert client.post('/api/mawi/replay', json={}).status_code == 400
    assert client.post('/api/mawi/replay', json={'source': str(tmp_path / 'nope.pcap')}).status_code == 400
    seq0 = A._flow_recorder.seq
    r = client.post('/api/mawi/replay', json={'source': str(p), 'speed': 100, 'topk': 5}).json()
    assert r['running']
    for _ in range(100):
        if not replayer.status()['running']:
            break
        time.sleep(0.05)
    st = client.get('/api/mawi/status').json()
    assert st['phase'] == 'done' and st['packets'] == 24 and st['seconds'] == 6 and st['protocols']['tcp'] == 8
    evs = [e for e in A._flow_recorder.events if e['seq'] > seq0 and e.get('via') == 'mawi']
    assert len(evs) == st['flows_sent'] and evs[0]['src_ip'].startswith('133.1.0.')
    assert client.get('/api/world').json()['mawi']['phase'] == 'done'


def test_world_map_data_and_page():
    d = json.load(open(os.path.join(ROOT, 'static', 'world', 'world.json'), encoding='utf-8'))
    assert d['countries']['JP'][1] == '日本' and 30 < d['countries']['JP'][2] < 40
    assert d['countries']['SG'][2] == 1.35 and len(d['lines']) > 500
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert 'id="mWorld"' in html and 'function drawWorld' in html and "view') === 'world'" in html


def test_ihr_new_host_and_drops_rejected_params():
    """IHR は www.ihr.live に移転(旧 URL は 301)。新 API は知らない項目を 400 invalid_query_params で断るので、
    断られた項目を外して1回だけやり直す(実際に format が断られた: 2026-11-21、ユーザーの PC から確認)"""
    import io
    import urllib.error
    assert IhrClient.BASE == 'https://www.ihr.live/ihr/api'
    urls = []

    def fetch(url, headers=None, timeout=15):
        urls.append(url)
        if 'streamtype=bogus' in url:
            body = json.dumps({'error': 'invalid_query_params', 'unexpected': ['streamtype'],
                               'allowed': ['streamname', 'starttime__gte', 'ordering']}).encode()
            raise urllib.error.HTTPError(url, 400, 'Bad Request', {}, io.BytesIO(body))
        return {'results': [{'ok': 1}]}
    c = IhrClient(fetch=fetch)
    assert c._get('/disco/events/', streamname='JP', streamtype='bogus') == [{'ok': 1}]
    assert 'streamtype' not in urls[-1] and 'streamname=JP' in urls[-1]
    c.snapshot()
    assert not any('format=' in u for u in urls)

    def fetch_bad(url, headers=None, timeout=15):
        raise urllib.error.HTTPError(url, 400, 'Bad Request', {}, io.BytesIO(b'{"error": "other"}'))
    with pytest.raises(RuntimeError, match='HTTP 400'):
        IhrClient(fetch=fetch_bad)._get('/disco/events/', streamname='JP')
