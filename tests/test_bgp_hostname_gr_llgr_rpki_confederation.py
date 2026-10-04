"""
vincentbernat/network-lab から持ち込んだ4つのBGP機能:
- lab-bgp-hostname: BGP Hostname Capability
- lab-bgp-graceful-restart / lab-bgp-llgr: Graceful Restart / Long-Lived GR
- lab-bgp-rpki: RPKI Origin Validation
- lab-bgp-confederation: BGP Confederation

いずれも新しいエンジンを作らず、既存の`engine/protocols.py`の
`BgpEngine`に機能を足す形にした。`tests/test_bgp_advanced.py`と同じ
方式(engine.protocolsをpytest-asyncioで直接操作)で書く — BGPの
セッション確立/タイマーは実際の非同期FSM(asyncio.sleep)なので、
同期TestClient越しだとタイマーが進まず確立しない。

**Hostname Capability**: 実機(FRR/BIRD)は自動ネゴシエーションだが、
このエミュレータでは`bgp hostname-capability`で明示的にトグルする。
双方で有効な場合のみ`show ip bgp summary`のNeighbor列が
"hostname(ip)"形式になり、`show ip bgp neighbors`にも
Hostname Capability行が出る。

**Graceful Restart/LLGR**: セッション断時、即座にrib_inから経路を
消すのではなくstale化して保持する(GRは`restart_time`秒以内に
再確立できなければ本当に撤去、LLGRはタイマーでは消さず保持し続ける)。
ベストパス選択(`_recalc_best_path`の`_better()`)はstaleを常に
非staleより劣後させる。

**RPKI**: `bgp rpki roa <prefix>/<len> max-length <max> origin-as <as>`
で登録したROAテーブルと、学習した経路のorigin AS(as_pathの末尾)を
照合し valid/invalid/notfound を判定する。`bgp rpki invalid-drop`で
invalidをインバウンドの時点で拒否できる。

**Confederation**: `bgp confederation identifier <as>` /
`bgp confederation peers <as...>` は実機そのままのCisco IOS構文。
加盟国(confederation peers)同士のeBGPはas_pathに自分のsub-AS番号を
積むが、真の外部eBGPへ出る際には内部のsub-AS群を全て隠し、
confederation identifierの1つだけに圧縮して見せる。

Live-verified(TestClientスクリプト、本テストとは別に実施): 4機能
それぞれ単体で動作確認済み。本テストはその固定。
"""

import asyncio
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import engine.protocols as proto
import app as app_module
from engine.rules import DeviceState


@pytest.fixture
def fresh_engines():
    proto.vnet.links.clear()
    proto.vnet.ws_send_callbacks.clear()
    proto.bgp_engine.nodes.clear()
    proto.rib_engine.nodes.clear()

    async def noop(msg):
        pass

    return {
        'vnet': proto.vnet, 'bgp': proto.bgp_engine,
        'rib': proto.rib_engine, 'noop': noop,
    }


def _link(e, a, b):
    e['vnet'].register(a, e['noop'])
    e['vnet'].register(b, e['noop'])
    e['vnet'].add_link(a, b)


class TestBgpHostnameCapability:
    @pytest.mark.asyncio
    async def test_summary_shows_hostname_when_both_sides_enable_it(self, fresh_engines):
        e = fresh_engines
        _link(e, 'HA', 'HB')
        await e['bgp'].start('HA', 'RouterA', 65001)
        await e['bgp'].start('HB', 'RouterB', 65002)
        e['bgp'].set_hostname_capability('HA', True)
        e['bgp'].set_hostname_capability('HB', True)
        await e['bgp'].add_neighbor('HA', 'HB', 'RouterB', 65002)
        await e['bgp'].add_neighbor('HB', 'HA', 'RouterA', 65001)
        await asyncio.sleep(3)

        summary = e['bgp'].format_show_bgp_summary('HA')
        assert 'RouterB(' in summary
        nbrs = e['bgp'].format_show_bgp_neighbors('HA')
        assert 'Hostname Capability: advertised and received (Hostname RouterB)' in nbrs

    @pytest.mark.asyncio
    async def test_summary_shows_raw_ip_when_not_both_enabled(self, fresh_engines):
        e = fresh_engines
        _link(e, 'HC', 'HD')
        await e['bgp'].start('HC', 'RouterC', 65003)
        await e['bgp'].start('HD', 'RouterD', 65004)
        e['bgp'].set_hostname_capability('HC', True)
        # HDは有効にしない
        await e['bgp'].add_neighbor('HC', 'HD', 'RouterD', 65004)
        await e['bgp'].add_neighbor('HD', 'HC', 'RouterC', 65003)
        await asyncio.sleep(3)

        summary = e['bgp'].format_show_bgp_summary('HC')
        assert 'RouterD(' not in summary


class TestGracefulRestart:
    @pytest.mark.asyncio
    async def test_route_is_marked_stale_not_deleted_on_session_down(self, fresh_engines):
        e = fresh_engines
        _link(e, 'GA', 'GB')
        await e['bgp'].start('GA', 'GA', 65001)
        await e['bgp'].start('GB', 'GB', 65002)
        e['bgp'].set_graceful_restart('GA', True, restart_time=2)
        e['bgp'].set_graceful_restart('GB', True, restart_time=2)
        await e['bgp'].add_neighbor('GA', 'GB', 'GB', 65002)
        await e['bgp'].add_neighbor('GB', 'GA', 'GA', 65001)
        await e['bgp'].advertise_network('GB', '10.9.0.0/24')
        await asyncio.sleep(3)

        await e['bgp'].session_down('GA', 'GB', 'test')
        learned = {r.prefix: r for r in e['bgp'].nodes['GA']['rib_in']}
        assert '10.9.0.0' in learned
        assert learned['10.9.0.0'].stale is True

        table = e['bgp'].format_show_bgp_table('GA')
        assert '*s' in table

    @pytest.mark.asyncio
    async def test_stale_route_is_removed_after_restart_time_expires(self, fresh_engines):
        e = fresh_engines
        _link(e, 'GC', 'GD')
        await e['bgp'].start('GC', 'GC', 65001)
        await e['bgp'].start('GD', 'GD', 65002)
        e['bgp'].set_graceful_restart('GC', True, restart_time=2)
        e['bgp'].set_graceful_restart('GD', True, restart_time=2)
        await e['bgp'].add_neighbor('GC', 'GD', 'GD', 65002)
        await e['bgp'].add_neighbor('GD', 'GC', 'GC', 65001)
        await e['bgp'].advertise_network('GD', '10.11.0.0/24')
        await asyncio.sleep(3)

        await e['bgp'].session_down('GC', 'GD', 'test')
        await asyncio.sleep(4)  # restart_time=2 経過
        learned = [r.prefix for r in e['bgp'].nodes['GC']['rib_in']]
        assert '10.11.0.0' not in learned

    @pytest.mark.asyncio
    async def test_without_graceful_restart_route_is_removed_immediately(self, fresh_engines):
        """regression防止: GR未設定時は既存通り即時撤去されること。"""
        e = fresh_engines
        _link(e, 'GE', 'GF')
        await e['bgp'].start('GE', 'GE', 65001)
        await e['bgp'].start('GF', 'GF', 65002)
        await e['bgp'].add_neighbor('GE', 'GF', 'GF', 65002)
        await e['bgp'].add_neighbor('GF', 'GE', 'GE', 65001)
        await e['bgp'].advertise_network('GF', '10.12.0.0/24')
        await asyncio.sleep(3)

        await e['bgp'].session_down('GE', 'GF', 'test')
        learned = [r.prefix for r in e['bgp'].nodes['GE']['rib_in']]
        assert '10.12.0.0' not in learned


class TestLongLivedGracefulRestart:
    @pytest.mark.asyncio
    async def test_llgr_route_never_auto_expires(self, fresh_engines):
        e = fresh_engines
        _link(e, 'LA', 'LB')
        await e['bgp'].start('LA', 'LA', 65001)
        await e['bgp'].start('LB', 'LB', 65002)
        e['bgp'].set_llgr('LA', True)
        e['bgp'].set_llgr('LB', True)
        await e['bgp'].add_neighbor('LA', 'LB', 'LB', 65002)
        await e['bgp'].add_neighbor('LB', 'LA', 'LA', 65001)
        await e['bgp'].advertise_network('LB', '10.8.0.0/24')
        await asyncio.sleep(3)

        await e['bgp'].session_down('LA', 'LB', 'test')
        await asyncio.sleep(4)
        learned = {r.prefix: r for r in e['bgp'].nodes['LA']['rib_in']}
        assert '10.8.0.0' in learned
        assert learned['10.8.0.0'].stale is True

    @pytest.mark.asyncio
    async def test_non_stale_alternative_route_is_preferred_over_llgr_stale(self, fresh_engines):
        """stale経路は、別経路で同じprefixに到達できるなら
        ベストパスとしては選ばれない(常に非staleが優先される)。"""
        e = fresh_engines
        _link(e, 'LC', 'LD')
        _link(e, 'LC', 'LE')
        await e['bgp'].start('LC', 'LC', 65001)
        await e['bgp'].start('LD', 'LD', 65002)
        await e['bgp'].start('LE', 'LE', 65003)
        e['bgp'].set_llgr('LC', True)
        e['bgp'].set_llgr('LD', True)
        await e['bgp'].add_neighbor('LC', 'LD', 'LD', 65002)
        await e['bgp'].add_neighbor('LD', 'LC', 'LC', 65001)
        await e['bgp'].add_neighbor('LC', 'LE', 'LE', 65003)
        await e['bgp'].add_neighbor('LE', 'LC', 'LC', 65001)
        await e['bgp'].advertise_network('LD', '10.13.0.0/24')
        await asyncio.sleep(3)
        await e['bgp'].session_down('LC', 'LD', 'test')
        await asyncio.sleep(1)
        # LEから同じprefixを広告しなおす(別経路で到達可能にする)
        await e['bgp'].advertise_network('LE', '10.13.0.0/24')
        await asyncio.sleep(3)

        best = {r['prefix']: r for r in e['bgp'].nodes['LC']['loc_rib']}
        assert best['10.13.0.0']['learned_from'] == 'LE'
        assert best['10.13.0.0']['stale'] is False


class TestRpki:
    @pytest.mark.asyncio
    async def test_matching_roa_is_valid(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RA', 'RB')
        await e['bgp'].start('RA', 'RA', 65001)
        await e['bgp'].start('RB', 'RB', 65002)
        await e['bgp'].add_neighbor('RA', 'RB', 'RB', 65002)
        await e['bgp'].add_neighbor('RB', 'RA', 'RA', 65001)
        e['bgp'].add_roa('RA', '203.0.113.0', 24, 24, 65002)
        await e['bgp'].advertise_network('RB', '203.0.113.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['RA']['rib_in']}
        assert learned['203.0.113.0'].rpki_state == 'valid'

    @pytest.mark.asyncio
    async def test_no_covering_roa_is_notfound(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RC', 'RD')
        await e['bgp'].start('RC', 'RC', 65001)
        await e['bgp'].start('RD', 'RD', 65002)
        await e['bgp'].add_neighbor('RC', 'RD', 'RD', 65002)
        await e['bgp'].add_neighbor('RD', 'RC', 'RC', 65001)
        await e['bgp'].advertise_network('RD', '198.51.100.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['RC']['rib_in']}
        assert learned['198.51.100.0'].rpki_state == 'notfound'

    @pytest.mark.asyncio
    async def test_wrong_origin_as_is_invalid(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RE', 'RF')
        await e['bgp'].start('RE', 'RE', 65001)
        await e['bgp'].start('RF', 'RF', 65002)
        await e['bgp'].add_neighbor('RE', 'RF', 'RF', 65002)
        await e['bgp'].add_neighbor('RF', 'RE', 'RE', 65001)
        # ROAはAS 99999をoriginとして要求するが、実際の広告元はRF(AS65002)
        e['bgp'].add_roa('RE', '203.0.114.0', 24, 24, 99999)
        await e['bgp'].advertise_network('RF', '203.0.114.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['RE']['rib_in']}
        assert learned['203.0.114.0'].rpki_state == 'invalid'

    @pytest.mark.asyncio
    async def test_invalid_drop_rejects_the_route_entirely(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RG', 'RH')
        await e['bgp'].start('RG', 'RG', 65001)
        await e['bgp'].start('RH', 'RH', 65002)
        await e['bgp'].add_neighbor('RG', 'RH', 'RH', 65002)
        await e['bgp'].add_neighbor('RH', 'RG', 'RG', 65001)
        e['bgp'].add_roa('RG', '203.0.115.0', 24, 24, 99999)
        e['bgp'].set_rpki_invalid_drop('RG', True)
        await e['bgp'].advertise_network('RH', '203.0.115.0/24')
        await asyncio.sleep(3)

        learned = [r.prefix for r in e['bgp'].nodes['RG']['rib_in']]
        assert '203.0.115.0' not in learned

    @pytest.mark.asyncio
    async def test_max_length_exceeded_is_invalid(self, fresh_engines):
        e = fresh_engines
        _link(e, 'RI', 'RJ')
        await e['bgp'].start('RI', 'RI', 65001)
        await e['bgp'].start('RJ', 'RJ', 65002)
        await e['bgp'].add_neighbor('RI', 'RJ', 'RJ', 65002)
        await e['bgp'].add_neighbor('RJ', 'RI', 'RI', 65001)
        # ROAはmax-length 23までしか許さないが、広告は/24
        e['bgp'].add_roa('RI', '203.0.116.0', 23, 23, 65002)
        await e['bgp'].advertise_network('RJ', '203.0.116.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['RI']['rib_in']}
        assert learned['203.0.116.0'].rpki_state == 'invalid'


class TestConfederation:
    @pytest.mark.asyncio
    async def test_confed_peer_ebgp_shows_sub_as_in_path(self, fresh_engines):
        e = fresh_engines
        _link(e, 'CA', 'CB')
        await e['bgp'].start('CA', 'CA', 65010)
        await e['bgp'].start('CB', 'CB', 65020)
        e['bgp'].set_confederation_identifier('CA', 65000)
        e['bgp'].add_confederation_peer('CA', 65020)
        e['bgp'].set_confederation_identifier('CB', 65000)
        e['bgp'].add_confederation_peer('CB', 65010)
        await e['bgp'].add_neighbor('CA', 'CB', 'CB', 65020)
        await e['bgp'].add_neighbor('CB', 'CA', 'CA', 65010)
        await e['bgp'].advertise_network('CA', '172.20.0.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['CB']['rib_in']}
        assert learned['172.20.0.0'].as_path == [65010]

    @pytest.mark.asyncio
    async def test_external_ebgp_hides_sub_as_shows_only_confederation_id(self, fresh_engines):
        e = fresh_engines
        _link(e, 'CC', 'CD')
        _link(e, 'CD', 'CE')
        await e['bgp'].start('CC', 'CC', 65011)
        await e['bgp'].start('CD', 'CD', 65021)
        await e['bgp'].start('CE', 'CE', 65999)  # 真の外部AS
        e['bgp'].set_confederation_identifier('CC', 65001)
        e['bgp'].add_confederation_peer('CC', 65021)
        e['bgp'].set_confederation_identifier('CD', 65001)
        e['bgp'].add_confederation_peer('CD', 65011)
        await e['bgp'].add_neighbor('CC', 'CD', 'CD', 65021)
        await e['bgp'].add_neighbor('CD', 'CC', 'CC', 65011)
        await e['bgp'].add_neighbor('CD', 'CE', 'CE', 65999)
        await e['bgp'].add_neighbor('CE', 'CD', 'CD', 65021)
        await e['bgp'].advertise_network('CC', '172.21.0.0/24')
        await asyncio.sleep(4)

        learned = {r.prefix: r for r in e['bgp'].nodes['CE']['rib_in']}
        assert learned['172.21.0.0'].as_path == [65001]
        # confederation内部のsub-AS(65011/65021)は外からは一切見えない
        assert 65011 not in learned['172.21.0.0'].as_path
        assert 65021 not in learned['172.21.0.0'].as_path

    @pytest.mark.asyncio
    async def test_without_confederation_identifier_behaves_like_plain_ebgp(self, fresh_engines):
        """regression防止: confederation未設定時は既存のeBGP挙動のまま。"""
        e = fresh_engines
        _link(e, 'CF', 'CG')
        await e['bgp'].start('CF', 'CF', 65031)
        await e['bgp'].start('CG', 'CG', 65032)
        await e['bgp'].add_neighbor('CF', 'CG', 'CG', 65032)
        await e['bgp'].add_neighbor('CG', 'CF', 'CF', 65031)
        await e['bgp'].advertise_network('CF', '172.22.0.0/24')
        await asyncio.sleep(3)

        learned = {r.prefix: r for r in e['bgp'].nodes['CG']['rib_in']}
        assert learned['172.22.0.0'].as_path == [65031]


class TestCliParsing:
    """app.py側の新規CLI正規表現が bgp_engine を正しく呼び出すこと。
    セッション確立の非同期タイマーには依存しない。"""

    def _dev(self, dev_id, hostname='R'):
        proto.bgp_engine.nodes.pop(dev_id, None)
        st = DeviceState('cisco', hostname)
        app_module.device_sessions[dev_id] = st
        return dev_id, st

    def _aconf(self, dev, st, cmds):
        async def _go():
            out = ''
            for c in cmds:
                out = await app_module.handle_protocol_config(dev, c, st)
            return out
        return asyncio.run(_go())

    def test_hostname_capability_command(self):
        dev, st = self._dev('cli-host-1')
        self._aconf(dev, st, ['router bgp 65000', 'bgp hostname-capability'])
        assert proto.bgp_engine.nodes[dev]['hostname_capability'] is True

    def test_graceful_restart_and_restart_time_commands(self):
        dev, st = self._dev('cli-gr-1')
        self._aconf(dev, st, ['router bgp 65000',
                              'bgp graceful-restart restart-time 90'])
        n = proto.bgp_engine.nodes[dev]
        assert n['graceful_restart_default'] is True
        assert n['gr_restart_time'] == 90

    def test_llgr_command(self):
        dev, st = self._dev('cli-llgr-1')
        self._aconf(dev, st, ['router bgp 65000', 'bgp long-lived-graceful-restart'])
        assert proto.bgp_engine.nodes[dev]['llgr_default'] is True

    def test_confederation_identifier_and_peers_commands(self):
        dev, st = self._dev('cli-confed-1')
        self._aconf(dev, st, ['router bgp 65011',
                              'bgp confederation identifier 65000',
                              'bgp confederation peers 65012 65013'])
        n = proto.bgp_engine.nodes[dev]
        assert n['confederation_id'] == 65000
        assert n['confederation_peers'] == {65012, 65013}

    def test_rpki_roa_and_invalid_drop_commands(self):
        dev, st = self._dev('cli-rpki-1')
        self._aconf(dev, st, ['router bgp 65000',
                              'bgp rpki roa 10.0.0.0/8 max-length 24 origin-as 65001',
                              'bgp rpki invalid-drop'])
        n = proto.bgp_engine.nodes[dev]
        assert n['roas'] == [{'prefix': '10.0.0.0', 'prefix_len': 8,
                              'max_length': 24, 'origin_as': 65001}]
        assert n['rpki_invalid_drop'] is True
