"""
経路の MIB(IP-FORWARD-MIB)と CPU の「情」「注」

- エミュレータの SNMP に inetCidrRouteTable / ipCidrRouteTable / 経路数が載ること
- スタンドアロン版の小さな SNMP クライアントが、エミュレータの本物の SNMP(UDP)から
  経路表を walk して経路に戻せること(SnmpPoller で差分 → 情)
- CPU: 急上昇 → 情 cpu_up、高い状態が続く → 注(cpu_high)、戻る → 情 cpu_ok
- ラボ版: 大きな ping で経路上の装置の CPU が上がり、情・注が出ること
- engine/route_watch.py とスタンドアロン版のコピーがずれていないこと
"""
import asyncio
import filecmp
import os
import sys
import threading
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SA = os.path.join(ROOT, 'cyber-view-standalone')
sys.path.insert(0, ROOT)
sys.path.insert(0, SA)

import app as app_module  # noqa: E402
from engine.protocols import snmp_agent  # noqa: E402
from engine.route_watch import CpuWatch, RouteWatcher, describe, watcher  # noqa: E402
from engine.snmp_udp_agent import SnmpDeviceProtocol  # noqa: E402
from cvs import snmp_client as SC  # noqa: E402
from cvs import snmp_poll as SP  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def test_route_watch_copy_is_in_sync():
    assert filecmp.cmp(os.path.join(ROOT, 'engine', 'route_watch.py'),
                       os.path.join(SA, 'cvs', 'route_watch.py'), shallow=False)


class TestCpuWatch:
    def test_jump_high_and_recovery(self):
        w = RouteWatcher()
        fired = []
        cw = CpuWatch(w, lambda *a: fired.append(a), high=80, jump=20, hold=2)
        assert cw.observe('d', 'D', 20, now=1) == []
        assert cw.observe('d', 'D', 35, now=2) == []                 # +15 は情にしない
        ev = cw.observe('d', 'D', 85, now=3)
        assert [(e['kind'], e['prefix']) for e in ev] == [('cpu_up', '85% (+50)')]
        assert describe(ev[0]) == 'CPU 85% (+50)'
        assert fired == []                                            # 1回目はまだ注にしない
        cw.observe('d', 'D', 90, now=4)
        assert len(fired) == 1 and '2回連続で 80% 以上' in fired[0][3]
        cw.observe('d', 'D', 95, now=5)
        assert len(fired) == 1                                        # 高い間は1回だけ
        ev = cw.observe('d', 'D', 40, now=6)
        assert [e['kind'] for e in ev] == ['cpu_ok']
        assert w.seq == 2 and [e['code'] for e in w.events] == ['CPU', 'CPU']

    def test_none_is_ignored(self):
        cw = CpuWatch(RouteWatcher(), lambda *a: None)
        assert cw.observe('d', 'D', None) == []


class TestIndexParse:
    def test_inet_index(self):
        assert SP.parse_inet_index('1.4.10.240.2.0.24.2.0.0.1.4.10.240.12.2') == ('10.240.2.0', 24, '10.240.12.2')
        assert SP.parse_inet_index('2.16.' + '.'.join(['0'] * 16) + '.0.2.0.0.2.16.' + '.'.join(['0'] * 16)) is None

    def test_old_index(self):
        assert SP.parse_old_index('10.240.2.0.255.255.255.0.0.10.240.12.2') == ('10.240.2.0', 24, '10.240.12.2')
        assert SP.parse_old_index('1.2.3') is None


def _lab(c, a, b, net):
    for dev, host in ((a, 'RM-A'), (b, 'RM-B')):
        c.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': host})
    c.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    _cli(c, a, 'configure terminal', 'interface GigabitEthernet0/0/0', f'ip address {net}.1 255.255.255.252',
         'no shutdown', 'exit', 'snmp-server community rt-ro RO', 'end')
    _cli(c, b, 'configure terminal', 'interface GigabitEthernet0/0/0', f'ip address {net}.2 255.255.255.252',
         'no shutdown', 'exit', 'snmp-server community rt-ro RO', 'end')


def test_emulator_mib_has_route_tables(client):
    a, b = f'rm-a-{_RUN}', f'rm-b-{_RUN}'
    _lab(client, a, b, '10.247.12')
    _cli(client, a, 'configure terminal', 'ip route 172.31.250.0 255.255.255.0 10.247.12.2', 'end')
    mib = {o: v for o, _t, v in snmp_agent._build_mib(a)}
    assert mib['1.3.6.1.2.1.4.24.7.1.9.1.4.172.31.250.0.24.2.0.0.1.4.10.247.12.2'] in (3, '3')   # netmgmt
    assert mib['1.3.6.1.2.1.4.24.4.1.7.172.31.250.0.255.255.255.0.0.10.247.12.2'] in (3, '3')
    assert int(mib['1.3.6.1.2.1.4.24.6.0']) == int(mib['1.3.6.1.2.1.4.24.3.0']) >= 2
    assert '1.3.6.1.4.1.9.9.109.1.1.1.1.8.1' in mib                                         # cpmCPUTotal5minRev


def test_standalone_poller_reads_routes_over_real_snmp(client):
    """エミュレータの本物の SNMP(UDP)にスタンドアロン版のクライアントで問い合わせ、経路の差分が情になる"""
    a, b = f'rp-a-{_RUN}', f'rp-b-{_RUN}'
    _lab(client, a, b, '10.247.13')
    loop = asyncio.new_event_loop()
    tr, _p = loop.run_until_complete(loop.create_datagram_endpoint(
        lambda: SnmpDeviceProtocol(a, snmp_agent), local_addr=('127.0.0.1', 0)))
    port = tr.get_extra_info('sockname')[1]
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    try:
        assert SC.get('127.0.0.1', 'rt-ro', [SP.ROUTE_NUM[0]], port=port)[SP.ROUTE_NUM[0]] >= 1
        with pytest.raises(SC.SnmpError):
            SC.get('127.0.0.1', 'wrong', [SP.ROUTE_NUM[0]], port=port, timeout=0.3, retries=0)

        class _An:
            fired = []

            def fire(self, *a, **k):
                self.fired.append(a)
        cfg = {'devices': [{'id': a, 'hostname': 'RP-A', 'mgmt_ip': '127.0.0.1'}],
               'sources': {'snmp': {'community': 'rt-ro', 'port': port, 'timeout': 1}}}
        poller = SP.SnmpPoller(cfg, _An(), {})
        d = cfg['devices'][0]
        routes = poller.routes(d, 0)
        conn = [r for r in routes if r['network'] == '10.247.13.0']
        assert conn and conn[0]['source'] == 'connected' and conn[0]['prefix'] == 30
        assert conn[0]['iface'] in ('Gi0/0/0', 'GigabitEthernet0/0/0')
        assert poller.tick(now=1) == []                                  # 基準
        _cli(client, a, 'configure terminal', 'ip route 172.31.251.0 255.255.255.0 10.247.13.2', 'end')
        ev = [e for e in poller.tick(now=2) if e['code'] != 'CPU']
        assert [(e['kind'], e['prefix'], e['code'], e['next_hop']) for e in ev] == \
            [('add', '172.31.251.0/24', 'S', '10.247.13.2')]
        _cli(client, a, 'configure terminal', 'no ip route 172.31.251.0 255.255.255.0 10.247.13.2', 'end')
        ev = [e for e in poller.tick(now=3) if e['code'] != 'CPU']
        assert [(e['kind'], e['prefix']) for e in ev] == [('del', '172.31.251.0/24')]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)
        tr.close()


def test_lab_big_ping_raises_cpu_info_and_notice(client, monkeypatch):
    a, b = f'rc-a-{_RUN}', f'rc-b-{_RUN}'
    _lab(client, a, b, '10.247.14')
    app_module._route_watch_tick()                                       # 基準
    monkeypatch.setitem(snmp_agent._cpu_state, a, 20.0)
    seq = watcher.seq
    out = _cli(client, a, 'ping 10.247.14.2 repeat 1000 size 1500')
    assert '100 percent' in out
    assert snmp_agent._cpu_state[a] >= 75
    app_module._route_watch_tick()
    evs = [e for e in watcher.since(seq) if e['dev'] == a]
    assert any(e['kind'] == 'cpu_up' for e in evs)
    r = client.get(f'/api/traffic/routes?since={seq}').json()
    assert any(e['text'].startswith('CPU ') for e in r['events'] if e['dev'] == a)
    # 2回連続で 80% 以上 → 注(cpu_high、警告)
    snmp_agent._cpu_state[a] = 95.0
    app_module._route_watch_tick()
    app_module._route_watch_tick()
    al = [x for x in client.get('/api/traffic/alerts').json()['alerts'] if x['rule'] == 'cpu_high' and x['src'] == a]
    assert al and al[-1]['severity'] == 'warning'
    snmp_agent._cpu_state[a] = 20.0
    app_module._route_watch_tick()
    assert any(e['kind'] == 'cpu_ok' for e in watcher.since(seq) if e['dev'] == a)


def test_normal_ping_does_not_raise_cpu(client):
    a, b = f'rn-a-{_RUN}', f'rn-b-{_RUN}'
    _lab(client, a, b, '10.247.15')
    snmp_agent._cpu_percent(a, 'cisco')
    before = snmp_agent._cpu_state[a]
    _cli(client, a, 'ping 10.247.15.2')
    assert snmp_agent._cpu_state[a] == before
