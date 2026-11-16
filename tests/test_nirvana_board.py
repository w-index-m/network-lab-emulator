"""
ニルヴァーナのボード(Grafana が持つ機能: BOARD / EXPLORE / ALERTS)

- 内部の時系列と小さな PromQL 評価器(Prometheus が無くても動くように)
- ダッシュボードの保存・版の衝突・検証・Grafana の JSON の取り込み
- アラートルール(normal → pending → firing → normal)と webhook の通知(本物の HTTP で受ける)
- 注釈、内部ログ、データソース未設定時のエラー
- スタンドアロン版にも同じ API と画面のスクリプトがあること(コピーのずれも検出)
"""
import filecmp
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from engine import nirvana_board as NB

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _db():
    db = NB.InternalTSDB()
    for i in range(20):
        t = 1000 + i * 10
        db.ingest_text(f'''# HELP x
nb_bps{{a="r1",b="r2",dir="ab"}} {i * 100}
nb_bps{{a="r1",b="r2",dir="ba"}} 50
nb_bps{{a="r2",b="r3",dir="ab"}} 7
nb_state{{a="r1",b="r2"}} {3 if i >= 15 else 0}
nb_cnt{{k="x"}} {(i * 2) if i < 10 else (i - 10) * 2}
''', t)
    return db


def _last(db, expr, t=1190):
    return {tuple(sorted((k, v) for k, v in r['labels'].items() if k != '__name__')): r['points'][-1][1]
            for r in NB.internal_query_range(db, expr, t, t, 10)}


# ── 小さな PromQL ──────────────────────────────────
def test_promql_selectors_aggregation_and_binops():
    db = _db()
    assert _last(db, 'nb_bps{a="r1",dir="ab"}') == {(('a', 'r1'), ('b', 'r2'), ('dir', 'ab')): 1900.0}
    assert len(_last(db, 'nb_bps{dir!="ab"}')) == 1 and len(_last(db, 'nb_bps{b=~"r[23]"}')) == 3
    assert _last(db, 'sum by (a) (nb_bps)') == {(('a', 'r1'),): 1950.0, (('a', 'r2'),): 7.0}
    assert _last(db, 'sum without (dir) (nb_bps)')[(('a', 'r1'), ('b', 'r2'))] == 1950.0
    assert _last(db, 'max(nb_bps) / 1000') == {(): 1.9}
    assert _last(db, 'count(nb_state == 3)') == {(): 1.0}
    assert _last(db, 'count(nb_state == 3)', t=1100) == {}          # その時点では断なし
    assert list(_last(db, 'topk(1, nb_bps)').values()) == [1900.0]
    assert _last(db, 'nb_bps{dir="ab"} > 100') == {(('a', 'r1'), ('b', 'r2'), ('dir', 'ab')): 1900.0}
    # ベクトル同士(ラベルが一致するものだけ)
    assert _last(db, 'nb_bps{dir="ab"} - nb_bps{dir="ab"}')[(('a', 'r1'), ('b', 'r2'), ('dir', 'ab'))] == 0.0
    assert _last(db, '-1 * abs(nb_state - 5)') == {(('a', 'r1'), ('b', 'r2')): -2.0}


def test_promql_range_functions_and_counter_reset():
    db = _db()
    # nb_cnt は 0,2,..,18 → 0,2,.. とリセット。rate はリセットを跨いでも増加分だけ数える
    r = _last(db, 'increase(nb_cnt[1m])', t=1110)       # 1060..1110: 12,14,16,18 → 0,2 = 6+2
    assert list(r.values())[0] == pytest.approx(8 * 60 / 50)
    assert list(_last(db, 'rate(nb_bps{dir="ab",a="r1"}[1m])').values())[0] == pytest.approx(10.0)
    assert list(_last(db, 'max_over_time(nb_state[5m])').values())[0] == 3.0
    assert list(_last(db, 'avg_over_time(nb_bps{dir="ba"}[1m])').values())[0] == 50.0
    for bad in ('rate(nb_cnt)', 'nb_bps{a=="x"}', 'sum(', 'nb_bps[5m]', '1 +'):
        with pytest.raises(NB.QueryError):
            NB.internal_query_range(db, bad, 1000, 1010, 10)


def test_durations_times_retention_and_labels():
    assert NB.parse_duration('1h30m') == 5400 and NB.parse_duration('90') == 90
    with pytest.raises(ValueError):
        NB.parse_duration('5x')
    assert NB.parse_time('now-5m', now=1000) == 700 and NB.parse_time('123.5') == 123.5
    db = NB.InternalTSDB(retention=50)
    for t in range(0, 200, 10):
        db.add('m', {'x': '1'}, t, t)
    assert db.select('m', [])[0][1][0][0] >= 140                    # 古い点は捨てる
    db = _db()
    assert db.label_values('a', 'nb_bps') == ['r1', 'r2'] and 'nb_state' in db.names()


# ── API(ラボ版) ──────────────────────────────────
@pytest.fixture
def board(tmp_path):
    import app as app_module
    st = app_module._board_store
    old_path, old_data = st.path, st.data
    st.path = str(tmp_path / 'board.json')
    st.data = {'dashboards': {}, 'alert_rules': {}, 'contact_points': {}, 'annotations': {}}
    app_module._board_eval.instances.clear()
    app_module._board_eval.history.clear()
    c = TestClient(app_module.app)
    yield c, app_module
    st.path, st.data = old_path, old_data


def test_datasources_query_logs_and_errors(board, monkeypatch):
    c, app_module = board
    monkeypatch.delenv('PROMETHEUS_URL', raising=False)
    monkeypatch.delenv('LOKI_URL', raising=False)
    d = c.get('/api/nirvana/datasources').json()
    assert d['default'] == 'internal' and {x['uid']: x['available'] for x in d['datasources']} == \
        {'internal': True, 'prometheus': False, 'loki': False}
    import time
    now = time.time()
    for i in range(6):
        app_module._board_tsdb.add('nbtest_metric', {'dev': 'x'}, i, now - 50 + i * 10)
    q = c.get('/api/nirvana/query', params={'ds': 'internal', 'expr': 'nbtest_metric * 2', 'start': 'now-2m'}).json()
    assert q['series'][0]['points'][-1][1] == 10.0
    assert c.get('/api/nirvana/query', params={'ds': 'internal', 'expr': 'sum('}).status_code == 400
    r = c.get('/api/nirvana/query', params={'ds': 'prometheus', 'expr': 'up'})
    assert r.status_code == 400 and 'PROMETHEUS_URL' in r.json()['error']
    assert c.get('/api/nirvana/query_instant', params={'ds': 'internal', 'expr': 'nbtest_metric'}).json()['result'][0]['value'] == 5.0
    assert 'x' in c.get('/api/nirvana/labels', params={'ds': 'internal', 'label': 'dev', 'metric': 'nbtest_metric'}).json()['values']
    app_module._flow_recorder.record_log('nbdev', 'NB-DEV', 3, '%LINK-3-UPDOWN: Interface Gi0/0/9, changed state to down')
    logs = c.get('/api/nirvana/logs', params={'ds': 'internal', 'expr': 'Gi0/0/9'}).json()['logs']
    assert logs and logs[0]['labels']['host'] == 'NB-DEV' and logs[0]['labels']['level'] == 'error'


def test_dashboard_crud_versioning_and_validation(board):
    c, _ = board
    d = {'title': 'テスト', 'panels': [{'id': 1, 'type': 'stat', 'title': 'x', 'gridPos': {'x': 0, 'y': 0, 'w': 4, 'h': 3},
                                       'targets': [{'expr': 'up'}]}], 'variables': [], 'time': {'from': 'now-1h', 'to': 'now'}}
    r = c.post('/api/nirvana/dashboards', json=d).json()
    uid = r['uid']
    assert r['version'] == 1
    got = c.get(f'/api/nirvana/dashboards/{uid}').json()
    got['title'] = '改名'
    assert c.put(f'/api/nirvana/dashboards/{uid}', json=got).json()['version'] == 2
    assert c.put(f'/api/nirvana/dashboards/{uid}', json=got).status_code == 409     # 古い版で上書きしない
    assert [x['title'] for x in c.get('/api/nirvana/dashboards').json()['dashboards']] == ['改名']
    for bad in ({'title': ''}, {'title': 'a', 'panels': [{'id': 1, 'type': 'pie', 'gridPos': {'x': 0, 'y': 0, 'w': 1, 'h': 1}}]},
                {'title': 'a', 'panels': [{'id': 1, 'type': 'stat'}]},
                {'title': 'a', 'variables': [{'name': '1bad'}]}, {'title': 'a', 'time': {'from': 'yesterday'}}):
        assert c.post('/api/nirvana/dashboards', json=bad).status_code == 400, bad
    assert c.delete(f'/api/nirvana/dashboards/{uid}').json()['ok'] is True
    assert c.get(f'/api/nirvana/dashboards/{uid}').status_code == 404


def test_import_grafana_dashboard_json(board):
    """iac の Grafana ダッシュボードをそのまま取り込める(24列→12列、fieldConfig、templating)"""
    c, _ = board
    gf = json.load(open(os.path.join(ROOT, 'iac', 'cisco-traffic-monitoring', 'grafana', 'dashboards', 'cisco-traffic.json'), encoding='utf-8'))
    d = c.post('/api/nirvana/dashboards/import', json=gf).json()
    assert d['uid'] == gf['uid'] and len(d['panels']) == len([p for p in gf['panels'] if p['type'] != 'row'])
    assert all(p['gridPos']['x'] + p['gridPos']['w'] <= 12 for p in d['panels'])
    assert {p['ds'] for p in d['panels']} == {'prometheus'}
    ts = next(p for p in d['panels'] if p['type'] == 'timeseries')
    assert ts['options']['unit'] == 'bps' and '$__rate_interval' in ts['targets'][0]['expr']
    assert [v['name'] for v in d['variables']] == ['router', 'ifname'] and d['variables'][0]['current'] == ['$__all']
    assert d['variables'][0]['query'].startswith('label_values(')
    assert c.post('/api/nirvana/dashboards/import', json=gf).status_code == 409               # 既にある
    assert c.post('/api/nirvana/dashboards/import?overwrite=true', json={'dashboard': gf}).status_code == 200


class _Hook:
    def __init__(self):
        self.got = []
        me = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                me.got.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_port}/hook'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_alert_rule_lifecycle_webhook_and_nirvana_alert(board):
    c, app_module = board
    hook = _Hook()
    try:
        cp = c.post('/api/nirvana/contact-points', json={'name': '試験', 'url': hook.url}).json()
        assert c.post(f"/api/nirvana/contact-points/{cp['uid']}/test").json()['status'] == 200
        assert hook.got[-1]['status'] == 'firing' and 'ニルヴァーナ' in hook.got[-1]['title']
        assert c.post('/api/nirvana/contact-points', json={'name': 'x', 'url': 'ftp://a'}).status_code == 400
        rule = {'title': 'NB試験断', 'ds': 'internal', 'expr': 'nbrule_state', 'reducer': 'last', 'op': '>=', 'threshold': 3,
                'window': '1m', 'for': '30s', 'severity': 'critical', 'contacts': [cp['uid']]}
        assert c.post('/api/nirvana/alert-rules', json={**rule, 'op': '=>'}).status_code == 400
        assert c.post('/api/nirvana/alert-rules', json={**rule, 'expr': 'sum('}).status_code == 400
        uid = c.post('/api/nirvana/alert-rules', json=rule).json()['uid']
        ev = app_module._board_eval
        ev.sources = app_module._board_sources()
        db = app_module._board_tsdb
        import time
        t0 = time.time()
        db.add('nbrule_state', {'host_a': 'NBX', 'host_b': 'NBY'}, 3, t0)
        ev.evaluate(now=t0)
        st = {i['key']: i['state'] for i in ev.states() if i['rule'] == uid}
        assert list(st.values()) == ['pending']                       # for 30s の間は保留
        assert len(hook.got) == 1
        db.add('nbrule_state', {'host_a': 'NBX', 'host_b': 'NBY'}, 3, t0 + 31)
        ev.evaluate(now=t0 + 31)
        assert [i['state'] for i in ev.states() if i['rule'] == uid] == ['firing']
        assert hook.got[-1]['status'] == 'firing' and hook.got[-1]['alerts'][0]['labels']['alertname'] == 'NB試験断'
        al = [a for a in app_module._flow_recorder.alerts if a['rule'] == 'nirvana_rule' and 'NB試験断' in a['name']]
        assert al and al[-1]['source'] == 'RULE' and al[-1]['severity'] == 'high'
        db.add('nbrule_state', {'host_a': 'NBX', 'host_b': 'NBY'}, 0, t0 + 40)
        ev.evaluate(now=t0 + 40)
        assert [i['state'] for i in ev.states() if i['rule'] == uid] == ['normal']
        assert hook.got[-1]['status'] == 'resolved'
        hist = c.get('/api/nirvana/alert-state').json()['history']
        assert [(h['from'], h['to']) for h in hist if h['rule'] == uid] == [('normal', 'pending'), ('pending', 'firing'), ('firing', 'normal')]
        prev = c.post('/api/nirvana/alert-rules/preview', json={**rule, 'threshold': -1}).json()['results']
        assert prev and all(r['firing'] for r in prev)
        # 注釈: ルールの状態変化と利用者の注釈
        a = c.post('/api/nirvana/annotations', json={'text': '作業開始', 'tags': ['ops']}).json()
        anns = c.get('/api/nirvana/annotations', params={'start': 'now-1h', 'end': str(t0 + 120)}).json()['annotations']
        assert any(x['kind'] == 'user' and x['text'] == '作業開始' for x in anns)
        assert any(x['kind'] == 'rule' and 'NB試験断' in x['text'] for x in anns)
        assert c.post('/api/nirvana/annotations', json={'text': ' '}).status_code == 400
        assert c.delete(f"/api/nirvana/annotations/{a['uid']}").json()['ok']
        assert c.delete(f'/api/nirvana/alert-rules/{uid}').json()['ok']
    finally:
        hook.srv.shutdown()


def test_rule_with_unreachable_source_reports_error(board, monkeypatch):
    c, app_module = board
    monkeypatch.delenv('PROMETHEUS_URL', raising=False)
    uid = c.post('/api/nirvana/alert-rules', json={'title': 'P', 'ds': 'prometheus', 'expr': 'up', 'threshold': 0}).json()['uid']
    app_module._board_eval.sources = app_module._board_sources()
    app_module._board_eval.evaluate()
    assert 'PROMETHEUS_URL' in c.get('/api/nirvana/alert-rules').json()['errors'][uid]


def test_default_dashboard_uses_metrics_from_metrics_endpoint():
    d = NB.default_dashboard()
    exprs = ' '.join(t['expr'] for p in d['panels'] for t in p['targets'])
    src = open(os.path.join(ROOT, 'engine', 'valhalla.py'), encoding='utf-8').read()
    import re
    names = set(re.findall(r"head\('(netlab_\w+)'", src))
    used = set(re.findall(r'\b(netlab_\w+)', exprs))
    assert used and used <= names, used - names
    NB.validate_dashboard(d)


# ── 画面・スタンドアロン版 ─────────────────────────────
def test_page_has_board_modes_and_script():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    for s in ('id="mBoard"', 'id="mExplore"', 'id="mAlerts"', '<script src="nirvana-board.js">', 'window.__cvBridge',
              "RULE: { label: 'RULES'"):
        assert s in html, s
    js = open(os.path.join(ROOT, 'static', 'nirvana_board.js'), encoding='utf-8').read()
    for s in ('/api/nirvana/dashboards', '/api/nirvana/alert-rules', '/api/nirvana/annotations', 'function drawTimeseries',
              'function drawGauge', 'function drawHeatmap', 'bindDrag', '$__rate_interval', 'window.NirvanaBoard'):
        assert s in js, s


def test_lab_serves_board_script(board):
    c, _ = board
    r = c.get('/nirvana-board.js')
    assert r.status_code == 200 and 'javascript' in r.headers['content-type'] and 'NirvanaBoard' in r.text


def test_standalone_copies_and_routes(tmp_path, monkeypatch):
    for a, b in (('engine/nirvana_board.py', 'cyber-view-standalone/cvs/nirvana_board.py'),
                 ('engine/nirvana_board_api.py', 'cyber-view-standalone/cvs/nirvana_board_api.py'),
                 ('static/nirvana_board.js', 'cyber-view-standalone/web/nirvana_board.js'),
                 ('static/cyber_view.html', 'cyber-view-standalone/web/cyber_view.html')):
        assert filecmp.cmp(os.path.join(ROOT, a), os.path.join(ROOT, b), shallow=False), f'{a} を {b} にコピーし直す'
    sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
    import server as sa_server
    from cvs import config as sa_config
    monkeypatch.setenv('NIRVANA_BOARD_PATH', str(tmp_path / 'b.json'))
    cfg = sa_config.validate({'devices': [{'id': 'a'}], 'links': [], 'sources': {}})
    c = TestClient(sa_server.create_app(cfg, start_collector=False))
    assert c.get('/api/nirvana/datasources').json()['default'] == 'internal'
    assert c.get('/nirvana-board.js').status_code == 200
    d = c.post('/api/nirvana/dashboards', json={'title': 'sa', 'panels': []}).json()
    assert os.path.exists(tmp_path / 'b.json') and d['version'] == 1
