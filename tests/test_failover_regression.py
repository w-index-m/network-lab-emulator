"""
障害切替（再収束）の回帰テスト — BGP / RIP / EIGRP / HSRP

OSPFで「収束状態は正しいのに再収束が全く動かない」不具合が10件
まとめて見つかったため、他プロトコルにも同じ穴が無いかを横並びで
検証する。構成はOSPFのとき（tests/test_ospf_failover.py）と同じ:

    主回線 = 動的プロトコル / 予備回線 = フローティングスタティック(AD 210)
    主回線をshutdown → 予備に切り替わり、復旧したら戻る

EIGRPではこの試験で**無限再帰によるクラッシュ**が見つかった。
2台構成でEIGRPを設定した瞬間に receive -> _send_update -> send_to ->
receive -> ... とUpdateを撃ち返し合い、RecursionErrorでAPIが500を
返していた（vnet.send_toはreceiveを直接awaitするため、メッセージの
ループではなくスタックが伸び続ける）。
"""

import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import (                      # noqa: E402
    EigrpRoute, eigrp_engine, icmp_engine, rib_engine, vnet,
)


def _setup(a='t-fo-a', b='t-fo-b'):
    """a --- b を主(Gi1/0/1)・予備(Gi1/0/2)の2本で接続した状態"""
    for d in (a, b):
        vnet.links.pop(d, None)
        vnet.interface_links.pop(d, None)
        vnet.down_interfaces.pop(d, None)
        if hasattr(vnet, 'link_ifaces'):
            vnet.link_ifaces.pop(d, None)
        eigrp_engine.nodes.pop(d, None)
        rib_engine.nodes.pop(d, None)
        icmp_engine.device_ips.pop(d, None)

    def _reg(d, ifaces):
        icmp_engine.device_ips[d] = {
            'interfaces': {k: {'ip': v[0], 'prefix': v[1]}
                           for k, v in ifaces.items()},
            'ips': {v[0]: v[1] for v in ifaces.values()},
        }

    _reg(a, {'GigabitEthernet1/0/1': ('10.93.1.1', 24),
             'GigabitEthernet1/0/2': ('10.93.2.1', 24),
             'Loopback0': ('172.27.1.1', 32)})
    _reg(b, {'GigabitEthernet1/0/1': ('10.93.1.2', 24),
             'GigabitEthernet1/0/2': ('10.93.2.2', 24),
             'Loopback0': ('172.27.2.2', 32)})
    vnet.add_link(a, b, 'GigabitEthernet1/0/1', 'GigabitEthernet1/0/1')
    vnet.add_link(a, b, 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/2')
    return a, b


# ══════════════════════════════════════════
# EIGRP: Update撃ち返しによる無限再帰
# ══════════════════════════════════════════
ASN = 1


def _eigrp_node(dev, peer, hostname):
    n = eigrp_engine._node(dev)
    n['enabled'] = True
    n['hostname'] = hostname
    n['asn'] = ASN
    n['neighbors'][peer] = type('N', (), {
        'neighbor_id': peer, 'hostname': peer, 'uptime': 0})()
    return n


def _update(peer, network, prefix, metric, external=False):
    """EIGRPのUpdateメッセージ。asnが無いとAS不一致で捨てられる。"""
    return {'op': 'update', 'asn': ASN, 'src_id': peer, 'src_hostname': 'B',
            'entries': [{'network': network, 'prefix': prefix,
                         'metric': metric, 'external': external}]}


@pytest.mark.asyncio
async def test_eigrp_repeated_identical_update_does_not_recurse():
    """同じ隣接から同じ内容のUpdateを繰り返し受けても再送しない

    ここが changed=True のままだと、相手も同じ理由で撃ち返すため
    receive と _send_update が相互に無限再帰する。
    """
    a, b = _setup()
    n = _eigrp_node(a, b, 'A')
    n['topology'].append(EigrpRoute(
        network='172.27.2.2', prefix=32, fd=5632, rd=2816,
        next_hop='10.93.1.2', learned_from=b,
        learned_from_hostname='B', external=False))

    msg = _update(b, '172.27.2.2', 32, 2816)

    sent = []
    orig = eigrp_engine._send_update

    async def _spy(device_id):
        sent.append(device_id)

    eigrp_engine._send_update = _spy
    try:
        for _ in range(5):
            await eigrp_engine.receive(a, msg)
    finally:
        eigrp_engine._send_update = orig
    assert sent == [], f'変化が無いのにUpdateを送っている: {sent}'


@pytest.mark.asyncio
async def test_eigrp_update_with_changed_metric_does_send():
    """本当に変化したときはちゃんとUpdateを出す（過剰抑制でないこと）"""
    a, b = _setup()
    n = _eigrp_node(a, b, 'A')
    n['topology'].append(EigrpRoute(
        network='172.27.2.2', prefix=32, fd=5632, rd=2816,
        next_hop='10.93.1.2', learned_from=b,
        learned_from_hostname='B', external=False))

    sent = []
    orig = eigrp_engine._send_update

    async def _spy(device_id):
        sent.append(device_id)

    eigrp_engine._send_update = _spy
    try:
        await eigrp_engine.receive(a, _update(b, '172.27.2.2', 32, 9999))
    finally:
        eigrp_engine._send_update = orig
    assert sent == [a], 'メトリックが変わったのにUpdateを送っていない'


@pytest.mark.asyncio
async def test_eigrp_new_route_sends_update():
    a, b = _setup()
    _eigrp_node(a, b, 'A')
    sent = []
    orig = eigrp_engine._send_update

    async def _spy(device_id):
        sent.append(device_id)

    eigrp_engine._send_update = _spy
    try:
        await eigrp_engine.receive(a, _update(b, '192.0.2.0', 24, 2816))
    finally:
        eigrp_engine._send_update = orig
    assert sent == [a]


# ══════════════════════════════════════════
# フローティングスタティックへの切替（EIGRP）
# ══════════════════════════════════════════
def test_eigrp_route_is_withdrawn_when_exit_interface_is_shut():
    """主回線をshutdownするとEIGRP経路が消え、AD210のstaticが浮上する"""
    a, b = _setup()
    n = _eigrp_node(a, b, 'A')
    n['topology'].append(EigrpRoute(
        network='172.27.2.2', prefix=32, fd=5632, rd=2816,
        next_hop='10.93.1.2', learned_from=b,
        learned_from_hostname='B', external=False))
    rib_engine.add_static_route(a, a, '172.27.2.2', 32, '10.93.2.2', ad=210)

    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(a)}
    assert best['172.27.2.2/32']['source'] == 'eigrp'

    vnet.interface_down(a, 'GigabitEthernet1/0/1')
    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(a)}
    r = best['172.27.2.2/32']
    assert r['source'] == 'static', f'期待:static 実際:{r["source"]}'
    assert r['ad'] == 210

    vnet.interface_up(a, 'GigabitEthernet1/0/1')
    best = {f"{r['network']}/{r['prefix']}": r
            for r in rib_engine.get_best_routes(a)}
    assert best['172.27.2.2/32']['source'] == 'eigrp'
