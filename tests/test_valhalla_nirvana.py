"""
ヴァルハラ(Prometheus)× ニルヴァーナ(/cyber-view)の連携

  ① GET /metrics           ニルヴァーナの判断をメトリクスで出す(形式・認証)
  ② /api/valhalla/replay   ① の履歴から各時刻のリンク状態と状態変化を組み立てる
  ③ /api/valhalla/forecast predict_linear / 平常時とのずれ
  ④ OSPF-MIB               エミュレータの SNMP に ospfRouterId / ospfNbrTable、スタンドアロン版の OSPF 表示
  ⑤ 発報中の警報            新しく出たものだけ VALHALLA の花びらで取り込む

Prometheus は偽サーバー(クエリの先頭語で結果を返す)で置き換える。実物でのライブ確認は docs/valhalla-nirvana.md。
"""
import filecmp
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')   # app を import する前に

from engine import valhalla as V

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class FakeProm:
    """/api/v1/query・query_range・alerts を真似る。query は「式の先頭が一致した最初の行」を返す"""

    def __init__(self):
        self.instant = []      # [(先頭語, rows)]
        self.ranges = []       # [(先頭語, rows)]
        self.alerts = []
        self.seen = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def _send(self, data):
                body = json.dumps({'status': 'success', 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == '/api/v1/alerts':
                    return self._send({'alerts': fake.alerts})
                expr = q.get('query', [''])[0]
                fake.seen.append(expr)
                table = fake.instant if u.path == '/api/v1/query' else fake.ranges
                for k, rows in table:
                    if expr.startswith(k):
                        return self._send({'resultType': 'vector', 'result': rows})
                return self._send({'resultType': 'vector', 'result': []})

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_port}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def prom():
    f = FakeProm()
    yield f
    f.srv.shutdown()


def _iv(lab, v):
    return {'metric': lab, 'value': [0, str(v)]}


def _rv(lab, pts):
    return {'metric': lab, 'values': [[t, str(v)] for t, v in pts]}


# ── ① ─────────────────────────────────────────────
def test_render_metrics_format():
    links = [{'a': 'r1', 'b': 'r2', 'host_a': 'R1', 'host_b': 'R"2', 'state': 'down', 'ab_bps': 1.5e6, 'ba_bps': 0, 'util': 0.25}]
    health = {'overall': 'crit', 'categories': [{'key': 'network', 'state': 'crit'}, {'key': 'syslog', 'state': 'warn'}]}
    ospf = {'enabled': True, 'devices': [{'id': 'r1', 'hostname': 'R1', 'router_id': '1.1.1.1', 'spf_runs': 4},
                                         {'id': 'r2', 'hostname': 'R2', 'router_id': '2.2.2.2', 'spf_runs': 3}],
            'adjacencies': [{'a': 'r1', 'b': 'r2', 'area': '0.0.0.0', 'state_a': 'Full', 'state_b': 'Init',
                             'dead_a': 31.0, 'dead_b': None}]}
    txt = V.render_metrics(links, health, ospf, {('valhalla', 'high', 'VALHALLA'): 2}, {('R1', 'add'): 5})
    assert txt.endswith('\n')
    assert 'netlab_link_state{a="r1",b="r2",host_a="R1",host_b="R\\"2"} 3' in txt    # 引用符はエスケープ
    assert 'dir="ab"} 1.5e+06' in txt and 'netlab_link_util{' in txt and '} 0.25' in txt
    assert 'netlab_health_state{category="network"} 2' in txt and 'netlab_health_state{category="overall"} 2' in txt
    assert 'netlab_ospf_adjacency_state{device="R1",neighbor="R2",area="0.0.0.0"} 8' in txt
    assert 'netlab_ospf_adjacency_state{device="R2",neighbor="R1",area="0.0.0.0"} 3' in txt
    assert 'netlab_ospf_dead_remaining_seconds{device="R1",neighbor="R2",area="0.0.0.0"} 31.0' in txt
    assert 'device="R2",neighbor="R1",area="0.0.0.0"} 31' not in txt                        # dead 無しは出さない
    assert 'netlab_ospf_spf_runs_total{device="R1",router_id="1.1.1.1"} 4' in txt
    assert 'netlab_alerts_total{rule="valhalla",severity="high",source="VALHALLA"} 2' in txt
    assert 'netlab_route_changes_total{device="R1",kind="add"} 5' in txt
    # HELP / TYPE が各メトリクスに1回ずつ
    assert txt.count('# TYPE netlab_link_state gauge') == 1 and txt.count('# TYPE netlab_alerts_total counter') == 1
    # OSPF 無効なら OSPF の行は出さない
    assert 'netlab_ospf' not in V.render_metrics([], None, {'enabled': False})


def test_metrics_endpoint_and_auth(monkeypatch):
    import app as app_module
    c = TestClient(app_module.app)
    r = c.get('/metrics')
    assert r.status_code == 200 and r.headers['content-type'].startswith('text/plain')
    assert '# TYPE netlab_link_state gauge' in r.text and 'netlab_health_state{category="overall"}' in r.text
    # 認証を有効にすると、トークン無しは 401、Bearer(NETLAB_METRICS_TOKEN)なら通る
    monkeypatch.setattr(app_module, '_AUTH_DISABLED', False)
    monkeypatch.setenv('NETLAB_METRICS_TOKEN', 's3cret')
    assert c.get('/metrics').status_code == 401
    assert c.get('/metrics', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert c.get('/metrics', headers={'Authorization': 'Bearer s3cret'}).status_code == 200


def test_valhalla_endpoints_without_prometheus(monkeypatch):
    import app as app_module
    monkeypatch.delenv('PROMETHEUS_URL', raising=False)
    c = TestClient(app_module.app)
    assert c.get('/api/valhalla/status').json()['configured'] is False
    assert c.get('/api/valhalla/replay').status_code == 404
    assert c.get('/api/valhalla/forecast').status_code == 404


# ── ② ─────────────────────────────────────────────
def test_replay_frames_and_state_events(prom):
    lab = {'a': 'r1', 'b': 'r2', 'host_a': 'R1', 'host_b': 'R2'}
    prom.ranges = [
        ('netlab_link_state', [_rv(lab, [(100, 0), (115, 3), (130, 3), (145, 0)])]),
        ('netlab_link_bps', [_rv({**lab, 'dir': 'ab'}, [(100, 5e6), (145, 4e6)]), _rv({**lab, 'dir': 'ba'}, [(100, 1e6)])]),
        ('netlab_link_util', [_rv(lab, [(100, 0.05)])]),
    ]
    d = V.replay_frames_lab(prom.url, 90, 150, 15)
    assert [f['t'] for f in d['frames']] == [100, 115, 130, 145]
    f0 = d['frames'][0]['links'][0]
    assert f0['state'] == 'ok' and f0['ab_bps'] == 5e6 and f0['ba_bps'] == 1e6 and f0['util'] == 0.05 and f0['host_b'] == 'R2'
    assert d['frames'][1]['links'][0]['down'] is True
    assert [(e['t'], e['from'], e['to']) for e in d['events']] == [(115, 'ok', 'down'), (145, 'down', 'ok')]
    assert d['events'][0]['text'] == 'R1 ⇄ R2: ok → down'


def test_replay_endpoint_uses_prometheus(prom, monkeypatch):
    import app as app_module
    monkeypatch.setenv('PROMETHEUS_URL', prom.url)
    prom.ranges = [('netlab_link_state', [_rv({'a': 'x', 'b': 'y'}, [(10, 1)])])]
    d = TestClient(app_module.app).get('/api/valhalla/replay?start=0&end=20&step=10').json()
    assert d['source'] == 'valhalla' and d['frames'][0]['links'][0]['state'] == 'warn'


# ── ③ ─────────────────────────────────────────────
def test_forecast_days_to_alert_and_zscore(prom):
    k1, k2 = {'a': 'r1', 'b': 'r2'}, {'a': 'r2', 'b': 'r3'}
    prom.instant = [
        ('deriv(', [_iv(k1, 0.1 / 86400), _iv(k2, -0.01 / 86400)]),           # 1日に +10% / 減少
        ('predict_linear(', [_iv(k1, 1.4), _iv(k2, -0.2)]),
        ('avg_over_time(', [_iv(k1, 0.1), _iv(k2, 0.3)]),
        ('stddev_over_time(', [_iv(k1, 0.05), _iv(k2, 0.0)]),
        ('max by', [_iv(k1, 0.4), _iv(k2, 0.3)]),
    ]
    rows = {(r['a'], r['b']): r for r in V.forecast(prom.url, 'max by (a, b) (netlab_link_util)', fit_window='15m')}
    r1, r2 = rows[('r1', 'r2')], rows[('r2', 'r3')]
    assert r1['days_to_alert'] == pytest.approx(4.0)              # (0.8 - 0.4) / 0.1
    assert r1['slope_per_day'] == pytest.approx(0.1)
    assert r1['z'] == pytest.approx(6.0)                          # (0.4 - 0.1) / 0.05
    assert r1['predicted'] == 1.0 and r2['predicted'] == 0.0      # 0..1 に収める
    assert r2['days_to_alert'] is None and r2['z'] is None        # 減っている / ばらつき0
    assert any(e.startswith('deriv((max by (a, b) (netlab_link_util))[15m:])') for e in prom.seen)


# ── ④ ─────────────────────────────────────────────
def test_ospf_mib_in_emulator_snmp_agent():
    """3台(R1─R2─R3)で OSPF を張り、SNMP エージェントの MIB に ospfRouterId と ospfNbrState(Full=8)が出る"""
    import app as app_module
    c = TestClient(app_module.app)
    import uuid
    s = uuid.uuid4().hex[:6]
    ids = [f'om{s}{i}' for i in (1, 2, 3)]
    for d in ids:
        c.post('/api/device', json={'id': d, 'type': 'cisco', 'hostname': d.upper()})
    c.post('/api/link', json={'a': ids[0], 'b': ids[1], 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    c.post('/api/link', json={'a': ids[1], 'b': ids[2], 'iface_a': 'GigabitEthernet0/0/1', 'iface_b': 'GigabitEthernet0/0/0'})

    def cli(d, *cmds):
        for cmd in cmds:
            c.post('/api/cli', json={'device_id': d, 'command': cmd})
    cli(ids[0], 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.233.12.1 255.255.255.0', 'no shutdown', 'end')
    cli(ids[1], 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.233.12.2 255.255.255.0', 'no shutdown',
        'exit', 'interface GigabitEthernet0/0/1', 'ip address 10.233.23.1 255.255.255.0', 'no shutdown', 'end')
    cli(ids[2], 'configure terminal', 'interface GigabitEthernet0/0/0', 'ip address 10.233.23.2 255.255.255.0', 'no shutdown', 'end')
    from types import SimpleNamespace as NS
    from engine.protocols import ospf_engine, snmp_agent
    for d in ids:
        cli(d, 'configure terminal', 'router ospf 1', 'network 10.233.0.0 0.0.255.255 area 0', 'end')
    # 隣接の成立は Hello 待ちなので、ここでは隣接を直接置いて MIB の組み立てだけを見る
    rid = {d: ospf_engine.nodes[d]['router_id'] for d in ids}
    ospf_engine.nodes[ids[1]]['neighbors'] = {ids[0]: NS(state='Full', router_id=rid[ids[0]], last_hello=None, role='DR'),
                                             ids[2]: NS(state='Init', router_id=rid[ids[2]], last_hello=None, role='DR'),
                                             '9.9.9.9': NS(state='Full', router_id='9.9.9.9', last_hello=None, role='DR')}
    mib = {o: v for o, _t, v in snmp_agent._build_mib(ids[1])}
    assert mib['1.3.6.1.2.1.14.1.1.0'] == rid[ids[1]] and mib['1.3.6.1.2.1.14.1.2.0'] == '1'
    # 近隣は「相手側の IF の IP」をインデックスにする(R2 から見て R1 は 10.233.12.1、R3 は 10.233.23.2)
    assert mib['1.3.6.1.2.1.14.10.1.6.10.233.12.1.0'] == '8'
    assert mib['1.3.6.1.2.1.14.10.1.6.10.233.23.2.0'] == '3'
    assert mib['1.3.6.1.2.1.14.10.1.3.10.233.12.1.0'] == rid[ids[0]]
    assert not any('9.9.9.9' in o for o in mib)      # 装置でないキー(Router-ID の重複登録)は出さない
    ospf_engine.nodes[ids[1]]['neighbors'] = {}
    for d in ids:
        cli(d, 'configure terminal', 'no router ospf 1', 'end')


def _sa():
    sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
    from cvs import config as sa_config, valhalla_views as VV
    from cvs.collector import classify
    cfg = sa_config.validate({
        'devices': [{'id': 'r1', 'hostname': 'R1', 'mgmt_ip': '10.9.0.1', 'interfaces': []},
                    {'id': 'r2', 'hostname': 'R2', 'mgmt_ip': '10.9.0.2', 'interfaces': []}],
        'links': [{'a': 'r1', 'a_if': 'GigabitEthernet0/0/0', 'b': 'r2', 'b_if': 'GigabitEthernet0/0/0', 'speed': 100_000_000}],
        'sources': {}})
    return cfg, VV, classify


def test_standalone_ospf_from_snmp_exporter(prom):
    cfg, VV, _ = _sa()
    prom.instant = [
        ('ospfRouterId', [_iv({'instance': '10.9.0.1', 'ospfRouterId': '1.1.1.1'}, 1),
                          _iv({'instance': '10.9.0.2', 'ospfRouterId': '2.2.2.2'}, 1)]),
        ('ospfNbrState', [_iv({'instance': '10.9.0.1', 'ospfNbrIpAddr': '10.9.0.2', 'ospfNbrRtrId': '2.2.2.2'}, 8),
                          _iv({'instance': '10.9.0.2', 'ospfNbrIpAddr': '10.9.0.1', 'ospfNbrRtrId': '1.1.1.1'}, 4)]),
    ]
    ip = {'10.9.0.1': 'r1', '10.9.0.2': 'r2'}
    d = VV.ospf(prom.url, 'cisco-snmp', cfg, lambda *v: next((ip[x] for x in v if x in ip), None))
    assert d['enabled'] and [x['router_id'] for x in d['devices']] == ['1.1.1.1', '2.2.2.2']
    a = d['adjacencies'][0]
    assert (a['state'], a['state_a'], a['state_b']) == ('partial', 'Full', 'TwoWay')
    assert d['tree'] is None and a['dead_a'] is None
    assert any(e == 'ospfNbrState{job="cisco-snmp"}' for e in prom.seen)


def test_standalone_replay_and_forecast_map_to_config_links(prom):
    cfg, VV, classify = _sa()
    a = {'instance': '10.9.0.1:161', 'ifName': 'Gi0/0/0'}
    b = {'instance': '10.9.0.2:161', 'ifName': 'Gi0/0/0'}
    prom.ranges = [
        ('rate(ifHCOutOctets', [_rv(a, [(100, 90e6), (115, 1e6)]), _rv(b, [(100, 1e6)])]),
        ('rate(ifHCInOctets', [_rv(a, [(100, 1e6)])]),
        ('ifOperStatus', [_rv(a, [(100, 1), (115, 2)]), _rv(b, [(100, 1), (115, 1)])]),
    ]
    d = VV.replay(prom.url, 'cisco-snmp', cfg, 90, 120, 15, classify)
    f0, f1 = d['frames'][0]['links'][0], d['frames'][1]['links'][0]
    assert f0['ab_bps'] == 90e6 and f0['util'] == pytest.approx(0.9) and f0['state'] == 'crit'
    assert f1['down'] is True and f1['state'] == 'down' and f1['iface_a'] == 'GigabitEthernet0/0/0'
    assert [(e['from'], e['to']) for e in d['events']] == [('crit', 'down')]
    # 予測: 両端の IF の大きい方をリンクの値にする
    prom.instant = [('max by', [_iv({'instance': '10.9.0.1:161', 'ifName': 'Gi0/0/0'}, 0.2),
                                _iv({'instance': '10.9.0.2:161', 'ifName': 'Gi0/0/0'}, 0.5),
                                _iv({'instance': '10.9.9.9:161', 'ifName': 'Gi0/0/0'}, 0.9)])]
    rows = VV.forecast(prom.url, 'cisco-snmp', cfg, fit_window='15m')
    assert len(rows) == 1 and rows[0]['util'] == 0.5 and (rows[0]['a'], rows[0]['b']) == ('r1', 'r2')


# ── ⑤ ─────────────────────────────────────────────
def _alert(name, sev='critical', **lab):
    return {'name': name, 'labels': {'alertname': name, 'severity': sev, **lab},
            'annotations': {'summary': f'{name} です'}, 'active_at': None, 'severity': sev}


def test_firing_alerts_only_firing(prom):
    prom.alerts = [{'state': 'firing', 'labels': {'alertname': 'A', 'severity': 'critical'}, 'annotations': {}},
                   {'state': 'pending', 'labels': {'alertname': 'B'}, 'annotations': {}}]
    got = V.firing_alerts(prom.url)
    assert [a['name'] for a in got] == ['A'] and got[0]['severity'] == 'critical'


def test_lab_imports_new_alerts_once_with_valhalla_petal():
    import app as app_module
    from engine.traffic_analytics import RULE_SOURCE
    app_module._valhalla_seen.clear()
    c = TestClient(app_module.app)
    c.post('/api/device', json={'id': 'vhx1', 'type': 'cisco', 'hostname': 'VHX1'})
    c.post('/api/device', json={'id': 'vhx2', 'type': 'cisco', 'hostname': 'VHX2'})
    al = [_alert('NirvanaLinkDown', host_a='VHX1', host_b='VHX2')]
    got = app_module._valhalla_import_alerts(al, now=1000.0)
    assert len(got) == 1 and got[0]['rule'] == 'valhalla' and got[0]['source'] == RULE_SOURCE['valhalla'] == 'VALHALLA'
    assert got[0]['severity'] == 'high' and got[0]['name'] == '刃流覇羅: NirvanaLinkDown'
    assert got[0]['src'] == 'vhx1' and got[0]['dst'] == 'vhx2'
    assert app_module._valhalla_import_alerts(al, now=1010.0) == []          # 発報し続けている間は二重にしない
    assert app_module._valhalla_import_alerts([], now=1020.0) == []          # 解消
    assert len(app_module._valhalla_import_alerts(al, now=1030.0)) == 1      # 再発したら再び
    app_module._valhalla_seen.clear()


def test_standalone_alert_importer():
    _, VV, _ = _sa()
    from cvs.analytics import Analytics
    an = Analytics()
    imp = VV.AlertImporter()
    devs = {'R1': 'r1', 'R2': 'r2'}
    dev_of = lambda *v: next((devs[x] for x in v if x in devs), None)   # noqa: E731
    al = [_alert('NirvanaHealthCritical', 'warning', device='R1')]
    got = imp.run(al, an, dev_of, now=1000.0)
    assert len(got) == 1 and got[0]['source'] == 'VALHALLA' and got[0]['severity'] == 'warning' and got[0]['src'] == 'r1'
    assert imp.run(al, an, dev_of, now=1010.0) == []


# ── 画面・コピーの同期・IaC ───────────────────────────
def test_page_has_replay_bar_forecast_and_valhalla_petal():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    for s in ('id="vhbar"', 'id="vhOpen"', '/api/valhalla/replay', '/api/valhalla/forecast', '/api/valhalla/status',
              "VALHALLA: { label: '刃流覇羅'", 'function applyFrame', 'いつもと違う', '予測 80%まで'):
        assert s in html, s


def test_standalone_copy_of_valhalla_is_identical():
    assert filecmp.cmp(os.path.join(ROOT, 'engine', 'valhalla.py'),
                       os.path.join(ROOT, 'cyber-view-standalone', 'cvs', 'valhalla.py'), shallow=False), \
        'engine/valhalla.py を直したら cyber-view-standalone/cvs/valhalla.py にもコピーする'


def test_iac_scrapes_nirvana_and_walks_ospf_mib():
    import yaml
    base = os.path.join(ROOT, 'iac', 'cisco-traffic-monitoring')
    prom_cfg = yaml.safe_load(open(os.path.join(base, 'prometheus', 'prometheus.yml')))
    job = next(j for j in prom_cfg['scrape_configs'] if j['job_name'] == 'netlab-nirvana')
    assert job['metrics_path'] == '/metrics' and job['authorization']['credentials_file'] == '/etc/prometheus/nirvana.token'
    rules = yaml.safe_load(open(os.path.join(base, 'prometheus', 'alert_rules.yml')))
    names = {r.get('alert') for g in rules['groups'] for r in g['rules']}
    assert {'NirvanaLinkDown', 'NirvanaHealthCritical', 'NirvanaLinkCapacityForecast', 'NirvanaOspfSpfChurn',
            'CiscoOspfNeighborNotFull'} <= names
    snmp = yaml.safe_load(open(os.path.join(base, 'snmp_exporter', 'snmp.yml')))
    mod = snmp['modules']['cisco_if']
    assert {m['name'] for m in mod['metrics']} >= {'ospfRouterId', 'ospfNbrState'}
