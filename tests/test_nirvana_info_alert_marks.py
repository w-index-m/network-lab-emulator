"""どの経路から入った変化も「情」「注」「警」として画面に出ること

- 単体版: リンク断は「警」(link_down)、復旧は「情」(link_up)。ヴァルハラで応答しなくなった装置は「警」(snmp_lost)、
  戻ったら「情」(snmp_ok)。どちらも遷移のときに1回だけ
- ラボ版: shutdown → no shutdown の復旧が「情」になる。ヴァルハラの応答なしの遷移も同じ
- 画面: 新しい「情」の種類の表示と、リンク断・応答なしの「警」/ 復旧の「情」でリンク図をすぐ取り直すこと
- `no access-list <番号>` で ACL を丸ごと消せること(以前は黙って無視され、作り直す行が古い ACL に追記されていた)

実際の画面は docs/nirvana-introduction.md(本物の docker のスタックで撮影)。
"""
import os
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
import app as A  # noqa: E402
from test_cyber_view_standalone import Fake, _cfg, _prom_row  # noqa: E402

import server as sa_server  # noqa: E402


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.srv.shutdown()


def _kinds(col, since=0):
    return [e['kind'] for e in col.snmp.watch.since(since, 500)]


def _rules(col):
    return [a['rule'] for a in col.analytics.alerts]


def test_standalone_link_down_alert_then_link_up_info(fake):
    up = [{'metric': {'instance': '10.9.0.1'}, 'value': [0, '1']}, {'metric': {'instance': '10.9.0.2'}, 'value': [0, '1']}]
    fake.prom = {'ifOperStatus': [_prom_row('10.9.0.1', 'Gi0/0/0', 2), _prom_row('10.9.0.2', 'Gi0/0/0', 2)],
                 'up{': up, 'ifHC': []}
    app = sa_server.create_app(_cfg({'prometheus': {'url': fake.url, 'job': 'snmp'}}), start_collector=False)
    col = app.state.collector
    col.tick()
    assert _rules(col).count('link_down') == 1 and 'link_up' not in _kinds(col)
    col.tick()                                                   # 断のままなら繰り返さない
    assert _rules(col).count('link_down') == 1
    fake.prom['ifOperStatus'] = [_prom_row('10.9.0.1', 'Gi0/0/0', 1), _prom_row('10.9.0.2', 'Gi0/0/0', 1)]
    col._cache.clear()                                           # 取得結果の短い間の使い回しを捨てる(次の周期に相当)
    col.tick()
    ev = [e for e in col.snmp.watch.since(0, 500) if e['kind'] == 'link_up']
    assert len(ev) == 1 and ev[0]['code'] == 'LINK' and 'R2' in ev[0]['prefix']
    r = TestClient(app).get('/api/traffic/routes?since=0').json()     # 画面はこの流れから「情」を出す
    assert any(e['kind'] == 'link_up' for e in r['events'])


def test_standalone_snmp_lost_alert_then_ok_info(fake):
    fake.prom = {'up{': [{'metric': {'instance': '10.9.0.1'}, 'value': [0, '1']},
                         {'metric': {'instance': '10.9.0.2'}, 'value': [0, '0']}], 'ifHC': []}
    app = sa_server.create_app(_cfg({'prometheus': {'url': fake.url, 'job': 'snmp'}}), start_collector=False)
    col = app.state.collector
    col.tick()
    col.tick()
    lost = [a for a in col.analytics.alerts if a['rule'] == 'snmp_lost']
    assert len(lost) == 1 and lost[0]['src'] == 'r2' and lost[0]['source'] == 'NET' and lost[0]['severity'] == 'medium'
    fake.prom['up{'][1]['value'] = [0, '1']
    col._cache.clear()
    col.tick()
    assert _kinds(col).count('snmp_ok') == 1


def _dev(c, typ='cisco'):
    did = f'nim-{uuid.uuid4().hex[:6]}'
    c.post('/api/device', json={'id': did, 'type': typ, 'hostname': did.upper()})
    return did


def _cli(c, did, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': did, 'command': cmd}).json()['output']
    return out


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def test_lab_no_shutdown_gives_link_up_info(client):
    from engine.route_watch import watcher
    a, b = _dev(client), _dev(client)
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet0/0/1', 'iface_b': 'GigabitEthernet0/0/1'})
    s0 = watcher.seq
    _cli(client, a, 'configure terminal', 'interface GigabitEthernet0/0/1', 'shutdown')
    assert not [e for e in watcher.since(s0, 500) if e['kind'] == 'link_up' and e['dev'] == a]
    _cli(client, a, 'no shutdown', 'end')
    ev = [e for e in watcher.since(s0, 500) if e['kind'] == 'link_up' and e['dev'] == a]
    assert len(ev) == 1 and ev[0]['code'] == 'LINK' and b.upper() in ev[0]['prefix']


def test_lab_snmp_reachability_transitions():
    from engine.route_watch import watcher
    nodes = {'x1': {'hostname': 'X1'}}
    A._snmp_lost_prev.clear()
    n0 = len([x for x in A._flow_recorder.alerts if x['rule'] == 'snmp_lost'])
    A._note_snmp_reachability({'x1': '10.0.0.1'}, nodes, time.time())
    A._note_snmp_reachability({'x1': '10.0.0.1'}, nodes, time.time())        # 続いている間は1回だけ
    assert len([x for x in A._flow_recorder.alerts if x['rule'] == 'snmp_lost']) == n0 + 1
    s0 = watcher.seq
    A._note_snmp_reachability({}, nodes, time.time())
    assert [e['kind'] for e in watcher.since(s0, 50) if e['dev'] == 'x1'] == ['snmp_ok']


def test_no_access_list_deletes_whole_acl(client):
    from engine.protocols import ipfilter_engine
    d = _dev(client)
    _cli(client, d, 'configure terminal', 'access-list 20 permit 10.240.0.0 0.0.255.255', 'access-list 20 permit host 10.99.0.9')
    assert len(ipfilter_engine.acls[d]['20']) == 2
    _cli(client, d, 'no access-list 20', 'access-list 20 permit host 10.99.0.9', 'end')
    assert [r.src for r in ipfilter_engine.acls[d]['20']] == ['host 10.99.0.9']
    assert not ipfilter_engine.check_source(d, '20', '10.240.12.2')
    _cli(client, d, 'configure terminal', 'ip access-list standard MGMT', 'permit host 10.1.1.1', 'exit',
         'no ip access-list standard MGMT', 'end')
    assert 'MGMT' not in ipfilter_engine.acls[d]


def test_screen_shows_new_marks_and_refreshes_links():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert "link_up: ['#3dff9a', 'INFO LINK UP']" in html and "snmp_ok: ['#3dff9a', 'INFO SNMP OK']" in html
    assert "snmp_lost: 'SNMP'" in html
    assert "new Set(['link_down', 'snmp_lost'])" in html                # 「警」でリンク図をすぐ取り直す
    assert "e.kind === 'link_up' || e.kind === 'snmp_ok'" in html       # 復旧の「情」でも
    assert 'Math.min(10, w)' in html                                     # 断の間は10秒ごと
