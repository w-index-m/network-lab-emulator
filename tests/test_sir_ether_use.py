"""
Si-R のインタフェース使用/不使用（ether <group> <port> use on|off）の実装。

Si-R には Cisco の shutdown / no shutdown が無く、実機で使えるのは
コマンドリファレンス 4.1.2 の `ether <group> <port> use <mode>` だけ。
これが未実装だったため、Si-R では
  - リンクを落とす手段が無い
  - したがって linkDown トラップも出せない
  - OSPF/RIP の Dead タイマー試験ができない
という状態だった。

ether → VLAN → lan の対応（ether vlan untag <vid> / lan <n> vlan <vid>）を
たどって、落ちる lan インタフェースを決めている点もあわせて確認する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module
from engine.protocols import vnet

client = TestClient(app_module.app)


def _dev(id_, type_='sir'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


class TestEtherUseCommand:
    def test_use_off_marks_port_disable_in_show_ether_brief(self):
        _dev('sir-u1')
        _run('sir-u1', ['configure', 'lan 1 ip address 192.168.50.1/24 3'])
        out = _cli('sir-u1', 'show ether brief')
        # 見出しが実機と同じ桁組みであること
        assert 'port  status   type               media  mdi   speed  duplex  flow' in out
        assert '2 1' in out

        _run('sir-u1', ['configure', 'ether 2 1 use off'])
        out = _cli('sir-u1', 'show ether brief')
        line = [ln for ln in out.splitlines() if ln.startswith('2 1')][0]
        # 実機は「定義により使用しない」状態を disable と表示する
        assert 'disable' in line, line

        _run('sir-u1', ['configure', 'ether 2 1 use on'])
        line = [ln for ln in _cli('sir-u1', 'show ether brief').splitlines()
                if ln.startswith('2 1')][0]
        assert 'disable' not in line, line

    def test_use_off_brings_down_the_bound_lan_interface(self):
        _dev('sir-u2')
        _run('sir-u2', ['configure', 'lan 1 ip address 192.168.51.1/24 3'])
        assert 'lan1' not in vnet.down_interfaces.get('sir-u2', set())

        _run('sir-u2', ['configure', 'ether 2 1 use off'])
        # ether 2 1 → vlan 2 → lan 1 の順にたどって lan1 が落ちる
        assert 'lan1' in vnet.down_interfaces.get('sir-u2', set())

        _run('sir-u2', ['configure', 'ether 2 1 use on'])
        assert 'lan1' not in vnet.down_interfaces.get('sir-u2', set())

    def test_port_list_notation_is_expanded(self):
        _dev('sir-u3')
        _run('sir-u3', ['configure', 'ether 2 1,3-4 use off'])
        out = _cli('sir-u3', 'show ether brief')
        for port in ('2 1', '2 3', '2 4'):
            line = [ln for ln in out.splitlines() if ln.startswith(port)][0]
            assert 'disable' in line, f'{port}: {line}'
        line = [ln for ln in out.splitlines() if ln.startswith('2 2')][0]
        assert 'disable' not in line, f'指定していない 2 2 まで落ちた: {line}'

    def test_nonexistent_port_is_rejected(self):
        _dev('sir-u4')
        out = _run('sir-u4', ['configure', 'ether 2 9 use off'])
        assert 'format error' in out, out

    def test_shutdown_is_not_a_sir_command(self):
        # Si-Rに shutdown は無い。これでリンクが落ちてしまうと
        # 実機と食い違い、試験結果を信用できなくなる
        _dev('sir-u5')
        _run('sir-u5', ['configure', 'lan 1 ip address 192.168.55.1/24 3'])
        _run('sir-u5', ['configure', 'shutdown'])
        assert 'lan1' not in vnet.down_interfaces.get('sir-u5', set())


class TestLanVlanBinding:
    def test_lan_vlan_command_changes_which_lan_goes_down(self):
        _dev('sir-b1')
        _run('sir-b1', [
            'configure',
            'lan 3 ip address 192.168.52.1/24 3',
            'lan 3 vlan 2',          # lan3 を VLAN 2 に付け替える
            'lan 1 vlan 99',         # lan1 は別VLANへ退避
        ])
        _run('sir-b1', ['configure', 'ether 2 1 use off'])
        down = vnet.down_interfaces.get('sir-b1', set())
        assert 'lan3' in down, f'付け替えたlan3が落ちていない: {down}'
        assert 'lan1' not in down, f'関係のないlan1まで落ちた: {down}'

    def test_ether_vlan_untag_changes_the_mapping(self):
        _dev('sir-b2')
        _run('sir-b2', [
            'configure',
            'lan 0 ip address 192.168.53.1/24 3',
            'ether 2 1 vlan untag 1',   # ether 2 1 を VLAN1(=lan0) 側へ
        ])
        _run('sir-b2', ['configure', 'ether 2 1 use off'])
        down = vnet.down_interfaces.get('sir-b2', set())
        assert 'lan0' in down, f'VLAN付け替え後のlan0が落ちていない: {down}'


class TestLinkTrapSuppression:
    def test_trap_command_is_accepted(self):
        _dev('sir-t1')
        assert _run('sir-t1', ['configure',
                               'ether 2 1 snmp trap linkdown disable']) == ''
        assert _run('sir-t1', ['configure',
                               'ether 2 1 snmp trap linkup enable']) == ''

    def test_linkdown_trap_suppressed_when_disabled(self):
        import app as m
        sent = []
        orig = m.snmp_dispatcher.emit

        async def _spy(device_id, hostname, oid, msg):
            sent.append((device_id, oid))
            return await orig(device_id, hostname, oid, msg)

        m.snmp_dispatcher.emit = _spy
        try:
            _dev('sir-t2')
            _run('sir-t2', ['configure', 'lan 1 ip address 192.168.54.1/24 3'])
            _run('sir-t2', ['configure', 'ether 2 1 snmp trap linkdown disable'])
            sent.clear()
            _run('sir-t2', ['configure', 'ether 2 1 use off'])
            downs = [x for x in sent if x[1] == '1.3.6.1.6.3.1.1.5.3']
            assert not downs, f'抑止したのにlinkDownトラップが出た: {downs}'

            # linkup は enable のままなので、復旧時のトラップは出る
            sent.clear()
            _run('sir-t2', ['configure', 'ether 2 1 use on'])
            ups = [x for x in sent if x[1] == '1.3.6.1.6.3.1.1.5.4']
            assert ups, 'linkUpトラップが出ていない'
        finally:
            m.snmp_dispatcher.emit = orig
