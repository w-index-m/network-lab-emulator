"""
L2 ループの検知・嵐・止め方(engine/l2_loop.py)と、/cyber-view 用 API・RESPONSE・保存

- スイッチ3台の輪(STP なし)→ 警報 l2_loop・MAC フラップ・ストーム(カウンタと CPU)・状態ボード異常
- 全台 STP → ループにしない
- storm-control: level だけなら捨てるだけ(ループは続く)、action shutdown なら err-disable で止まる
- errdisable recovery で戻り、原因が残っていればまた止まる
- BPDU guard(STP を動かす相手から BPDU)・APRESIA loopdetect(LBD)
- 手動の shutdown → no shutdown で err-disable が解ける
- RESPONSE の「ループ遮断」で止まり、何で止まったかが残る
- 設定の running-config・保存・復元
"""
import asyncio
import json
import os
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as A  # noqa: E402
from engine import l2_loop  # noqa: E402
from engine.l2_loop import loops, find_rings  # noqa: E402

G = ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2']
P = ['Port1/0/1', 'Port1/0/2']


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


@pytest.fixture(autouse=True)
def _fresh():
    loops.reset()
    yield
    loops.reset()


@pytest.fixture(scope='module')
def ev():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


def _cli(c, d, *cmds):
    out = ''
    for x in cmds:
        out = c.post('/api/cli', json={'device_id': d, 'command': x}).json().get('output', '')
    return out


def _ring(c, kind='catalyst', ifs=G, close=True, n=3):
    tag = uuid.uuid4().hex[:5]
    ids = [f'l2-{tag}-{i}' for i in range(n)]
    for i, d in enumerate(ids):
        c.post('/api/device', json={'id': d, 'type': kind, 'hostname': f'SW{i + 1}-{tag}'})
    for i in range(n if close else n - 1):
        c.post('/api/link', json={'a': ids[i], 'b': ids[(i + 1) % n], 'iface_a': ifs[1], 'iface_b': ifs[0]})
    return ids


def _close(c, ids, ifs=G):
    c.post('/api/link', json={'a': ids[-1], 'b': ids[0], 'iface_a': ifs[1], 'iface_b': ifs[0]})


def _tick(ev, t):
    return [a for a in ev.run_until_complete(A._l2loop_tick(t)) if a['do'] not in ('storm', 'cpu')]


def _mine(acts, ids):
    return [a for a in acts if a.get('dev') in ids or a.get('src') in ids or
            (a['do'] == 'cleared' and set(a['loop']['devices']) & set(ids))]


def _logs(ids):
    return [x['msg'] for x in A._flow_recorder.logs if x['dev'] in ids]


def test_find_rings_ignores_trees_and_counts_rings():
    class V:
        def __init__(self, edges):
            self.links = {}
            for a, b in edges:
                self.links.setdefault(a, set()).add(b)
                self.links.setdefault(b, set()).add(a)

        def _edge_up(self, a, b):
            return True
    tree = V([('a', 'b'), ('b', 'c'), ('c', 'd')])
    types = {k: 'catalyst' for k in 'abcd'}
    assert find_rings(tree, types, lambda d: False) == []
    two = V([('a', 'b'), ('b', 'c'), ('c', 'a'), ('c', 'd'), ('d', 'e'), ('e', 'f'), ('f', 'd')])
    rs = find_rings(two, {k: 'catalyst' for k in 'abcdef'}, lambda d: False)
    assert sorted(r['devices'] for r in rs) == [['a', 'b', 'c'], ['d', 'e', 'f']]   # 橋 c-d は含めない
    assert find_rings(two, {k: 'cisco' for k in 'abcdef'}, lambda d: False) == []    # ルータは L2 ループにならない
    assert l2_loop.ring_order([('a', 'b'), ('b', 'c'), ('a', 'c')]) == ['a', 'b', 'c']


def test_ring_without_stp_is_a_loop_with_storm_alert_and_health(client, ev):
    ids = _ring(client, close=False)
    t = time.time()
    assert not _mine(_tick(ev, t), ids)
    _close(client, ids)
    before = A.dp_engine.get_counter(ids[0], G[0])['in_bytes']
    acts = _mine(_tick(ev, t + 1), ids)
    al = [a for a in acts if a['do'] == 'alert']
    assert len(al) == 1 and 'L2ループ発生' in al[0]['detail'] and 'STP なし' in al[0]['detail']
    assert sum('%SW_MATM-4-MACFLAP_NOTIF' in a.get('msg', '') for a in acts) == 3
    assert A.dp_engine.get_counter(ids[0], G[0])['in_bytes'] - before >= l2_loop.STORM_PPS * 64 * 0.99
    assert any(a['rule'] == 'l2_loop' and a['source'] == 'LOOP' for a in A._flow_recorder.alerts)
    d = client.get('/api/traffic/loops').json()
    lp = next(x for x in d['active'] if ids[0] in x['devices'])
    assert len(lp['edges']) == 3 and lp['order'] is not None
    assert 'loops' in client.get('/api/traffic/links').json()
    net = client.get('/api/traffic/health').json()['categories'][0]
    assert net['state'] == 'crit' and 'L2ループ' in net['summary']
    # 2回目の tick では警報を出し直さない(1つのループに1件)
    assert not [a for a in _mine(_tick(ev, t + 2), ids) if a['do'] == 'alert']


def test_all_stp_is_not_a_loop(client, ev):
    ids = _ring(client)
    for d in ids:
        _cli(client, d, 'configure terminal', 'spanning-tree mode rapid-pvst', 'end')
    _tick(ev, time.time())
    assert not [x for x in loops.active.values() if ids[0] in x['devices']]


def test_storm_control_level_only_filters_and_loop_continues(client, ev):
    ids = _ring(client, close=False)
    _cli(client, ids[1], 'configure terminal', f'interface {G[1]}', 'storm-control broadcast level 5', 'end')
    _close(client, ids)
    t = time.time()
    _tick(ev, t)
    acts = _mine(_tick(ev, t + 2), ids)
    assert any('%STORM_CONTROL-3-FILTERED' in a.get('msg', '') for a in acts)
    assert not [a for a in acts if a['do'] == 'errdisable']
    assert [x for x in loops.active.values() if ids[0] in x['devices']]
    assert 'Blocking' in _cli(client, ids[1], 'show storm-control')


def test_storm_control_shutdown_errdisables_then_recovery_and_redetect(client, ev):
    ids = _ring(client, close=False)
    _cli(client, ids[2], 'configure terminal', f'interface {G[1]}', 'storm-control broadcast level 10',
         'storm-control action shutdown', 'exit', 'errdisable recovery cause storm-control',
         'errdisable recovery interval 30', 'end')
    rc = _cli(client, ids[2], 'show running-config')
    for line in ('errdisable recovery cause storm-control', 'errdisable recovery interval 30',
                 ' storm-control broadcast level 10.00', ' storm-control action shutdown'):
        assert line in rc
    _close(client, ids)
    t = time.time()
    _tick(ev, t)
    acts = _mine(_tick(ev, t + 1.5), ids)
    ed = [a for a in acts if a['do'] == 'errdisable']
    assert ed and ed[0]['dev'] == ids[2] and ed[0]['cause'] == 'storm-control'
    logs = _logs([ids[2]])
    assert any('%STORM_CONTROL-3-SHUTDOWN' in m for m in logs)
    assert any('%PM-4-ERR_DISABLE: storm-control error detected on GigabitEthernet1/0/2' in m for m in logs)
    assert any('%LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down' in m for m in logs)
    st = A.device_sessions[ids[2]]
    assert st.interfaces[G[1]]['status'] == 'err-disabled'
    assert 'err-disabled' in _cli(client, ids[2], 'show interfaces status err-disabled')
    assert 'Shutdown' in _cli(client, ids[2], 'show storm-control')
    assert 'storm-control       ' in _cli(client, ids[2], 'show errdisable recovery')
    cl = [a for a in _mine(_tick(ev, t + 2.5), ids) if a['do'] == 'cleared']
    assert cl and 'storm-control で遮断' in cl[0]['loop']['resolved_by']
    # 30秒後に自動で戻る → 原因の輪が残っているのでまたループ → また止まる
    acts = _mine(_tick(ev, t + 40), ids)
    assert any('%PM-4-ERR_RECOVER' in a.get('msg', '') for a in acts)
    assert st.interfaces[G[1]]['status'] != 'err-disabled'
    acts = _mine(_tick(ev, t + 41), ids) + _mine(_tick(ev, t + 42.5), ids)
    assert any(a['do'] == 'alert' for a in acts) and any(a['do'] == 'errdisable' for a in acts)


def test_bpduguard_from_stp_neighbor(client, ev):
    ids = _ring(client, close=False)
    _cli(client, ids[0], 'configure terminal', 'spanning-tree mode rapid-pvst', 'end')
    _cli(client, ids[1], 'configure terminal', f'interface {G[0]}', 'spanning-tree bpduguard enable', 'end')
    _close(client, ids)
    t = time.time()
    _tick(ev, t)
    acts = _mine(_tick(ev, t + 1.5), ids)
    assert any('%SPANTREE-2-BLOCK_BPDUGUARD: Received BPDU on port GigabitEthernet1/0/1' in a.get('msg', '') for a in acts)
    ed = [a for a in acts if a['do'] == 'errdisable']
    assert ed and ed[0]['dev'] == ids[1] and ed[0]['cause'] == 'bpduguard'


def test_manual_shutdown_no_shutdown_clears_errdisable(client, ev):
    ids = _ring(client, close=False)
    _cli(client, ids[2], 'configure terminal', f'interface {G[1]}', 'storm-control broadcast level 10',
         'storm-control action shutdown', 'end')
    _close(client, ids)
    t = time.time()
    _tick(ev, t)
    _tick(ev, t + 1.5)
    assert loops.port_state(ids[2], G[1])
    _cli(client, ids[2], 'configure terminal', f'interface {G[1]}', 'shutdown', 'no shutdown', 'end')
    assert not loops.port_state(ids[2], G[1])


def test_apresia_loopdetect_blocks_and_recovers(client, ev):
    ids = _ring(client, kind='apresia', ifs=P, close=False)
    assert 'Success.' in _cli(client, ids[0], 'configure', 'enable loopdetect', 'config loopdetect interval 3',
                              'config loopdetect recover_timer 60')
    _close(client, ids, P)
    t = time.time()
    _tick(ev, t)
    assert not [a for a in _mine(_tick(ev, t + 2), ids) if a['do'] == 'errdisable']    # interval 前は止めない
    acts = _mine(_tick(ev, t + 3.5), ids)
    assert any(a.get('msg') == 'Port Port1/0/2 LBD loop occurred. Port blocked' for a in acts)
    sh = _cli(client, ids[0], 'show loopdetect')
    assert 'LBD Status            : Enabled' in sh and 'Loop!' in sh
    assert sh.index('Port1/0/2 ') < sh.index('Port1/0/10')                                 # 自然順
    cl = [a for a in _mine(_tick(ev, t + 4), ids) if a['do'] == 'cleared']
    assert cl and 'loopdetect' in cl[0]['loop']['resolved_by']
    acts = _mine(_tick(ev, t + 70), ids)
    assert any('LBD port recovered. Loop detection restarted' in a.get('msg', '') for a in acts)


def test_apresia_loopdetect_disabled_does_nothing(client, ev):
    ids = _ring(client, kind='apresia', ifs=P)
    t = time.time()
    for k in range(0, 30, 5):
        acts = _mine(_tick(ev, t + k), ids)
        assert not [a for a in acts if a['do'] == 'errdisable']
    assert 'Disabled' in _cli(client, ids[0], 'configure', 'disable loopdetect', 'end', 'show loopdetect')


def test_response_loopcut_stops_loop(client, ev):
    ids = _ring(client)
    t = time.time()
    _tick(ev, t)
    alert = [a for a in A._flow_recorder.alerts if a['rule'] == 'l2_loop' and a['src'] in ids][-1]
    plan = A._response_plan(alert)
    assert plan['actions'] and all(a['kind'] == 'loopcut' for a in plan['actions'])
    act = plan['actions'][-1]
    assert act['steps'][2] == (act['target'], 'shutdown')
    ev.run_until_complete(A._response_run(alert, act))
    cl = [a for a in _mine(_tick(ev, t + 1), ids) if a['do'] == 'cleared']
    assert cl and cl[0]['loop']['resolved_by'].startswith('RESPONSE: ループ遮断')


def test_config_is_saved_and_restored(client, tmp_path, monkeypatch):
    ids = _ring(client, close=False, n=2)
    ap = _ring(client, kind='apresia', ifs=P, close=False, n=2)
    _cli(client, ids[0], 'configure terminal', f'interface {G[1]}', 'storm-control broadcast level 7.5',
         'storm-control action trap', 'exit', 'errdisable recovery cause all', 'errdisable recovery interval 120', 'end')
    _cli(client, ap[0], 'configure', 'enable loopdetect', 'config loopdetect ports 3-4 state disabled', 'end')
    path = tmp_path / 'saved.json'
    monkeypatch.setattr(A, 'SAVED_CONFIG_PATH', path)
    A._save_config()
    data = json.loads(path.read_text())
    blob = json.dumps(data)
    assert '"storm_control"' in blob and '"errdisable_recovery"' in blob and '"loopdetect"' in blob
    st0, ap0 = A.device_sessions[ids[0]], A.device_sessions[ap[0]]
    st0.storm_control, st0.errdisable_recovery, ap0.loopdetect = {}, {}, {}
    A._load_config()
    st0, ap0 = A.device_sessions[ids[0]], A.device_sessions[ap[0]]
    assert st0.storm_control[G[1]] == {'action': 'trap', 'level': 7.5}
    assert set(st0.errdisable_recovery['causes']) == {'bpduguard', 'storm-control', 'loopback'}
    assert st0.errdisable_recovery['interval'] == 120
    assert ap0.loopdetect['enabled'] and ap0.loopdetect['ports_off'] == ['3', '4']


def test_routed_ports_are_not_an_l2_loop():
    """no switchport のルーテッドポートで三角形に組んだ Catalyst(OSPF でよくある形)は L2 ループではない"""
    from engine.l2_loop import find_rings

    class V:
        links = {'c1': {'c2', 'c3'}, 'c2': {'c1', 'c3'}, 'c3': {'c1', 'c2'}}

        def _edge_up(self, a, b):
            return True
    vn = V()
    types = {k: 'catalyst' for k in ('c1', 'c2', 'c3')}
    assert len(find_rings(vn, types, lambda d: False)) == 1                       # L2 なら輪
    assert find_rings(vn, types, lambda d: False, routed=lambda d, p: d == 'c1') == []   # c1 側がルーテッドなら輪は切れる
    assert find_rings(vn, types, lambda d: False, routed=lambda d, p: True) == []
