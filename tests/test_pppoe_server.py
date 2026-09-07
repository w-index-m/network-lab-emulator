"""
Cisco IOS の PPPoE サーバ（アクセスコンセントレータ）機能。

実機の代表的な構成:
    bba-group pppoe global
     virtual-template 1
    interface Virtual-Template1
     ip unnumbered <上流IF>
     peer default ip address pool POOL
     ppp authentication chap pap
    ip local pool POOL 192.168.100.10 192.168.100.100
    interface GigabitEthernet0/1
     pppoe enable group global

これらのコマンドが受理され、show running-config / show bba-group /
show pppoe session / show ip local pool に反映されることを確認する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
os.environ.setdefault('NETLAB_FAST_TIMERS', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='cisco'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


def _run(id_, cmds):
    out = ''
    for c in cmds:
        out = _cli(id_, c)
    return out


PPPOE_SETUP = [
    'configure terminal',
    'ip local pool POOL 192.168.100.10 192.168.100.100',
    'bba-group pppoe global',
    'virtual-template 1',
    'exit',
    'interface Virtual-Template1',
    'ip unnumbered GigabitEthernet0/0',
    'peer default ip address pool POOL',
    'ppp authentication chap pap',
    'exit',
    'interface GigabitEthernet0/1',
    'pppoe enable group global',
    'end',
]


class TestBbaGroupMode:
    def test_bba_group_creates_submode_and_stores_virtual_template(self):
        _dev('pppoe-r1')
        _run('pppoe-r1', PPPOE_SETUP)
        out = _cli('pppoe-r1', 'show bba-group')
        assert 'bba-group pppoe global' in out
        assert 'virtual-template 1' in out

    def test_virtual_template_interface_is_auto_created(self):
        _dev('pppoe-r2')
        _run('pppoe-r2', PPPOE_SETUP)
        out = _cli('pppoe-r2', 'show ip interface brief')
        assert 'Virtual-Template1' in out


class TestIpLocalPool:
    def test_ip_local_pool_shows_in_show_command(self):
        _dev('pppoe-r3')
        _run('pppoe-r3', PPPOE_SETUP)
        out = _cli('pppoe-r3', 'show ip local pool')
        assert 'POOL' in out
        assert '192.168.100.10' in out
        assert '192.168.100.100' in out


class TestPppoeSessionShow:
    def test_show_pppoe_session_lists_bound_interface(self):
        _dev('pppoe-r4')
        _run('pppoe-r4', PPPOE_SETUP)
        out = _cli('pppoe-r4', 'show pppoe session')
        assert 'GigabitEthernet0/1' in out
        assert 'Vi1' in out

    def test_no_sessions_when_nothing_bound(self):
        _dev('pppoe-r5')
        _run('pppoe-r5', ['configure terminal', 'bba-group pppoe global',
                           'virtual-template 1', 'end'])
        out = _cli('pppoe-r5', 'show pppoe session')
        assert '0 sessions' in out


class TestRunningConfig:
    def test_running_config_reflects_pppoe_server(self):
        _dev('pppoe-r6')
        _run('pppoe-r6', PPPOE_SETUP)
        out = _cli('pppoe-r6', 'show running-config')
        assert 'ip local pool POOL 192.168.100.10 192.168.100.100' in out
        assert 'bba-group pppoe global' in out
        assert 'virtual-template 1' in out
        assert 'interface Virtual-Template1' in out
        assert 'ip unnumbered GigabitEthernet0/0' in out
        assert 'peer default ip address pool POOL' in out
        assert 'ppp authentication chap pap' in out
        assert 'pppoe enable group global' in out


class TestPeerPoolWithoutBbaGroup:
    def test_virtual_template_settings_independent_of_bba_group(self):
        # 実機同様、Virtual-TemplateのIF設定自体はbba-groupが無くても入る
        _dev('pppoe-r7')
        _run('pppoe-r7', [
            'configure terminal',
            'interface Virtual-Template2',
            'peer default ip address pool POOL2',
            'end',
        ])
        out = _cli('pppoe-r7', 'show running-config')
        assert 'interface Virtual-Template2' in out
        assert 'peer default ip address pool POOL2' in out
