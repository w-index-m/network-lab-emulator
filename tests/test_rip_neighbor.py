"""
ip rip neighbor <IP> テスト（Si-R ユニキャストRIP）

テスト内容:
  1. add_neighbor() で static_neighbors に IP が登録される
  2. IPが icmp_engine.device_ips に登録された装置に解決される
  3. _send_update が、通常ならセグメント不一致でスキップされる相手にも
     static neighbor 指定があれば送信する
"""

import asyncio
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import RipEngine, icmp_engine, vnet, rip_engine as global_rip_engine


def test_add_neighbor_stores_ip():
    engine = RipEngine()
    device_id = 'sir-1'
    engine.add_neighbor(device_id, '192.168.1.2')
    n = engine._node(device_id)
    assert '192.168.1.2' in n['static_neighbors']


def test_remove_neighbor():
    engine = RipEngine()
    device_id = 'sir-1'
    engine.add_neighbor(device_id, '192.168.1.2')
    engine.remove_neighbor(device_id, '192.168.1.2')
    n = engine._node(device_id)
    assert '192.168.1.2' not in n['static_neighbors']


def test_resolve_static_neighbor_devices():
    engine = RipEngine()
    device_id = 'sir-1'
    peer_id = 'sir-2'

    icmp_engine.device_ips[peer_id] = {'ips': {'192.168.1.2': 24}}
    try:
        engine.add_neighbor(device_id, '192.168.1.2')
        resolved = engine._resolve_static_neighbor_devices(device_id)
        assert resolved == {peer_id}
    finally:
        icmp_engine.device_ips.pop(peer_id, None)


def test_send_update_reaches_static_neighbor_despite_segment_mismatch():
    """通常はセグメント不一致でスキップされる相手でも、
    ip rip neighbor 指定があればユニキャストで届くことを確認。
    vnet.send_to はグローバルの rip_engine.receive にルーティングするため、
    ここではグローバルシングルトンを直接使う。"""
    engine = global_rip_engine
    device_id = 'sir-rn-1'
    peer_id = 'sir-rn-2'

    vnet.add_link(device_id, peer_id)
    icmp_engine.device_ips[device_id] = {'ips': {'10.0.0.1': 30}}
    icmp_engine.device_ips[peer_id] = {'ips': {'172.16.0.2': 30}}  # 別セグメント

    try:
        n1 = engine._node(device_id)
        n1['enabled'] = True
        n1['networks'] = ['10.0.0.0/30']
        n2 = engine._node(peer_id)
        n2['enabled'] = True

        # ip rip neighbor で明示的に指定
        engine.add_neighbor(device_id, '172.16.0.2')

        received = []
        orig_receive = engine.receive
        async def spy_receive(receiver_id, msg):
            received.append((receiver_id, msg))
            await orig_receive(receiver_id, msg)
        engine.receive = spy_receive
        try:
            asyncio.run(engine._send_update(device_id))
        finally:
            engine.receive = orig_receive

        assert len(received) == 1
        assert received[0][0] == peer_id
    finally:
        vnet.remove_link(device_id, peer_id)
        icmp_engine.device_ips.pop(device_id, None)
        icmp_engine.device_ips.pop(peer_id, None)
        engine.nodes.pop(device_id, None)
        engine.nodes.pop(peer_id, None)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
