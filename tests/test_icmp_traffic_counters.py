"""
show ip traffic のICMPカウンタ（Cisco/Catalyst）。

以前は`show ip traffic`自体が未実装だった。ダッシュボードで
「ICMP関連の流量も見たい」という要望を機に、ping()実行のたびに
実際にICMPパケット相当のカウンタを積み上げ、show ip trafficの
ICMPセクションに反映するようにした。

トポロジー上で実際にpingが通った/通らなかった分だけカウントされる
（本物のパケット送受信は無いが、シミュレーション上のイベント発生を
1回=1パケットとして扱う）。
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


TOPO_SETUP = [
    'configure terminal',
    'interface GigabitEthernet0/1',
    'ip address 10.20.0.1 255.255.255.0',
    'no shutdown',
    'end',
]


class TestShowIpTrafficBaseline:
    def test_zero_counters_before_any_ping(self):
        _dev('icmp-r1')
        out = _cli('icmp-r1', 'show ip traffic')
        assert 'ICMP statistics' in out
        assert '0 echo, 0 echo reply' in out


class TestShowIpTrafficAfterPing:
    def test_successful_ping_increments_echo_counters(self):
        _dev('icmp-r2')
        _dev('icmp-r3')
        client.post('/api/link', json={'a': 'icmp-r2', 'b': 'icmp-r3',
                                       'iface_a': 'GigabitEthernet0/1',
                                       'iface_b': 'GigabitEthernet0/1'})
        _run('icmp-r2', TOPO_SETUP)
        _run('icmp-r3', ['configure terminal', 'interface GigabitEthernet0/1',
                          'ip address 10.20.0.2 255.255.255.0', 'no shutdown', 'end'])

        _cli('icmp-r2', 'ping 10.20.0.2')

        out_src = _cli('icmp-r2', 'show ip traffic')
        assert 'Sent:  0 redirects, 0 unreachable, 5 echo, 0 echo reply' in out_src
        assert ('Rcvd:  0 format errors, 0 checksum errors, 0 redirects, '
                '0 unreachable\n         0 echo, 5 echo reply') in out_src

        out_dst = _cli('icmp-r3', 'show ip traffic')
        assert ('Rcvd:  0 format errors, 0 checksum errors, 0 redirects, '
                '0 unreachable\n         5 echo, 0 echo reply') in out_dst
        assert 'Sent:  0 redirects, 0 unreachable, 0 echo, 5 echo reply' in out_dst

    def test_unreachable_ping_increments_unreachable_rcvd(self):
        _dev('icmp-r4')
        _run('icmp-r4', TOPO_SETUP)
        _cli('icmp-r4', 'ping 192.0.2.99')  # 到達不能な宛先
        out = _cli('icmp-r4', 'show ip traffic')
        assert '5 unreachable\n         0 echo, 0 echo reply' in out


class TestClearIpTraffic:
    def test_clear_ip_traffic_resets_counters(self):
        _dev('icmp-r5')
        _dev('icmp-r6')
        client.post('/api/link', json={'a': 'icmp-r5', 'b': 'icmp-r6',
                                       'iface_a': 'GigabitEthernet0/1',
                                       'iface_b': 'GigabitEthernet0/1'})
        _run('icmp-r5', TOPO_SETUP)
        _run('icmp-r6', ['configure terminal', 'interface GigabitEthernet0/1',
                          'ip address 10.20.0.2 255.255.255.0', 'no shutdown', 'end'])
        _cli('icmp-r5', 'ping 10.20.0.2')
        assert '5 echo, 0 echo reply' in _cli('icmp-r5', 'show ip traffic')

        _cli('icmp-r5', 'clear ip traffic')
        out = _cli('icmp-r5', 'show ip traffic')
        assert '0 echo, 0 echo reply' in out


class TestIcmpRedirectDetection:
    """ICMP Redirect発生条件（非対称経路）の検出。

    典型的な発生条件: ホストのデフォルトゲートウェイ(R1)が、宛先への
    最適経路としてホストと同一サブネット上の別ルータ(R2)を持っている
    場合、R1はホストにICMP Redirectを送り返す。

    構成: [host] --- [switch] --- [R1]
                          |
                        [R2] --- [host2]
    host のデフォルトGWはR1。R1はhost2向けのネットワークをR2(host と
    同一サブネット)経由と学習しているため、pingのたびにRedirectが
    発生する条件になっている。
    """

    def _build_asymmetric_topology(self, prefix):
        sw, host, r1, r2, host2 = (
            f'{prefix}-sw', f'{prefix}-host', f'{prefix}-r1',
            f'{prefix}-r2', f'{prefix}-host2')
        _dev(sw, type_='catalyst')
        _dev(host)
        _dev(r1)
        _dev(r2)
        _dev(host2)
        client.post('/api/link', json={'a': host, 'b': sw,
                                       'iface_a': 'GigabitEthernet0/1', 'iface_b': 'GigabitEthernet1/0/1'})
        client.post('/api/link', json={'a': r1, 'b': sw,
                                       'iface_a': 'GigabitEthernet0/1', 'iface_b': 'GigabitEthernet1/0/2'})
        client.post('/api/link', json={'a': r2, 'b': sw,
                                       'iface_a': 'GigabitEthernet0/1', 'iface_b': 'GigabitEthernet1/0/3'})
        client.post('/api/link', json={'a': r2, 'b': host2,
                                       'iface_a': 'GigabitEthernet0/2', 'iface_b': 'GigabitEthernet0/1'})

        _run(host, ['configure terminal', 'interface GigabitEthernet0/1',
                     'ip address 192.168.90.10 255.255.255.0', 'no shutdown', 'exit',
                     'ip route 0.0.0.0 0.0.0.0 192.168.90.1', 'end'])
        _run(r1, ['configure terminal', 'interface GigabitEthernet0/1',
                   'ip address 192.168.90.1 255.255.255.0', 'no shutdown', 'exit',
                   'ip route 192.168.91.0 255.255.255.0 192.168.90.2', 'end'])
        _run(r2, ['configure terminal', 'interface GigabitEthernet0/1',
                   'ip address 192.168.90.2 255.255.255.0', 'no shutdown', 'exit',
                   'interface GigabitEthernet0/2',
                   'ip address 192.168.91.1 255.255.255.0', 'no shutdown', 'end'])
        _run(host2, ['configure terminal', 'interface GigabitEthernet0/1',
                      'ip address 192.168.91.2 255.255.255.0', 'no shutdown', 'end'])
        return host, r1, r2, host2

    def test_redirect_counted_on_gateway_and_source(self):
        host, r1, r2, host2 = self._build_asymmetric_topology('redirtest1')
        out = _cli(host, 'ping 192.168.91.2')
        assert 'Success rate is 100 percent' in out

        r1_traffic = _cli(r1, 'show ip traffic')
        assert 'Sent:  5 redirects' in r1_traffic

        host_traffic = _cli(host, 'show ip traffic')
        assert '5 redirects, 0 unreachable\n         0 echo, 5 echo reply' in host_traffic

    def test_no_redirect_when_gateway_is_directly_on_path(self):
        """R1自身が終点まで最適経路を持つ（同一サブネットへの又貸しが
        発生しない）場合はRedirectが起きないことも確認する。"""
        prefix = 'redirtest2'
        sw, host, r1 = f'{prefix}-sw', f'{prefix}-host', f'{prefix}-r1'
        _dev(sw, type_='catalyst')
        _dev(host)
        _dev(r1)
        client.post('/api/link', json={'a': host, 'b': sw,
                                       'iface_a': 'GigabitEthernet0/1', 'iface_b': 'GigabitEthernet1/0/1'})
        client.post('/api/link', json={'a': r1, 'b': sw,
                                       'iface_a': 'GigabitEthernet0/1', 'iface_b': 'GigabitEthernet1/0/2'})
        _run(host, ['configure terminal', 'interface GigabitEthernet0/1',
                     'ip address 192.168.95.10 255.255.255.0', 'no shutdown', 'exit',
                     'ip route 0.0.0.0 0.0.0.0 192.168.95.1', 'end'])
        _run(r1, ['configure terminal', 'interface GigabitEthernet0/1',
                   'ip address 192.168.95.1 255.255.255.0', 'no shutdown', 'end'])

        _cli(host, 'ping 192.168.95.1')  # R1自身への直接到達（同一区間で又貸し無し）
        out = _cli(r1, 'show ip traffic')
        assert 'Sent:  0 redirects' in out


class TestRestconfDashboardIcmpMetrics:
    def test_dashboard_reports_icmp_and_traffic_fields(self):
        _dev('icmp-dash-1', type_='catalyst')
        _dev('icmp-dash-2', type_='catalyst')
        client.post('/api/link', json={'a': 'icmp-dash-1', 'b': 'icmp-dash-2',
                                       'iface_a': 'GigabitEthernet0/1',
                                       'iface_b': 'GigabitEthernet0/1'})
        _run('icmp-dash-1', TOPO_SETUP)
        _run('icmp-dash-2', ['configure terminal', 'interface GigabitEthernet0/1',
                             'ip address 10.20.0.2 255.255.255.0', 'no shutdown', 'end'])
        _cli('icmp-dash-1', 'ping 10.20.0.2')

        r = client.get('/api/restconf/dashboard')
        assert r.status_code == 200
        data = r.json()
        entry = next(d for d in data['devices'] if d['device_id'] == 'icmp-dash-1')
        assert 'traffic_bytes' in entry
        assert 'cpu_percent' in entry
        assert entry['icmp']['echo_sent'] == 5
        assert entry['icmp_total'] >= 5
        assert 'total_traffic_bytes' in data['summary']
        assert 'total_icmp_packets' in data['summary']
