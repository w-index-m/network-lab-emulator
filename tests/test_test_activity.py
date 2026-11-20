"""
試験の様子(engine/test_activity.py、/api/testrun/*、/cyber-view の「試」プレートと TEST RUN パネル)

- 出来事の記録: kind / name の確かめ、run ごとの集計(probe / pass / fail / skip)、end、古いものを捨てる
- API: 1 件・リストで送れる、おかしいものは 400、since で差分が取れる
- シナリオ回帰の送り方: show / ping だけを probe として送り、チェックの合否に直前の装置を付ける
- 画面: パネル・プレート・単体版でも同じファイル
"""
import filecmp
import importlib
import os
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as app_module  # noqa: E402
from engine.test_activity import MAX_EVENTS, TestActivity, activity  # noqa: E402


def _sr():
    """tests/scenario_regression.py をモジュールとして読む(本番の送り先には一切つながない)"""
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    return importlib.import_module('scenario_regression')


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def test_record_counts_per_run_and_end():
    a = TestActivity()
    a.record({'kind': 'start', 'suite': 'scenario', 'run': 'r1', 'name': 'x'}, now=100)
    a.record({'kind': 'probe', 'run': 'r1', 'name': 'stp', 'device': 'sw1', 'command': 'show spanning-tree'}, now=101)
    a.record({'kind': 'pass', 'run': 'r1', 'name': 'ok', 'device': 'sw1'}, now=102)
    a.record({'kind': 'fail', 'run': 'r1', 'name': 'ng', 'detail': 'x' * 1000}, now=103)
    a.record({'kind': 'skip', 'name': 'later'}, now=104)                 # run 無し → 直近の run
    a.record({'kind': 'end', 'run': 'r1'}, now=105)
    cur = a.summary()['current']
    assert (cur['run'], cur['pass'], cur['fail'], cur['skip'], cur['probe'], cur['ended']) == ('r1', 1, 1, 1, 1, 105)
    assert len(a.since(0)[3]['detail']) == 300                            # 長いものは切る
    assert [e['seq'] for e in a.since(4)] == [5, 6]


def test_record_rejects_bad_events_and_keeps_only_recent():
    a = TestActivity()
    for bad in ({'kind': 'boom', 'name': 'x'}, {'kind': 'pass'}, 'text'):
        with pytest.raises(ValueError):
            a.record(bad)
    for i in range(MAX_EVENTS + 20):
        a.record({'kind': 'pass', 'name': f't{i}'})
    assert len(a.events) == MAX_EVENTS and a.events[-1]['name'] == f't{MAX_EVENTS + 19}'


def test_api_post_one_and_many_and_get_since(client):
    run = f'api-{uuid.uuid4().hex[:6]}'
    r = client.post('/api/testrun/event', json={'kind': 'start', 'suite': 'scenario', 'run': run, 'name': 's'})
    assert r.status_code == 200
    seq0 = r.json()['seq']
    r = client.post('/api/testrun/event', json=[
        {'kind': 'probe', 'run': run, 'name': 'ospf', 'device': 'r1', 'command': 'show ip route'},
        {'kind': 'fail', 'run': run, 'name': 'r1 に経路', 'device': 'r1', 'detail': 'no route'}])
    assert r.json()['recorded'] == 2
    d = client.get(f'/api/testrun/events?since={seq0}').json()
    assert [e['kind'] for e in d['events']] == ['probe', 'fail']
    assert d['current']['run'] == run and d['current']['fail'] == 1 and d['current']['probe'] == 1
    assert client.post('/api/testrun/event', json={'kind': 'nope', 'name': 'x'}).status_code == 400
    assert client.post('/api/testrun/event', json=[{'kind': 'pass', 'name': 'x'}] * 101).status_code == 400


def test_scenario_regression_reports_probes_and_results(monkeypatch):
    sr = _sr()
    sent = []

    def fake_post(path, body):
        sent.append((path, body))
        return {'output': ''}
    monkeypatch.setattr(sr, '_post', fake_post)
    monkeypatch.setattr(sr, '_REPORT', True)
    monkeypatch.setattr(sr, '_scenario', 'stp_triangle')
    monkeypatch.setattr(sr, '_fails', [])
    sr.cli('sw1', 'configure terminal')                                  # 設定行は probe にしない
    sr.cli('sw3', 'show spanning-tree')
    sr.check('SW3 にブロッキング', False, 'no BLK')
    evs = [b for p, b in sent if p == '/api/testrun/event']
    assert [e['kind'] for e in evs] == ['probe', 'fail']
    assert evs[0] == {'suite': 'scenario', 'run': sr._RUN, 'kind': 'probe', 'name': 'stp_triangle',
                      'device': 'sw3', 'command': 'show spanning-tree'}
    assert evs[1]['device'] == 'sw3' and evs[1]['detail'] == 'no BLK'


def test_scenario_regression_keeps_going_when_server_lacks_the_api(monkeypatch):
    sr = _sr()

    def fail_post(path, body):
        if path == '/api/testrun/event':
            raise OSError('404')
        return {'output': 'ok'}
    monkeypatch.setattr(sr, '_post', fail_post)
    monkeypatch.setattr(sr, '_REPORT', True)
    assert sr.cli('r1', 'show ip route') == 'ok'
    assert sr._REPORT is False                                           # 以降は送らない


def test_page_has_panel_and_plates_and_standalone_copy_matches():
    page = os.path.join(ROOT, 'static', 'cyber_view.html')
    with open(page, encoding='utf-8') as f:
        s = f.read()
    for needle in ('id="testpanel"', "'testpanel']", 'function pollTests', '/api/testrun/events?since=',
                   "'試')", "'TEST FAIL', e.name, 9000, '警'", "includes('404')) testOff = true", 'window.__cvTests'):
        assert needle in s, needle
    assert filecmp.cmp(page, os.path.join(ROOT, 'cyber-view-standalone', 'web', 'cyber_view.html'), shallow=False)


def test_module_singleton_is_shared_with_the_api(client):
    n = activity.seq
    client.post('/api/testrun/event', json={'kind': 'pass', 'name': 'shared'})
    assert activity.seq == n + 1
