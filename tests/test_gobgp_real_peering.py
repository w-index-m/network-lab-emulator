"""
本物のGoBGP(gobgpd)とエミュレータの実BGPリスナー(engine/real_bgp_agent.py)を
TCPでピアリングさせる結合テスト。

- GoBGP → エミュレータ: `gobgp global rib add` した経路がエミュレータの
  loc_rib/rib_inに入り、`gobgp global rib del`で撤回されること(WITHDRAWN処理)
- エミュレータ → GoBGP: `network`で広告した経路をGoBGPのadj-inで受け取れること
  (eBGPなので自ASがprependされ、next-hopはエミュレータのIP)
- neighbor設定がある装置は、AS不一致の相手をOPEN Error(Bad Peer AS)で拒否すること

gobgpd/gobgpがPATH(または~/go/bin)に無い環境ではファイルごとskipする
(netmiko/ansible/zgrab2と同じく外部ツールは手動/オプションの検証扱い)。
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.protocols import BgpEngine  # noqa: E402
from engine.real_bgp_agent import start_all_bgp_agents  # noqa: E402


def _bin(name):
    return shutil.which(name) or (os.path.expanduser(f'~/go/bin/{name}')
                                  if os.path.exists(os.path.expanduser(f'~/go/bin/{name}')) else None)


GOBGPD, GOBGP = _bin('gobgpd'), _bin('gobgp')
pytestmark = pytest.mark.skipif(not (GOBGPD and GOBGP), reason='gobgpd/gobgp not installed')

EMU_IP = '127.0.31.1'
GOBGP_IP = '127.0.31.2'
BGP_PORT = 11279
API_PORT = 50771


class _State:
    def __init__(self, ip, neighbors=None):
        self.interfaces = {'Vlan1': {'ip': ip, 'prefix': 24, 'status': 'up'}}
        self._bgp_configured_neighbors = neighbors or {}


def _gobgp(*args):
    out = subprocess.run([GOBGP, '-p', str(API_PORT), *args], capture_output=True, text=True, timeout=10)
    return out.stdout + out.stderr


def _start_gobgpd(tmp_path, peer_as, local_as=65100):
    conf = tmp_path / 'gobgpd.toml'
    conf.write_text(f"""
[global.config]
  as = {local_as}
  router-id = "{GOBGP_IP}"
  port = -1
[[neighbors]]
  [neighbors.config]
    neighbor-address = "{EMU_IP}"
    peer-as = {peer_as}
  [neighbors.transport.config]
    remote-port = {BGP_PORT}
    local-address = "{GOBGP_IP}"
  [neighbors.timers.config]
    connect-retry = 1
  [[neighbors.afi-safis]]
    [neighbors.afi-safis.config]
      afi-safi-name = "ipv4-unicast"
""")
    proc = subprocess.Popen([GOBGPD, '-f', str(conf), '--api-hosts', f'127.0.0.1:{API_PORT}',
                             '-l', 'warn'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


async def _wait(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.3)
    return False


def _neighbor_state():
    try:
        data = json.loads(_gobgp('neighbor', EMU_IP, '-j'))
        return data['state']['session_state']
    except Exception:
        return None


@pytest.mark.asyncio
async def test_gobgp_exchanges_routes_both_ways(tmp_path):
    bgp = BgpEngine()
    dev = 'emu-r1'
    await bgp.start(dev, 'EMU-R1', 65200, router_id=EMU_IP)
    bgp._node(dev)['networks'].append('10.77.0.0/24')
    started = await start_all_bgp_agents({dev: _State(EMU_IP)}, bgp, port=BGP_PORT)
    server = started[dev][0]
    proc = _start_gobgpd(tmp_path, peer_as=65200)
    try:
        assert await _wait(lambda: _neighbor_state() in ('established', 6)), _gobgp('neighbor')
        sess = bgp.nodes[dev]['sessions'][GOBGP_IP]
        assert sess.state == 'Established' and sess.remote_as == 65100

        # エミュレータ → GoBGP
        assert await _wait(lambda: '10.77.0.0/24' in _gobgp('neighbor', EMU_IP, 'adj-in'))
        adj = _gobgp('neighbor', EMU_IP, 'adj-in')
        assert '65200' in adj and EMU_IP in adj

        # GoBGP → エミュレータ
        _gobgp('global', 'rib', 'add', '10.88.0.0/24', '-a', 'ipv4')
        learned = lambda: [r for r in bgp.nodes[dev]['loc_rib'] if r['prefix'] == '10.88.0.0']  # noqa: E731
        assert await _wait(lambda: bool(learned()))
        r = learned()[0]
        assert r['prefix_len'] == 24 and r['as_path'] == [65100] and r['learned_from'] == GOBGP_IP

        # 撤回
        _gobgp('global', 'rib', 'del', '10.88.0.0/24', '-a', 'ipv4')
        assert await _wait(lambda: not learned())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        server.close()


@pytest.mark.asyncio
async def test_configured_neighbor_rejects_bad_peer_as(tmp_path):
    bgp = BgpEngine()
    dev = 'emu-r2'
    await bgp.start(dev, 'EMU-R2', 65200, router_id=EMU_IP)
    # 65999を期待しているのにGoBGPは65100で来る → Bad Peer AS
    started = await start_all_bgp_agents({dev: _State(EMU_IP, {GOBGP_IP: 65999})}, bgp, port=BGP_PORT)
    server = started[dev][0]
    proc = _start_gobgpd(tmp_path, peer_as=65200)
    try:
        assert await _wait(lambda: bool(bgp.nodes[dev].get('external_rejects')))
        assert 'bad peer AS 65100' in bgp.nodes[dev]['external_rejects'][0]['reason']
        await asyncio.sleep(1.5)
        assert _neighbor_state() not in ('established', 6)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        server.close()
