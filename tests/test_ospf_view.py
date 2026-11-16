"""
/cyber-view の OSPF モード(GET /api/traffic/ospf、engine/ospf_view.py)

- 隣接: 両端から見た state・Dead タイマー残り・DR/BDR、片側だけ/両側とも消えた時の状態、断や不一致の理由
- エリア: 装置ごとのエリアと ABR(複数エリア)の判定
- 最短経路ツリー: 各ルータの OSPF 経路の次ホップ(Router-ID → 装置)をたどる、経路が無い装置
- スタンドアロン版はこの API を持たない(404 → 画面は OSPF ボタンを隠す)
"""
import os
import sys
import time
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')   # app を import する前に(test_cyber_view.py と同じ)

from engine.ospf_view import snapshot

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class FakeEng:
    def __init__(self, nodes, areas, ospf_ifs=None, peer_area=None):
        self.nodes, self.area_networks = nodes, areas
        self._ifs, self._peer_area = ospf_ifs or {}, peer_area or {}

    def ospf_enabled_ifaces(self, d):
        return self._ifs.get(d)

    def _area_for_peer(self, d, p):
        return self._peer_area.get((d, p))


VNET = NS(_norm_iface=lambda s: s.lower())


def _node(rid, nbrs, routes=(), **kw):
    n = {'enabled': True, 'router_id': rid, 'process_id': 1, 'area_id': '0.0.0.0', 'priority': 1,
         'hello_interval': 10, 'dead_interval': 40, 'lsdb': {'x': 1}, 'routes': list(routes),
         'neighbors': nbrs, 'spf_runs': 2, 'last_spf': 1.0}
    n.update(kw)
    return n


def _nbr(state='Full', last=None, role='DR'):
    return NS(state=state, last_hello=last, role=role)


def _lab(now, r1_nbrs=None, r3_routes=None):
    """R1 ─ R2 ─ R3。R3 の LAN 10.9.3.0/24 は area 1(R3 が ABR)"""
    nodes = {
        'r1': _node('1.1.1.1', r1_nbrs if r1_nbrs is not None else {'r2': _nbr(last=now - 5, role='BDR')},
                    routes=[{'network': '10.9.3.0', 'prefix': '24', 'metric': 30, 'next_hop': '2.2.2.2', 'type': 'O IA'}]),
        'r2': _node('2.2.2.2', {'r1': _nbr(last=now - 2, role='DROther'), 'r3': _nbr(last=now - 1)},
                    routes=[{'network': '10.9.3.0', 'prefix': '24', 'metric': 20, 'next_hop': '3.3.3.3', 'type': 'O IA'}]),
        'r3': _node('3.3.3.3', {'r2': _nbr(last=now - 3, role='BDR')}, routes=r3_routes or []),
    }
    areas = {'r1': {'0.0.0.0': []}, 'r2': {'0.0.0.0': []}, 'r3': {'0.0.0.0': [], '0.0.0.1': []}}
    icmp = NS(device_ips={
        'r1': {'interfaces': {'Gi1': {'ip': '10.9.12.1', 'prefix': 24}, 'Gi3': {'ip': '10.9.1.1', 'prefix': 24}}},
        'r2': {'interfaces': {'Gi1': {'ip': '10.9.12.2', 'prefix': 24}, 'Gi2': {'ip': '10.9.23.1', 'prefix': 24}}},
        'r3': {'interfaces': {'Gi1': {'ip': '10.9.23.2', 'prefix': 24}, 'Gi3': {'ip': '10.9.3.1', 'prefix': 24},
                              'Gi9': {'ip': '172.31.9.1', 'prefix': 24}}},   # OSPF に入れていない IF
    })
    eng = FakeEng(nodes, areas, ospf_ifs={'r3': {'Gi1', 'Gi3'}})
    links = [{'a': 'r1', 'b': 'r2', 'iface_a': 'Gi1', 'iface_b': 'Gi1', 'down': False},
             {'a': 'r2', 'b': 'r3', 'iface_a': 'Gi2', 'iface_b': 'Gi1', 'down': False}]
    return eng, icmp, links, {'r1': 'R1', 'r2': 'R2', 'r3': 'R3', 'pc': 'PC'}


def test_adjacency_dead_timer_roles_and_abr():
    now = 1000.0
    eng, icmp, links, hn = _lab(now)
    d = snapshot(eng, VNET, icmp, links, hn, dest='10.9.3.0/24', now=now)
    assert d['enabled'] and [x['id'] for x in d['devices']] == ['r1', 'r2', 'r3']
    r3 = next(x for x in d['devices'] if x['id'] == 'r3')
    assert r3['abr'] and r3['areas'] == ['0.0.0.0', '0.0.0.1'] and r3['router_id'] == '3.3.3.3'
    a = next(x for x in d['adjacencies'] if (x['a'], x['b']) == ('r1', 'r2'))
    assert a['state'] == 'full' and a['state_a'] == a['state_b'] == 'Full'
    assert a['dead_a'] == 35 and a['dead_b'] == 38 and a['dead_interval'] == 40   # 40秒 − 最終Helloからの経過
    assert a['role_b'] == 'BDR' and a['role_a'] == 'DROther' and a['why'] == []


def test_one_sided_and_down_adjacency_with_reasons():
    now = 1000.0
    eng, icmp, links, hn = _lab(now, r1_nbrs={})       # R1 側は隣接が消えた(IF断)、R2 側は Dead タイマー待ち
    links[0]['down'] = True
    a = snapshot(eng, VNET, icmp, links, hn, now=now)['adjacencies'][0]
    assert a['state'] == 'partial' and a['state_a'] == 'Down' and a['state_b'] == 'Full'
    assert a['dead_a'] is None and a['dead_b'] == 38 and 'リンク断' in a['why']
    eng.nodes['r2']['neighbors'].pop('r1')
    eng.nodes['r2']['hello_interval'] = 5
    a = snapshot(eng, VNET, icmp, links, hn, now=now)['adjacencies'][0]
    assert a['state'] == 'down' and 'Hello間隔の不一致' in a['why']


def test_shortest_path_tree_and_destinations():
    now = 1000.0
    eng, icmp, links, hn = _lab(now)
    d = snapshot(eng, VNET, icmp, links, hn, dest='10.9.3.0/24', now=now)
    t = d['tree']
    assert t['root'] == ['r3'] and t['unreachable'] == []
    assert {(e['from'], e['to'], e['cost']) for e in t['edges']} == {('r1', 'r2', 30), ('r2', 'r3', 20)}
    nets = [x['net'] for x in d['destinations']]
    # LAN(1台だけが直結)が先、ルータ間リンクが後。OSPF に入れていない IF は出さない
    assert nets[:2] == ['10.9.1.0/24', '10.9.3.0/24'] and '172.31.9.0/24' not in nets
    assert next(x for x in d['destinations'] if x['net'] == '10.9.12.0/24')['transit']
    # R2 の経路が消えたら R2 は「経路なし」
    eng.nodes['r2']['routes'] = []
    t = snapshot(eng, VNET, icmp, links, hn, dest='10.9.3.0/24', now=now)['tree']
    assert t['unreachable'] == ['r2'] and [e['from'] for e in t['edges']] == ['r1']
    # 宛先を指定しなければ最初の LAN
    assert snapshot(eng, VNET, icmp, links, hn, now=now)['tree']['dest'] == '10.9.1.0/24'


def test_disabled_and_non_ospf_link_is_skipped():
    now = time.time()
    eng, icmp, links, hn = _lab(now)
    eng._ifs['r2'] = {'Gi1'}         # R2 の Gi2(R3 向け)は network 文に入っていない
    d = snapshot(eng, VNET, icmp, links, hn, now=now)
    assert [(a['a'], a['b']) for a in d['adjacencies']] == [('r1', 'r2')]
    for n in eng.nodes.values():
        n['enabled'] = False
    d = snapshot(eng, VNET, icmp, links, hn, now=now)
    assert d['enabled'] is False and d['devices'] == [] and d['tree'] is None


def test_api_endpoint_and_page_button():
    import app as app_module
    c = TestClient(app_module.app)
    d = c.get('/api/traffic/ospf').json()
    assert set(d) >= {'enabled', 'devices', 'adjacencies', 'destinations', 'tree'}
    html = c.get('/cyber-view').text
    assert 'id="mOspf"' in html and '/api/traffic/ospf' in html


def test_standalone_has_no_ospf_endpoint():
    sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
    import server as sa_server
    from cvs import config as sa_config
    cfg = sa_config.validate({'devices': [{'id': 'a'}], 'links': [], 'sources': {}})
    assert TestClient(sa_server.create_app(cfg, start_collector=False)).get('/api/traffic/ospf').status_code == 404


def test_background_selector_in_page():
    """背景(標準/金魚鉢/富士山)の切り替えボタンと、?bg= での指定、選択の記憶"""
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    assert 'id="zBg"' in html and "BG_LIST = ['default', 'goldfish', 'fuji']" in html
    assert 'function drawGoldfishBowl' in html and 'function drawFuji' in html
    assert "localStorage.setItem('cvBg'" in html and "get('bg')" in html
