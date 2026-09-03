"""
実ワイヤプロトコル RIP/BGP リスナーの結合テスト

engine/real_rip_agent.py・engine/real_bgp_agent.py のリスナーを
実際にソケットにbindして起動し、tools/route_injector_cli.py が組み立てる
**本物のRIP/BGPパケット**を送り込んで、内部エンジン
(RipEngine / BgpEngine) に経路が学習されることを検証する。

CI等の非rootでも動くよう、well-knownポート(520/179)ではなく
非特権ポート(15520/11179)を使い、ループバックのIPは 127.0.0.0/8 の
既存アドレスを使う（新規にIPエイリアスを追加しない）。

OSPFは raw socket (IP protocol 89) と scapy が必要でroot権限を要するため
既定ではスキップする。実行するには NETLAB_OSPF_WIRE_TEST=1 を設定する
（手順は docs/real-wire-routing-protocols.md 参照）。
"""

import asyncio
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import BgpEngine, RipEngine
from engine.real_bgp_agent import start_all_bgp_agents
from engine.real_rip_agent import start_all_rip_agents
from tools import route_injector_cli as injector

RIP_TEST_PORT = 15520
BGP_TEST_PORT = 11179
LISTEN_IP = '127.0.0.1'


class _FakeDeviceState:
    """_pick_management_ip が参照する interfaces だけを持つスタブ"""

    def __init__(self, ip):
        self.interfaces = {'Vlan1': {'ip': ip, 'prefix': 24, 'status': 'up'}}


@pytest.mark.asyncio
async def test_real_rip_listener_learns_route():
    rip_engine = RipEngine()
    device_id = 'test-dev'
    await rip_engine.start(device_id, 'TestDev', ['192.168.99.0/24'], version=2)

    sessions = {device_id: _FakeDeviceState(LISTEN_IP)}
    started = await start_all_rip_agents(sessions, rip_engine, port=RIP_TEST_PORT)
    assert device_id in started, '実RIPリスナーの起動に失敗した'
    transport, _ip = started[device_id]

    try:
        routes = [{'network': '172.16.50.0', 'netmask': '255.255.255.0',
                   'nexthop': '0.0.0.0', 'metric': 1, 'tag': 0}]
        packet = injector.rip_build_packet(routes, 2, command=2)  # 2=Response
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(packet, (LISTEN_IP, RIP_TEST_PORT))
        finally:
            sock.close()

        # 受信はリスナー側のイベントループのタスクで非同期に処理される
        for _ in range(50):
            await asyncio.sleep(0.1)
            if any(r.network == '172.16.50.0' for r in rip_engine.nodes[device_id]['table']):
                break

        learned = [r for r in rip_engine.nodes[device_id]['table']
                   if r.network == '172.16.50.0']
        assert learned, '実RIPパケットで送った経路が学習されていない'
        assert learned[0].prefix == 24
        # 送信時metric=1 に受信側で+1されて2になる（ベルマン-フォード）
        assert learned[0].metric == 2
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_real_bgp_listener_learns_route():
    bgp_engine = BgpEngine()
    device_id = 'test-dev'
    await bgp_engine.start(device_id, 'TestDev', 65099)

    sessions = {device_id: _FakeDeviceState(LISTEN_IP)}
    started = await start_all_bgp_agents(sessions, bgp_engine, port=BGP_TEST_PORT)
    assert device_id in started, '実BGPリスナーの起動に失敗した'
    server, _ip = started[device_id]

    speaker = injector.BGPSpeaker(
        peer_ip=LISTEN_IP, local_as=65001, remote_as=65099,
        router_id='10.9.9.100', hold_time=30, port=BGP_TEST_PORT,
        on_log=lambda msg: None, connect_timeout=5,
    )

    try:
        # BGPSpeakerはブロッキングI/O + スレッドで動くため、
        # イベントループを止めないよう別スレッドで実行する
        done = threading.Event()
        result = {}

        def run_speaker():
            try:
                speaker.connect()
                if speaker.established_event.wait(timeout=10):
                    result['established'] = True
                    speaker.advertise([('172.16.60.0', 24)], next_hop='10.9.9.100')
                    time.sleep(1)
                else:
                    result['established'] = False
            except Exception as e:  # 接続失敗などをテスト側へ伝える
                result['error'] = repr(e)
            finally:
                done.set()

        threading.Thread(target=run_speaker, daemon=True).start()
        for _ in range(150):
            await asyncio.sleep(0.1)
            if done.is_set():
                break

        assert 'error' not in result, f"BGPセッションでエラー: {result.get('error')}"
        assert result.get('established'), 'BGPセッションがEstablishedに到達しなかった'

        for _ in range(30):
            await asyncio.sleep(0.1)
            if any(r['prefix'] == '172.16.60.0'
                   for r in bgp_engine.nodes[device_id]['loc_rib']):
                break

        learned = [r for r in bgp_engine.nodes[device_id]['loc_rib']
                   if r['prefix'] == '172.16.60.0']
        assert learned, '実BGP UPDATEで広告した経路がloc_ribに入っていない'
        assert learned[0]['prefix_len'] == 24
        assert learned[0]['next_hop'] == '10.9.9.100'
        # loc_rib は dict のリストである必要がある（show ip route がキー参照するため）
        assert isinstance(learned[0], dict)
    finally:
        speaker.close(send_cease=False)
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(
    os.environ.get('NETLAB_OSPF_WIRE_TEST') != '1',
    reason='OSPF実ワイヤ試験はraw socket(root)とscapyが必要。'
           'NETLAB_OSPF_WIRE_TEST=1 で有効化する',
)
@pytest.mark.asyncio
async def test_real_ospf_listener_learns_external_route():
    from engine.protocols import OspfEngine
    from engine.real_ospf_agent import DeviceOspfResponder

    ospf_engine = OspfEngine()
    device_id = 'test-dev'
    await ospf_engine.start(device_id, 'TestDev', 1, ['127.0.0.0/8'], '0.0.0.0')

    responder = DeviceOspfResponder(
        device_id=device_id, ospf_engine=ospf_engine,
        loop=asyncio.get_event_loop(),
        iface='lo', my_ip='127.0.0.1', router_id='127.0.0.1',
        area='0.0.0.0', mask='255.0.0.0',
        hello_interval=2, dead_interval=8,
    )
    responder.start()

    from network_route_injector import OSPFNeighborFaker  # noqa: E402

    peer = OSPFNeighborFaker(
        iface='lo', my_ip='127.0.0.2', router_id='127.0.0.2', area='0.0.0.0',
        mask='255.0.0.0', hello_interval=2, dead_interval=8, rxmt_interval=3,
    )
    # start() は sniff/hello/rxmt の3スレッドを起動する。DBDesc再送を行う
    # rxmtスレッドが無いと、DBDescが1回でも取りこぼされた時点でExStartから
    # 進めなくなるため、Helloを手書きループで送るのではなくstart()を使う
    peer.start()

    try:
        for _ in range(60):
            await asyncio.sleep(1)
            if peer.state == peer.STATE_FULL:
                break
        assert peer.state == peer.STATE_FULL, 'OSPFネイバーがFullに到達しなかった'

        peer.originate_router_lsa(cost=10)
        await asyncio.sleep(1)
        assert peer.inject_external_route('192.168.200.0', '255.255.255.0', metric=20)

        for _ in range(30):
            await asyncio.sleep(0.2)
            if '192.168.200.0/24' in ospf_engine.nodes[device_id].get('_learned_external', {}):
                break
        assert '192.168.200.0/24' in ospf_engine.nodes[device_id].get('_learned_external', {}), \
            '実OSPF External-LSAで注入した経路が学習されていない'
    finally:
        responder.stop()
        peer.stop()
