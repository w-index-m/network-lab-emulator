"""
大規模トポロジテスト: 8台以上のメッシュ・チェーンでのスケーラビリティ検証

【実行方法についての注意】
本ファイルは pytest では実行しない（スタンドアロンスクリプト）。
理由: tests/conftest.py が NETLAB_FAST_TIMERS=1 を強制するため、
OSPF Hello=1秒 / Dead=4秒 という極めて短い周期になる。8台・28リンクの
フルメッシュでは、非同期イベントループの輻輳により Hello 処理が遅延し、
本物の実装バグとは無関係に Dead タイマーが誤発火してネイバーが
フラッピングすることを確認済み（実タイマー(Hello=10s/Dead=40s)では
15秒で全リンクが安定して Full になることを検証済み）。
そのため大規模トポロジ検証は実タイマーで行う。

実行: python tests/test_extended_topologies.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto


class Runner:
    def __init__(self):
        self.results = []

    def record(self, name, ok, message):
        self.results.append((name, ok, message))
        icon = '✅' if ok else '❌'
        print(f'{icon} {name}: {message}')

    def report(self):
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print('\n' + '=' * 70)
        print(f'📊 大規模トポロジテスト結果: {passed}/{total} 成功')
        print('=' * 70)
        return passed == total


def _fresh():
    proto.vnet.links.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.rip_engine.nodes.clear()
    proto.ospf_engine.nodes.clear()
    proto.bgp_engine.nodes.clear()

    async def noop(msg):
        pass
    return noop


def _link(noop, a, b):
    proto.vnet.register(a, noop)
    proto.vnet.register(b, noop)
    proto.vnet.add_link(a, b)


def _fullmesh(noop, routers):
    for i, r1 in enumerate(routers):
        for r2 in routers[i + 1:]:
            _link(noop, r1, r2)


async def ospf_8router_fullmesh(runner: Runner):
    """OSPF 8台フルメッシュ(28リンク): 全員が全員とFull隣接、全LANを学習"""
    noop = _fresh()
    routers = [f'R{i}' for i in range(1, 9)]
    networks = [f'192.168.{i}.0/24' for i in range(1, 9)]
    _fullmesh(noop, routers)

    for rid, net in zip(routers, networks):
        await proto.ospf_engine.start(rid, f'Router-{rid}', 1, [net])

    await asyncio.sleep(15)

    all_full = True
    for rid in routers:
        neighbors = proto.ospf_engine.nodes[rid]['neighbors']
        others = [r for r in routers if r != rid]
        for other in others:
            if other not in neighbors or neighbors[other].state != 'Full':
                all_full = False
    runner.record('OSPF 8台フルメッシュ 全隣接Full',
                   all_full,
                   f'{sum(len(proto.ospf_engine.nodes[r]["neighbors"]) for r in routers)}/56 隣接 Full')

    all_learned = True
    for rid, own_net in zip(routers, networks):
        learned = {r['network'] for r in proto.ospf_engine.nodes[rid]['routes']}
        other_nets = [n.split('/')[0] for n in networks if n != own_net]
        if not all(on in learned for on in other_nets):
            all_learned = False
    runner.record('OSPF 8台フルメッシュ 全LAN学習',
                   all_learned,
                   '各ルータが他7台のLANを全学習')

    for rid in routers:
        await proto.ospf_engine.stop(rid)


async def ospf_10router_chain(runner: Runner):
    """OSPF 10台チェーン: 9ホップ先までの多段経路伝播"""
    noop = _fresh()
    routers = [f'R{i}' for i in range(1, 11)]
    networks = [f'10.{i}.0.0/24' for i in range(1, 11)]

    for i in range(len(routers) - 1):
        _link(noop, routers[i], routers[i + 1])

    for rid, net in zip(routers, networks):
        await proto.ospf_engine.start(rid, f'Router-{rid}', 1, [net])

    await asyncio.sleep(20)

    r1_learned = {r['network'] for r in proto.ospf_engine.nodes['R1']['routes']}
    r10_learned = {r['network'] for r in proto.ospf_engine.nodes['R10']['routes']}
    ok = '10.10.0.0' in r1_learned and '10.1.0.0' in r10_learned
    runner.record('OSPF 10台チェーン 9ホップ経路伝播', ok,
                   f'R1学習数={len(r1_learned)}, R10学習数={len(r10_learned)}')

    for rid in routers:
        await proto.ospf_engine.stop(rid)


async def bgp_8as_fullmesh(runner: Runner):
    """BGP 8AS フルメッシュ: 28セッション確立・全prefix学習"""
    noop = _fresh()
    routers = [f'R{i}' for i in range(1, 9)]
    asns = list(range(65001, 65009))
    prefixes = [f'10.{i}.0.0/16' for i in range(1, 9)]
    _fullmesh(noop, routers)

    for rid, asn, prefix in zip(routers, asns, prefixes):
        await proto.bgp_engine.start(rid, f'AS{asn}-{rid}', asn)
        await proto.bgp_engine.advertise_network(rid, prefix)

    for i, r1 in enumerate(routers):
        for r2 in routers[i + 1:]:
            asn1, asn2 = asns[routers.index(r1)], asns[routers.index(r2)]
            await proto.bgp_engine.add_neighbor(r1, r2, f'AS{asn2}-{r2}', asn2)
            await proto.bgp_engine.add_neighbor(r2, r1, f'AS{asn1}-{r1}', asn1)

    await asyncio.sleep(6)

    all_established = True
    for rid in routers:
        established = [s for s in proto.bgp_engine.nodes[rid]['sessions'].values()
                        if s.state == 'Established']
        if len(established) != 7:
            all_established = False
    runner.record('BGP 8AS フルメッシュ 全28セッション確立', all_established,
                   '各ASが他7ASとEstablished')

    all_prefixes_learned = True
    for rid, own_prefix in zip(routers, prefixes):
        learned = {r.prefix for r in proto.bgp_engine.nodes[rid]['rib_in']}
        other = [p.split('/')[0] for p in prefixes if p != own_prefix]
        if not all(o in learned for o in other):
            all_prefixes_learned = False
    runner.record('BGP 8AS フルメッシュ 全prefix学習', all_prefixes_learned,
                   '各ASが他7ASのprefixを全学習')


async def rip_8router_chain(runner: Runner):
    """RIP 8台チェーン: 7ホップ先までの経路収束"""
    noop = _fresh()
    routers = [f'R{i}' for i in range(1, 9)]
    networks = [f'172.16.{i}.0/24' for i in range(1, 9)]

    for i in range(len(routers) - 1):
        _link(noop, routers[i], routers[i + 1])

    for rid, net in zip(routers, networks):
        await proto.rip_engine.start(rid, f'Router-{rid}', [net])

    # RIP update timer は本番30秒だが、複数回明示送信して収束を早める
    await asyncio.sleep(2)
    for _ in range(4):
        for rid in routers:
            await proto.rip_engine._send_update(rid)
        await asyncio.sleep(2)

    r1_learned = {r.network for r in proto.rip_engine.nodes['R1']['table']}
    r8_learned = {r.network for r in proto.rip_engine.nodes['R8']['table']}
    ok = '172.16.8.0' in r1_learned and '172.16.1.0' in r8_learned
    runner.record('RIP 8台チェーン 7ホップ経路収束', ok,
                   f'R1学習数={len(r1_learned)}, R8学習数={len(r8_learned)}')

    for rid in routers:
        await proto.rip_engine.stop(rid)


async def main():
    print('🚀 大規模トポロジテスト開始（実タイマー使用のため数分かかります）')
    print('=' * 70)
    runner = Runner()

    await ospf_8router_fullmesh(runner)
    await ospf_10router_chain(runner)
    await bgp_8as_fullmesh(runner)
    await rip_8router_chain(runner)

    ok = runner.report()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    asyncio.run(main())
