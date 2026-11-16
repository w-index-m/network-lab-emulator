"""
ログからのフラップ検知(警告)の回帰テスト — Cisco IOS ルータ / Catalyst / Si-R

- ルールの正規表現が各機種の実際のログ形式に当たること
- 状態変化だけを数える(LINK と LINEPROTO の同じ down を二重に数えない)
- 1回の down/up では出ず、しきい値で warning のアラートになること
- CLI の shutdown/no shutdown(Cisco)と ether use off/on(Si-R)を繰り返すと実際に出ること
- 経路の追加/削除の繰り返し(route_watch)で route_flap
- /api/traffic/health の syslog 観点が「注意」になり、フラップ件数が出ること
"""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.flap_rules import match, rules_table  # noqa: E402
from engine.traffic_analytics import FlowRecorder, recorder  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


class TestRuleMatching:
    @pytest.mark.parametrize('dt,msg,rule,key,state', [
        ('cisco', '%LINK-3-UPDOWN: Interface GigabitEthernet0/0/1, changed state to down', 'if_flap', 'GigabitEthernet0/0/1', 'down'),
        ('catalyst', '%LINK-5-CHANGED: Interface Gi1/0/3, changed state to administratively down', 'if_flap', 'Gi1/0/3', 'down'),
        ('cisco', '%OSPF-5-ADJCHG: Process 1, Nbr 2.2.2.2 on Gi0/0/0 from LOADING to FULL, Loading Done', 'ospf_flap', '2.2.2.2', 'up'),
        ('cisco', '%OSPF-5-ADJCHG: Process 1, Nbr 2.2.2.2 interface down, changing state from FULL to DOWN', 'ospf_flap', '2.2.2.2', 'down'),
        ('cisco', '%BGP-5-ADJCHANGE: neighbor 10.0.0.2 Down — hold timer', 'bgp_flap', '10.0.0.2', 'down'),
        ('cisco', '%DUAL-5-NBRCHANGE: EIGRP-IPv4 100: Neighbor 10.1.1.2 (Gi0/0) is up: new adjacency', 'eigrp_flap', '10.1.1.2', 'up'),
        ('catalyst', '%HSRP-5-STATECHANGE: Vlan10 Grp 1 state Standby -> Active', 'fhrp_flap', 'HSRP Vlan10 Grp 1', 'active'),
        ('catalyst', '%SPANTREE-6-TOPOTRAP: Topology change detected on port Gi1/0/1', 'stp_tc_flap', 'topology', 'tc'),
        ('catalyst', '%SW_MATM-4-MACFLAP_NOTIF: Host 0011.2233.4455 in vlan 1 is flapping', 'mac_flap', '0011.2233.4455', 'flap'),
        ('cisco', '%PM-4-ERR_DISABLE: link-flap error detected on Gi0/1, putting Gi0/1 in err-disable state', 'errdisable_flap', 'Gi0/1', 'errdisable'),
        ('sir', '2026/11/16 03:00:00 SIR1 Si-R G120 : [LINK] lan0 Link Down', 'if_flap', 'lan0', 'down'),
        ('sir', '2026/11/16 03:00:00 SIR1 Si-R G120 : [LINK] lan0 Link Up (1Gbps full-duplex)', 'if_flap', 'lan0', 'up'),
        ('sir', 'x : [OSPF] neighbor 3.3.3.3 on lan1 (area 0.0.0.0) state: Full', 'ospf_flap', '3.3.3.3', 'up'),
        ('sir', 'x : [OSPF] neighbor 3.3.3.3 on lan1 is down (dead-timer)', 'ospf_flap', '3.3.3.3', 'down'),
        ('sir', 'x : [BGP] peer 10.9.9.9 (AS65001) is down (hold-timer expired)', 'bgp_flap', '10.9.9.9', 'down'),
        ('sir', 'x : [VRRP] VRID 1 on lan0: state changed to Master (priority=110, VIP=10.0.0.254)', 'fhrp_flap', 'VRRP lan0 VRID 1', 'master'),
    ])
    def test_vendor_formats(self, dt, msg, rule, key, state):
        r, k, s = match(dt, msg)
        assert (r['id'], k, s) == (rule, key, state)

    def test_vendor_scope(self):
        # Catalyst 専用の STP ルールは Cisco ルータには当てない
        assert match('cisco', '%SPANTREE-6-TOPOTRAP: Topology change detected') is None
        assert match('sir', '%SYS-5-CONFIG_I: Configured from console') is None

    def test_rules_table_has_all_vendors(self):
        vs = {v for r in rules_table() for v in r['vendors']}
        assert {'cisco', 'catalyst', 'sir'} <= vs


class TestDetection:
    def _rec(self, dt='cisco'):
        r = FlowRecorder()
        r.device_type_of = lambda d: dt
        return r

    def test_single_flap_is_not_a_warning(self):
        r = self._rec()
        r.record_log('r1', 'R1', 3, '%LINK-3-UPDOWN: Interface Gi0/0/1, changed state to down', now=100)
        r.record_log('r1', 'R1', 3, '%LINK-3-UPDOWN: Interface Gi0/0/1, changed state to up', now=105)
        assert not [a for a in r.alerts if a['rule'] == 'if_flap']

    def test_repeated_flap_raises_warning_once(self):
        r = self._rec()
        t = 100.0
        for st in ('down', 'up', 'down', 'up', 'down'):
            r.record_log('r1', 'R1', 3, f'%LINK-3-UPDOWN: Interface Gi0/0/1, changed state to {st}', now=t)
            r.record_log('r1', 'R1', 5, f'%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/0/1, changed state to {st}', now=t + 0.1)
            t += 5
        fl = [a for a in r.alerts if a['rule'] == 'if_flap']
        assert len(fl) == 1                                          # cooldown 内は1回
        assert fl[0]['severity'] == 'warning' and fl[0]['source'] == 'SYSLOG' and fl[0]['src'] == 'r1'
        assert fl[0]['detail'].startswith('Gi0/0/1: 60秒に 3 回')

    def test_slow_changes_outside_window_do_not_count(self):
        r = self._rec()
        for i, st in enumerate(('down', 'up', 'down')):
            r.record_log('r1', 'R1', 3, f'%LINK-3-UPDOWN: Interface Gi0/0/1, changed state to {st}', now=100 + i * 40)
        assert not [a for a in r.alerts if a['rule'] == 'if_flap']

    def test_mac_flap_is_immediate_on_catalyst(self):
        r = self._rec('catalyst')
        r.record_log('sw', 'SW', 4, '%SW_MATM-4-MACFLAP_NOTIF: Host 0011.2233.4455 in vlan 1 is flapping '
                     'between port Gi1/0/1 and port Gi1/0/2', now=100)
        assert [a['rule'] for a in r.alerts] == ['mac_flap']

    def test_route_flap(self):
        r = self._rec()
        for i, k in enumerate(('add', 'del', 'add', 'del')):
            r.record_route_event('r1', '10.9.0.0/16', k, now=100 + i)
        a = [x for x in r.alerts if x['rule'] == 'route_flap']
        assert len(a) == 1 and a[0]['source'] == 'NET' and a[0]['severity'] == 'warning'


def _cli(c, dev, *cmds):
    for cmd in cmds:
        c.post('/api/cli', json={'device_id': dev, 'command': cmd})


def test_cisco_shutdown_flap_via_cli(client):
    dev = f'fl-c-{_RUN}'
    client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': 'FL-C'})
    recorder.clear()
    for _ in range(2):
        _cli(client, dev, 'configure terminal', 'interface GigabitEthernet0/0/1', 'shutdown', 'no shutdown', 'end')
    a = [x for x in recorder.alerts if x['rule'] == 'if_flap' and x['src'] == dev]
    assert a and 'GigabitEthernet0/0/1' in a[0]['detail']
    h = {c['key']: c for c in client.get('/api/traffic/health').json()['categories']}
    assert h['syslog']['state'] in ('warn', 'crit') and 'フラップ' in h['syslog']['summary']
    assert any('インタフェースのフラップ' in d for d in h['syslog']['details'])
    assert h['security']['state'] == 'ok' or all('フラップ' not in d for d in h['security']['details'])


def test_sir_ether_use_flap_via_cli(client):
    dev = f'fl-s-{_RUN}'
    client.post('/api/device', json={'id': dev, 'type': 'sir', 'hostname': 'FL-S'})
    recorder.clear()
    for _ in range(2):
        _cli(client, dev, 'configure', 'ether 1 1 use off', 'ether 1 1 use on', 'exit')
    a = [x for x in recorder.alerts if x['rule'] == 'if_flap' and x['src'] == dev]
    assert a, [x['msg'] for x in recorder.logs if x['dev'] == dev]
    assert a[0]['detail'].startswith('lan')


def test_flap_rules_api(client):
    ids = {r['id'] for r in client.get('/api/traffic/flap-rules').json()['rules']}
    assert {'if_flap', 'ospf_flap', 'bgp_flap', 'mac_flap', 'stp_tc_flap', 'route_flap'} <= ids
