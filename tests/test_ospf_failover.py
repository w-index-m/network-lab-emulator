"""
回帰テスト: OSPF障害時の再収束とフローティングスタティックへの切替。

2台のCatalystを主回線(OSPF)と予備回線(OSPFに載せない)の2本で繋ぎ、
主回線をshutdownしたときに

  1. OSPF隣接が落ちる
  2. OSPF学習経路がRIBから撤回される
  3. AD値の大きいフローティングスタティックが浮上する

という実機どおりの動作になることを検証する。

修正前は以下が全て成立せず、切替が一切起きなかった:
  - 実OSPFリスナー(engine/real_ospf_agent.py)が受信側Deadタイマーを持たず、
    shutdownの通知も受けていなかったため隣接がFullのまま残った
  - stop()後もscapyのsniffがパケットを処理し続け、隣接が復活していた
  - シミュレーション側OSPFが network 文を無視して全リンクへHelloを流し、
    OSPFに入れていない予備回線上で隣接を張っていた
  - RIBのshutdownフィルタが「ifaceが空の動的経路」を素通りさせていた
  - 自分のrouter-idを次ホップとするOSPF経路が installされていた
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import (            # noqa: E402
    icmp_engine, ospf_engine, rib_engine, vnet,
)


def _setup(monkeypatch_free=True):
    """fs1(10.90.1.1/10.90.2.1) --- fs2 を主/予備の2本で接続した状態を作る"""
    dev, peer = 'tfo1', 'tfo2'
    for d in (dev, peer):
        vnet.links.pop(d, None)
        vnet.interface_links.pop(d, None)
        vnet.down_interfaces.pop(d, None)
        if hasattr(vnet, 'link_ifaces'):
            vnet.link_ifaces.pop(d, None)
        ospf_engine.nodes.pop(d, None)
        rib_engine.nodes.pop(d, None)
        icmp_engine.device_ips.pop(d, None)

    def _reg(d, ifaces):
        # resolve_learned_next_hop は 'ips'、_iface_for_* は 'interfaces'
        # を見るため、実機登録と同じく両方を持たせる
        icmp_engine.device_ips[d] = {
            'interfaces': {k: {'ip': v[0], 'prefix': v[1]}
                           for k, v in ifaces.items()},
            'ips': {v[0]: v[1] for v in ifaces.values()},
        }

    _reg(dev, {
        'GigabitEthernet1/0/1': ('10.90.1.1', 24),
        'GigabitEthernet1/0/2': ('10.90.2.1', 24),
        'Loopback0': ('172.31.1.1', 32),
    })
    # OSPF経路のnext_hopは相手のrouter-id。実装は共有セグメント上の
    # 実IP(10.90.1.2)へ解決するので、その元になるIFを持たせておく。
    _reg(peer, {
        'GigabitEthernet1/0/1': ('10.90.1.2', 24),
        'GigabitEthernet1/0/2': ('10.90.2.2', 24),
        'Loopback0': ('172.31.2.2', 32),
    })
    pn = ospf_engine._node(peer)
    pn['enabled'] = True
    pn['router_id'] = '172.31.2.2'
    vnet.add_link(dev, peer, 'GigabitEthernet1/0/1', 'GigabitEthernet1/0/1')
    vnet.add_link(dev, peer, 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/2')

    n = ospf_engine._node(dev)
    n['enabled'] = True
    n['router_id'] = '172.31.1.1'
    n['process_id'] = 1
    # 主回線だけをOSPFに載せる（予備回線 10.90.2.0/24 は入れない）
    n['networks'] = ['10.90.1.0/24', '172.31.1.1/32']
    return dev, peer


# ── network文に載っていないIFではOSPFを喋らない ──────────
def test_ospf_enabled_ifaces_follows_network_statements():
    dev, _ = _setup()
    enabled = ospf_engine.ospf_enabled_ifaces(dev)
    assert 'GigabitEthernet1/0/1' in enabled
    assert 'Loopback0' in enabled
    # 予備回線はnetwork文に無いのでOSPF対象外
    assert 'GigabitEthernet1/0/2' not in enabled


def test_no_networks_configured_falls_back_to_all_interfaces():
    """network未設定なら判断できないので従来どおり全IFを対象にする"""
    dev, _ = _setup()
    ospf_engine.nodes[dev]['networks'] = []
    assert ospf_engine.ospf_enabled_ifaces(dev) is None
    assert ospf_engine._non_ospf_peers(dev) == set()


def test_peer_reachable_only_via_non_ospf_link_is_excluded():
    dev, peer = _setup()
    # 予備回線だけで繋がっている状態にする
    vnet.link_ifaces[dev][peer] = {'GigabitEthernet1/0/2'}
    vnet.interface_links[dev][peer] = 'GigabitEthernet1/0/2'
    assert peer in ospf_engine._non_ospf_peers(dev)


def test_peer_on_parallel_links_is_allowed_while_ospf_link_is_up():
    """主(OSPF)と予備(非OSPF)の並列リンク。主が生きていれば隣接は張る"""
    dev, peer = _setup()
    assert peer not in ospf_engine._non_ospf_peers(dev)


def test_peer_excluded_when_the_only_ospf_link_is_shut():
    """主回線をshutdownしたら、予備回線が生きていてもOSPFは届かない

    vnet.interface_links は peer あたり1本しか覚えていないため、
    ここを見ていると予備回線側から送信できてしまい、主回線を落としても
    OSPF隣接が生き残っていた。
    """
    dev, peer = _setup()
    vnet.interface_down(dev, 'GigabitEthernet1/0/1')
    assert peer in ospf_engine._non_ospf_peers(dev)


# ── フローティングスタティックへの切替 ────────────────────
def _add_routes(dev, peer):
    """OSPF学習経路(AD110)とフローティングスタティック(AD210)を両方積む"""
    ospf_engine.nodes[dev]['routes'] = [{
        'network': '172.31.2.2', 'prefix': 32, 'metric': 20,
        'via': peer, 'next_hop': '172.31.2.2', 'type': 'O',
    }]
    rib_engine.add_static_route(dev, dev, '172.31.2.2', 32,
                                '10.90.2.2', ad=210)


def test_ospf_preferred_while_primary_link_is_up():
    dev, peer = _setup()
    _add_routes(dev, peer)
    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(dev)}
    r = best['172.31.2.2/32']
    assert r['source'] == 'ospf'
    assert r['ad'] == 110


def test_floating_static_takes_over_when_primary_link_is_shut():
    """主回線shutdownでOSPF経路が撤回され、AD210のstaticが浮上する

    OSPF候補は next_hop がrouter-idのため iface が空で、以前は
    shutdownフィルタを素通りして生き残っていた。
    """
    dev, peer = _setup()
    _add_routes(dev, peer)
    vnet.interface_down(dev, 'GigabitEthernet1/0/1')
    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(dev)}
    r = best['172.31.2.2/32']
    assert r['source'] == 'static', f'期待:static 実際:{r["source"]}'
    assert r['ad'] == 210
    assert r['next_hop'] == '10.90.2.2'


def test_ospf_route_returns_after_recovery():
    dev, peer = _setup()
    _add_routes(dev, peer)
    vnet.interface_down(dev, 'GigabitEthernet1/0/1')
    vnet.interface_up(dev, 'GigabitEthernet1/0/1')
    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(dev)}
    assert best['172.31.2.2/32']['source'] == 'ospf'


def test_self_router_id_next_hop_is_not_installed():
    """自分のrouter-idを次ホップとするOSPF経路はRIBに入れない

    直結経路がshutdownで消えた瞬間に
    "O 10.90.1.0/24 via <自分>, Loopback0" として表面化していた。
    """
    dev, peer = _setup()
    ospf_engine.nodes[dev]['routes'] = [{
        'network': '10.90.1.0', 'prefix': 24, 'metric': 10,
        'via': peer, 'next_hop': '172.31.1.1', 'type': 'O',
    }]
    vnet.interface_down(dev, 'GigabitEthernet1/0/1')
    nets = [f"{r['network']}/{r['prefix']}"
            for r in rib_engine.get_best_routes(dev) if r['source'] == 'ospf']
    assert '10.90.1.0/24' not in nets


# ── show ip route の引数対応 ──────────────────────────────
def test_show_ip_route_detail_reports_ad_and_source():
    dev, peer = _setup()
    _add_routes(dev, peer)
    out = rib_engine.format_show_ip_route_detail(dev, '172.31.2.2')
    assert 'Routing entry for 172.31.2.2/32' in out
    assert 'distance 110' in out
    assert 'Routing Descriptor Blocks:' in out


def test_show_ip_route_detail_switches_to_static_after_failure():
    dev, peer = _setup()
    _add_routes(dev, peer)
    vnet.interface_down(dev, 'GigabitEthernet1/0/1')
    out = rib_engine.format_show_ip_route_detail(dev, '172.31.2.2')
    assert 'distance 210' in out
    assert '10.90.2.2' in out


def test_show_ip_route_detail_unknown_prefix():
    dev, peer = _setup()
    _add_routes(dev, peer)
    assert 'not in table' in rib_engine.format_show_ip_route_detail(
        dev, '203.0.113.99')


def test_show_ip_route_proto_filters_by_source():
    dev, peer = _setup()
    _add_routes(dev, peer)
    out = rib_engine.format_show_ip_route_proto(dev, 'ospf')
    assert '172.31.2.2/32' in out
    # connected経路は含まれない
    assert 'directly connected' not in out
