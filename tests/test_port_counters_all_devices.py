"""
各機器のポートの SNMP カウンタ(IF-MIB の ifTable / ifXTable)

- 送受信の内訳: ping はユニキャスト、STP BPDU・OSPF Hello・CDP・LLDP はマルチキャスト、
  ブロードキャストストームはブロードキャスト。storm-control で捨てた分は ifInDiscards
- 制御プレーンの背景トラフィック(engine/control_traffic.py)は実機の既定の間隔で積まれる。
  shutdown したポートには積まない
- ifMtu / ifSpeed / ifHighSpeed / ifType は IF の種類どおり、ifLastChange は up/down で更新
- CLI の show interfaces / show interfaces counters / counters errors と SNMP が同じ値
- cisco / catalyst / nexus / arista / juniper / yamaha / fortigate / sir で、全ポートが IF-MIB に出る
"""
import os
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as app_module  # noqa: E402
from engine import control_traffic as CT  # noqa: E402
from engine.protocols import dp_engine, ospf_engine, snmp_agent, stp_engine, vnet  # noqa: E402

IF = '1.3.6.1.2.1.2.2.1.'
XT = '1.3.6.1.2.1.31.1.1.1.'
GI4 = 'GigabitEthernet1/0/4'


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def _dev(c, kind, name=None):
    d = f'pc-{kind}-{uuid.uuid4().hex[:5]}'
    c.post('/api/device', json={'id': d, 'type': kind, 'hostname': name or d.upper()})
    _cli(c, d, 'show version')           # 1回コマンドを通すと SNMP に登録される
    return d


def _mib(dev):
    m = {o: v for o, _t, v in snmp_agent._build_mib(dev)}
    idx = {v: o[len(XT) + 2:] for o, v in m.items() if o.startswith(XT + '1.')}
    return m, idx


def _col(m, base, col, i):
    return int(m[f'{base}{col}.{i}'])


def _sw_pc(c, net):
    sw = _dev(c, 'catalyst')
    pc = _dev(c, 'pc')
    c.post('/api/link', json={'a': pc, 'b': sw, 'iface_a': 'eth0', 'iface_b': GI4})
    _cli(c, sw, 'configure terminal', 'vlan 263', 'exit',
         'interface Vlan263', f'ip address 10.{net}.0.254 255.255.255.0', 'no shutdown', 'exit',
         f'interface {GI4}', 'switchport mode access', 'switchport access vlan 263', 'no shutdown', 'end')
    _cli(c, pc, f'ifconfig eth0 10.{net}.0.10 netmask 255.255.255.0')
    return sw, pc


# ── 内訳と CLI の一致 ────────────────────────────────────
def test_unicast_multicast_broadcast_discards_and_cli_agree(client):
    sw, pc = _sw_pc(client, 81)
    _cli(client, pc, 'ping -c 10 -s 1000 10.81.0.254')                     # ユニキャスト 10
    dp_engine.bump(sw, GI4, 'in', pkts=7, size=64, kind='mcast')         # BPDU 等
    dp_engine.bump(sw, GI4, 'in', pkts=3, size=64, kind='bcast')         # ARP 等
    dp_engine.drop(sw, GI4, 'in', pkts=5)                                # storm-control
    dp_engine.drop(sw, GI4, 'in', pkts=2, reason='crc')
    m, idx = _mib(sw)
    i = idx['Gi1/0/4']
    assert _col(m, IF, 11, i) == 10 and _col(m, XT, 7, i) == 10          # ifInUcastPkts / ifHCInUcastPkts
    assert _col(m, XT, 8, i) == 7 and _col(m, XT, 9, i) == 3             # ifHCInMulticast / Broadcast
    assert _col(m, XT, 2, i) == 7 and _col(m, XT, 3, i) == 3             # ifInMulticast / Broadcast(32bit)
    assert _col(m, IF, 12, i) == 10                                      # ifInNUcastPkts = mcast + bcast
    assert _col(m, IF, 13, i) == 5 and _col(m, IF, 14, i) == 2           # ifInDiscards / ifInErrors
    assert _col(m, XT, 6, i) == 10 * 1028 + 10 * 64                      # 捨てた分は octets に入れない

    counters = _cli(client, sw, 'show interfaces counters')
    row = next(ln for ln in counters.splitlines() if ln.startswith('Gi1/0/4'))
    assert row.split()[1:] == [str(10 * 1028 + 10 * 64), '10', '7', '3']
    errs = _cli(client, sw, 'show interfaces counters errors')
    assert next(ln for ln in errs.splitlines() if ln.startswith('Gi1/0/4')).split()[2] == '2'   # FCS-Err
    sh = _cli(client, sw, f'show interfaces {GI4}')
    assert '20 packets input' in sh and 'Received 10 broadcasts (7 multicasts)' in sh
    assert '2 input errors, 2 CRC' in sh and 'Input queue: 0/2000/5/0' in sh


def test_storm_control_level_counts_discards(client):
    sw = _dev(client, 'catalyst')
    app_module.asyncio.run(app_module._l2loop_apply(
        {'do': 'storm', 'dev': sw, 'iface': GI4, 'pps': 400, 'dropped': 600}, snmp_agent))
    m, idx = _mib(sw)
    i = idx['Gi1/0/4']
    assert _col(m, XT, 9, i) == 400 and _col(m, IF, 13, i) == 600        # 通した分はブロードキャスト、捨てた分は discards


# ── 制御プレーンの背景トラフィック ─────────────────────────
def test_control_traffic_bpdu_cdp_lldp_ospf_rates(client):
    a = _dev(client, 'catalyst')
    b = _dev(client, 'arista')
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet1/0/1', 'iface_b': 'Ethernet1'})
    stp_engine.nodes.setdefault(a, {'enabled': True, 'ports': {}})
    saved_stp = dict(stp_engine.nodes[a])
    stp_engine.nodes[a].update({'enabled': True, 'ports': {'Gi1/0/1': {'connected_to': b, 'role': 'DESIGNATED'}}})
    saved_ospf = ospf_engine.nodes.get(b)
    ospf_engine.nodes[b] = {'enabled': True, 'neighbors': {a: {}}, 'hello_interval': 10}
    try:
        ct = CT.ControlTraffic()
        ct.tick(app_module.device_sessions, vnet, dp_engine, stp_engine, ospf_engine, now=1000.0)
        before_a = dp_engine.get_counter(a, 'GigabitEthernet1/0/1')
        before_b = dp_engine.get_counter(b, 'Ethernet1')
        for k in range(1, 61):                                            # 60 秒ぶん、1 秒ごと
            ct.tick(app_module.device_sessions, vnet, dp_engine, stp_engine, ospf_engine, now=1000.0 + k)
        ca = dp_engine.get_counter(a, 'GigabitEthernet1/0/1')
        cb = dp_engine.get_counter(b, 'Ethernet1')
        # a(Catalyst)→b: BPDU 30 + CDP 1。b(Arista)→a: LLDP 2 + OSPF Hello 6
        assert ca['out_mcast'] - before_a['out_mcast'] == 31
        assert cb['in_mcast'] - before_b['in_mcast'] == 31
        assert cb['out_mcast'] - before_b['out_mcast'] == 8
        assert ca['in_mcast'] - before_a['in_mcast'] == 8
        assert ca['out_bytes'] - before_a['out_bytes'] == 30 * 64 + 400
        assert ca['out_ucast'] == before_a['out_ucast']                   # ユニキャストは増えない
    finally:
        stp_engine.nodes[a] = saved_stp
        if saved_ospf is None:
            ospf_engine.nodes.pop(b, None)
        else:
            ospf_engine.nodes[b] = saved_ospf
    # shutdown したら積まない(CDP / LLDP は STP・OSPF が無くても流れる)
    ct.tick(app_module.device_sessions, vnet, dp_engine, stp_engine, ospf_engine, now=1100.0)
    _cli(client, a, 'configure terminal', 'interface GigabitEthernet1/0/1', 'shutdown', 'end')
    frozen = (dp_engine.get_counter(a, 'GigabitEthernet1/0/1')['out_mcast'],
              dp_engine.get_counter(b, 'Ethernet1')['out_mcast'])
    for k in range(1, 181):
        ct.tick(app_module.device_sessions, vnet, dp_engine, stp_engine, ospf_engine, now=1100.0 + k)
    assert (dp_engine.get_counter(a, 'GigabitEthernet1/0/1')['out_mcast'],
            dp_engine.get_counter(b, 'Ethernet1')['out_mcast']) == frozen


def test_control_traffic_is_off_in_tests_but_on_by_default():
    assert os.environ.get('NETLAB_CTRL_TRAFFIC') == '0'                  # conftest で止めている
    src = open(os.path.join(os.path.dirname(app_module.__file__), 'app.py'), encoding='utf-8').read()
    assert "os.environ.get('NETLAB_CTRL_TRAFFIC', '1') != '0'" in src


# ── ifMtu / ifSpeed / ifType / ifLastChange ───────────────────
def test_speed_mtu_type_and_last_change(client):
    sw, _pc = _sw_pc(client, 82)
    m, idx = _mib(sw)
    gi, vl = idx['Gi1/0/4'], idx['Vl263']
    assert _col(m, IF, 4, gi) == 1500 and _col(m, IF, 5, gi) == 1_000_000_000 and _col(m, XT, 15, gi) == 1000
    assert m[f'{IF}3.{gi}'] == '6' and m[f'{IF}3.{vl}'] == '53'          # ethernetCsmacd / propVirtual(SVI)
    assert m[f'{IF}6.{gi}'].count(':') == 5                               # ifPhysAddress
    lc0 = _col(m, IF, 9, gi)
    time.sleep(0.05)
    _cli(client, sw, 'configure terminal', f'interface {GI4}', 'shutdown', 'end')
    m, _ = _mib(sw)
    lc1 = _col(m, IF, 9, gi)
    assert lc1 > 0 and lc1 >= lc0 and lc1 <= int(m['1.3.6.1.2.1.1.3.0'])   # sysUpTime 以下


# ── 各機器 ───────────────────────────────────────────────
@pytest.mark.parametrize('kind,port,speed_mbps', [
    ('cisco', 'Gi0/0/0', 1000),
    ('catalyst', 'Gi1/0/1', 1000),
    ('nexus', 'Ethernet1/1', 10000),
    ('arista', 'Ethernet1', 1000),
    ('juniper', 'ge-0/0/0', 1000),
    ('yamaha', 'lan1', 1000),
    ('fortigate', 'port1', 1000),
])
def test_every_device_type_lists_all_ports_with_counters(client, kind, port, speed_mbps):
    d = _dev(client, kind)
    m, idx = _mib(d)
    st = app_module.device_sessions[d]
    assert port in idx, sorted(idx)
    assert int(m['1.3.6.1.2.1.2.1.0']) == len(idx) >= len(st.interfaces)   # ifNumber: 全ポート
    i = idx[port]
    assert _col(m, XT, 15, i) == speed_mbps
    full = next(n for n in st.interfaces if snmp_agent_short(n) == port)
    dp_engine.bump(d, full, 'out', pkts=4, size=100)
    m, _ = _mib(d)
    assert _col(m, XT, 10, i) == 400 and _col(m, XT, 11, i) == 4         # ifHCOutOctets / ifHCOutUcastPkts
    for col in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):                 # ifXTable のカウンタ列がそろっている
        assert f'{XT}{col}.{i}' in m
    for col in (10, 11, 12, 13, 14, 16, 17, 18, 19, 20):                 # ifTable のカウンタ列
        assert f'{IF}{col}.{i}' in m


def snmp_agent_short(name):
    from engine.protocols import _snmp_if_name
    return _snmp_if_name(name)
