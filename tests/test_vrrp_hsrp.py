"""
VRRP / HSRP のネイバー確立・選出・フェイルオーバーのテスト

これまでVRRP/HSRPのテストは1件も無く、以下のバグが埋まっていた:

1. `VirtualNetwork.send_to()` のディスパッチ表に `vrrp_advert` /
   `hsrp_hello` が無く、hello_loopがパケットを撒いても受信側の
   エンジンに渡されず捨てられていた。その結果、両系ともMaster/Active
   のまま（スプリットブレイン）になっていた。
2. グループが相手の情報を保持しておらず、`show standby` /
   `show vrrp` の対向表示が常に `unknown` だった。
3. インターフェースをshutdownしても冗長化グループがInitに落ちず、
   切れた側がMaster/Activeを名乗り続けていた。
4. `broadcast_to_neighbors()` が送信側のdownしか見ておらず、
   自分のIFをshutdownした装置が対向からのHelloを受信し続けるため、
   Initに落とした直後に再選出されてActiveへ戻ってしまっていた。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto


@pytest.fixture
def two_routers():
    """10.9.9.0/24 で直結された2台（A: priority高 / B: 既定）"""
    proto.vnet.links.clear()
    proto.vnet.interface_links.clear()
    proto.vnet.down_interfaces.clear()
    if hasattr(proto.vnet, 'link_ifaces'):
        proto.vnet.link_ifaces.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.vrrp_engine.vrrp.clear()
    proto.vrrp_engine.hsrp.clear()

    async def noop(msg):
        pass

    for dev, ip in (('A', '10.9.9.1'), ('B', '10.9.9.2')):
        proto.vnet.register(dev, noop)
        proto.icmp_engine.register_device(
            dev, dev, {'Gi0/0': {'ip': ip, 'prefix': 24, 'status': 'up'}})
    proto.vnet.add_link('A', 'B', 'Gi0/0', 'Gi0/0')
    return proto.vrrp_engine


async def _settle(seconds=2.5):
    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_hsrp_elects_active_and_standby(two_routers):
    """priorityの高い方がActive、低い方がStandbyになる（両系Activeにならない）"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    assert e.hsrp['A'][1].state == 'Active', 'priority110の装置がActiveでない'
    assert e.hsrp['B'][1].state == 'Standby', \
        'priority100の装置がStandbyでない（スプリットブレイン）'


@pytest.mark.asyncio
async def test_hsrp_reports_peer_address(two_routers):
    """show standby が対向のIPとpriorityを表示する（unknownではない）"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    out_a = e.format_show_standby('A')
    assert 'Standby router is 10.9.9.2' in out_a, out_a
    out_b = e.format_show_standby('B')
    assert 'Active router is 10.9.9.1' in out_b, out_b


@pytest.mark.asyncio
async def test_hsrp_failover_on_interface_shutdown(two_routers):
    """Active側のIFをshutdownすると、Active側はInit・対向がActiveへ昇格"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()
    assert e.hsrp['A'][1].state == 'Active'

    proto.vnet.interface_down('A', 'Gi0/0')
    await e.interface_down('A', 'Gi0/0')
    await _settle(5)

    assert e.hsrp['A'][1].state == 'Init', \
        'IFがdownしたのにActiveのまま（両系Activeになる）'
    assert e.hsrp['B'][1].state == 'Active', 'Standby側が昇格していない'


@pytest.mark.asyncio
async def test_hsrp_preempt_restores_active_after_recovery(two_routers):
    """IF復旧後、preempt有効なpriority高の装置がActiveに戻る"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    proto.vnet.interface_down('A', 'Gi0/0')
    await e.interface_down('A', 'Gi0/0')
    await _settle(5)
    assert e.hsrp['B'][1].state == 'Active'

    proto.vnet.interface_up('A', 'Gi0/0')
    await e.interface_up('A', 'Gi0/0')
    await _settle(5)

    assert e.hsrp['A'][1].state == 'Active', 'preemptでActiveに戻っていない'
    assert e.hsrp['B'][1].state == 'Standby'


@pytest.mark.asyncio
async def test_vrrp_elects_master_and_backup(two_routers):
    """VRRPも同様にMaster/Backupが選出される"""
    e = two_routers
    await e.vrrp_start('A', 2, '10.9.9.253', priority=120, interface='Gi0/0')
    await e.vrrp_start('B', 2, '10.9.9.253', priority=100, interface='Gi0/0')
    await _settle()

    assert e.vrrp['A'][2].state == 'Master'
    assert e.vrrp['B'][2].state == 'Backup', \
        'priorityが低い側がBackupでない（スプリットブレイン）'
    assert 'Master Router is 10.9.9.1' in e.format_show_vrrp('B')


@pytest.mark.asyncio
async def test_vrrp_failover_on_interface_shutdown(two_routers):
    """VRRPのフェイルオーバー: Master側IF断でBackupが昇格"""
    e = two_routers
    await e.vrrp_start('A', 2, '10.9.9.253', priority=120, interface='Gi0/0')
    await e.vrrp_start('B', 2, '10.9.9.253', priority=100, interface='Gi0/0')
    await _settle()
    assert e.vrrp['A'][2].state == 'Master'

    proto.vnet.interface_down('A', 'Gi0/0')
    await e.interface_down('A', 'Gi0/0')
    await _settle(5)

    assert e.vrrp['A'][2].state == 'Init', 'IF断でInitに落ちていない'
    assert e.vrrp['B'][2].state == 'Master', 'Backupが昇格していない'


@pytest.mark.asyncio
async def test_shutdown_blocks_reception_not_only_transmission(two_routers):
    """自分のIFがdownしている装置は、対向からのHelloも受信しない

    送信側のdownしか見ていないと、shutdownした装置が対向のHelloを
    受け取り続け、Initに落とした直後に再選出されてしまう。
    """
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    proto.vnet.interface_down('A', 'Gi0/0')
    await e.interface_down('A', 'Gi0/0')
    # Bは生きているのでHelloを送り続けるが、AのIFはdownなので届かない
    await _settle(5)
    assert e.hsrp['A'][1].state == 'Init', \
        'downしているIFで対向のHelloを受信して再選出されている'


# ── HSRP object tracking（standby track） ────────────────────

@pytest.mark.asyncio
async def test_track_down_lowers_priority_and_triggers_preempt(two_routers):
    """トラック対象IFがdownするとpriorityが下がり、対向がpreemptで昇格する

    実機はActive自身が自発的に降格するのではなく、priorityが下がった
    ことをHelloで知った対向がpreemptで奪い取る、という順序で動く。
    """
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, preempt=True,
                       interface='Gi0/0')
    await _settle()
    assert e.hsrp['A'][1].state == 'Active'

    e.hsrp_set_track('A', 1, 'Gi0/1', decrement=20)
    await e.hsrp_track_down('A', 'Gi0/1')
    await _settle(3)

    assert e.hsrp['A'][1].priority == 90, e.hsrp['A'][1].priority
    assert e.hsrp['B'][1].state == 'Active', \
        'priority低下後もBがpreemptで昇格していない'
    assert e.hsrp['A'][1].state == 'Standby'


@pytest.mark.asyncio
async def test_track_up_restores_priority_and_preempt_takes_it_back(two_routers):
    """トラック対象IF復旧でpriorityが戻り、preemptでActiveを奪還する"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, preempt=True,
                       interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, preempt=True,
                       interface='Gi0/0')
    await _settle()
    e.hsrp_set_track('A', 1, 'Gi0/1', decrement=20)
    await e.hsrp_track_down('A', 'Gi0/1')
    await _settle(3)
    assert e.hsrp['B'][1].state == 'Active'

    await e.hsrp_track_up('A', 'Gi0/1')
    await _settle(3)

    assert e.hsrp['A'][1].priority == 110
    assert e.hsrp['A'][1].state == 'Active', 'priority復元後もpreemptで奪還していない'
    assert e.hsrp['B'][1].state == 'Standby'


@pytest.mark.asyncio
async def test_track_priority_never_goes_negative(two_routers):
    """decrementの合計が元のpriorityを超えても0未満にならない"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=15, interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=5, interface='Gi0/0')
    await _settle()

    e.hsrp_set_track('A', 1, 'Gi0/1', decrement=30)
    await e.hsrp_track_down('A', 'Gi0/1')
    await _settle(1)

    assert e.hsrp['A'][1].priority == 0, e.hsrp['A'][1].priority


@pytest.mark.asyncio
async def test_track_show_standby_reports_state_and_decrement(two_routers):
    """show standby にトラック対象・状態・decrementが表示される"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    e.hsrp_set_track('A', 1, 'Gi0/1', decrement=20)
    out_before = e.format_show_standby('A')
    assert 'Track interface Gi0/1 state Up decrement 20' in out_before, out_before

    await e.hsrp_track_down('A', 'Gi0/1')
    await _settle(1)
    out_after = e.format_show_standby('A')
    assert 'Track interface Gi0/1 state Down decrement 20' in out_after, out_after
    assert 'Priority 90 (configured 110)' in out_after, out_after


@pytest.mark.asyncio
async def test_multiple_tracked_interfaces_decrement_cumulatively(two_routers):
    """複数トラック対象がdownすると合計decrement分だけ下がる（累積誤差なし）"""
    e = two_routers
    await e.hsrp_start('A', 1, '10.9.9.254', priority=110, interface='Gi0/0')
    await e.hsrp_start('B', 1, '10.9.9.254', priority=100, interface='Gi0/0')
    await _settle()

    e.hsrp_set_track('A', 1, 'Gi0/1', decrement=10)
    e.hsrp_set_track('A', 1, 'Gi0/2', decrement=15)
    await e.hsrp_track_down('A', 'Gi0/1')
    await e.hsrp_track_down('A', 'Gi0/2')
    await _settle(1)
    assert e.hsrp['A'][1].priority == 85, e.hsrp['A'][1].priority

    await e.hsrp_track_up('A', 'Gi0/1')
    await _settle(1)
    assert e.hsrp['A'][1].priority == 95, e.hsrp['A'][1].priority

    await e.hsrp_track_up('A', 'Gi0/2')
    await _settle(1)
    assert e.hsrp['A'][1].priority == 110, e.hsrp['A'][1].priority
