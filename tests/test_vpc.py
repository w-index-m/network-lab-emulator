"""
Nexus vPC（仮想ポートチャネル）のテスト

構成・ロール選出・ピアキープアライブ・メンバーvPC・障害時の状態遷移を
検証する。手順とCLIの実行例は docs/nexus-vpc-howto.md を参照。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto


@pytest.fixture
async def two_nexus():
    """peer-link で結ばれた Nexus 2台。N9K-1 が role priority 100（小さい=primary）"""
    proto.vnet.links.clear()
    proto.vnet.interface_links.clear()
    proto.vnet.down_interfaces.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.vpc_engine.domains.clear()
    proto.vpc_engine.peers.clear()
    proto.vpc_engine.feature_enabled.clear()

    async def noop(msg):
        pass

    for dev in ('n1', 'n2'):
        proto.vnet.register(dev, noop)
        proto.vpc_engine.enable_feature(dev)
    proto.vnet.add_link('n1', 'n2', 'Ethernet1/1', 'Ethernet1/1')
    return proto.vpc_engine


async def _setup_vpc(e, keepalive=True):
    for dev, prio, dest, src in (
        ('n1', 100, '192.168.100.2', '192.168.100.1'),
        ('n2', 200, '192.168.100.1', '192.168.100.2'),
    ):
        e.create_domain(dev, 10)
        e.set_role_priority(dev, prio)
        if keepalive:
            e.set_keepalive(dev, dest, src)
        e.set_peer_link(dev, 'port-channel1')
    await asyncio.sleep(2)


@pytest.mark.asyncio
async def test_vpc_peer_comes_up(two_nexus):
    """両機の設定が揃うとピアが alive になる"""
    e = two_nexus
    await _setup_vpc(e)

    d1 = e.domains['n1']
    assert d1.domain_id == 10
    assert d1.keepalive_state == 'alive', \
        f'ピアキープアライブが上がっていない: {d1.keepalive_state}'


@pytest.mark.asyncio
async def test_vpc_role_election_lower_priority_wins(two_nexus):
    """role priority が小さい方が primary になる（HSRP等と逆なので注意）"""
    e = two_nexus
    await _setup_vpc(e)

    assert e.domains['n1'].role == 'primary', \
        f"priority100側がprimaryでない: {e.domains['n1'].role}"
    assert e.domains['n2'].role == 'secondary', \
        f"priority200側がsecondaryでない: {e.domains['n2'].role}"


@pytest.mark.asyncio
async def test_vpc_members_show_up(two_nexus):
    """vPCメンバーを両機に設定すると show vpc に up で並ぶ"""
    e = two_nexus
    await _setup_vpc(e)
    for dev in ('n1', 'n2'):
        e.add_vpc_member(dev, 'port-channel10', 10)
        e.add_vpc_member(dev, 'port-channel20', 20)
    await asyncio.sleep(1)

    out = e.format_show_vpc('n1')
    assert 'Number of vPCs configured          : 2' in out, out
    assert 'port-channel10' in out
    assert 'port-channel20' in out
    # peer-link と各vPCが up であること
    assert 'up' in out


@pytest.mark.asyncio
async def test_show_vpc_reports_peer_and_keepalive_alive(two_nexus):
    """show vpc の Peer status / keep-alive status が alive を示す"""
    e = two_nexus
    await _setup_vpc(e)

    out = e.format_show_vpc('n1')
    assert 'Peer status                        : alive' in out, out
    assert 'vPC keep-alive status              : alive' in out, out
    assert 'vPC role                           : primary' in out, out


@pytest.mark.asyncio
async def test_peer_failure_marks_peer_dead(two_nexus):
    """ピア障害で Peer status が dead になる"""
    e = two_nexus
    await _setup_vpc(e)
    assert e.domains['n1'].keepalive_state == 'alive'

    await e.simulate_peer_failure('n1')
    await asyncio.sleep(0.5)

    assert e.domains['n1'].keepalive_state == 'dead', \
        'ピア障害後もaliveのまま'


@pytest.mark.asyncio
async def test_peer_link_failure_marks_link_down(two_nexus):
    """peer-link 障害で peer-link が down になる"""
    e = two_nexus
    await _setup_vpc(e)
    assert e.domains['n1'].peer_link_state == 'up'

    await e.simulate_peer_link_failure('n1')
    await asyncio.sleep(0.5)

    assert e.domains['n1'].peer_link_state == 'down', \
        'peer-link障害後もupのまま'
    assert 'port-channel1 (down)' in e.format_show_vpc_brief('n1')


@pytest.mark.asyncio
async def test_single_switch_stays_pending(two_nexus):
    """対向が設定されていなければ pending のまま上がらない

    vPCは2台揃って初めて成立する。1台だけ設定して alive になって
    しまうと、実際にはピアが居ないのに冗長化できているように
    見えてしまう。
    """
    e = two_nexus
    e.create_domain('n1', 10)
    e.set_role_priority('n1', 100)
    e.set_keepalive('n1', '192.168.100.2', '192.168.100.1')
    e.set_peer_link('n1', 'port-channel1')
    await asyncio.sleep(2)

    assert e.domains['n1'].keepalive_state != 'alive', \
        '対向不在なのにキープアライブがaliveになっている'
    out = e.format_show_vpc('n1')
    # "keep-alive" という語自体に 'alive' が含まれるため、値の行で判定する
    assert 'Peer status                        : pending' in out, out
    assert 'vPC keep-alive status              : pending' in out, out
