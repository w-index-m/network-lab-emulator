"""
エミュレータの再現度: L2 ポートの IF-MIB、PC の ping のカウンタ、PC の ifconfig

サンドボックスで単体版ニルヴァーナをモック Catalyst につないだ検証で見つかった3件。
- L2 のアクセスポート(switchport・IP 無し)が IF-MIB に出ず、ポートの流量・状態を SNMP で読めなかった
  → IP のあるインタフェースの後ろに足す(既存の ifIndex は変えない)。oper はリンクがあれば up、shutdown は down
- PC の ping(-c / -s 付き)ではカウンタが動かず、-s も受け付けなかった。オプション無しは IOS 形式の表示だった
- PC の「ifconfig eth0 <ip> netmask <mask>」が表示だけで、IP が設定されなかった
"""
import os
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import app as app_module  # noqa: E402
from engine.protocols import icmp_engine, snmp_agent  # noqa: E402

_RUN = uuid.uuid4().hex[:6]
IFNAME, ADMIN, OPER = '1.3.6.1.2.1.31.1.1.1.1.', '1.3.6.1.2.1.2.2.1.7.', '1.3.6.1.2.1.2.2.1.8.'
HC_IN, HC_OUT = '1.3.6.1.2.1.31.1.1.1.6.', '1.3.6.1.2.1.31.1.1.1.10.'


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def _lab(c, net):
    sw, pc = f'l2sw-{_RUN}-{net}', f'l2pc-{_RUN}-{net}'
    c.post('/api/device', json={'id': sw, 'type': 'catalyst', 'hostname': 'L2SW'})
    c.post('/api/device', json={'id': pc, 'type': 'pc', 'hostname': 'L2PC'})
    c.post('/api/link', json={'a': pc, 'b': sw, 'iface_a': 'eth0', 'iface_b': 'GigabitEthernet1/0/4'})
    _cli(c, sw, 'configure terminal', 'vlan 263', 'exit', 'interface Vlan263', f'ip address 10.{net}.0.254 255.255.255.0',
         'no shutdown', 'exit', 'interface GigabitEthernet1/0/4', 'switchport mode access', 'switchport access vlan 263',
         'no shutdown', 'exit', 'snmp-server community ro RO', 'end')
    assert _cli(c, pc, f'ifconfig eth0 10.{net}.0.10 netmask 255.255.255.0') == ''
    return sw, pc


def _mib(dev):
    m = {o: v for o, _t, v in snmp_agent._build_mib(dev)}
    idx = {v: o[len(IFNAME):] for o, v in m.items() if o.startswith(IFNAME)}
    return m, idx


def test_pc_ifconfig_sets_and_registers_the_address(client):
    _sw, pc = _lab(client, 61)
    assert app_module.device_sessions[pc].interfaces['eth0']['ip'] == '10.61.0.10'
    assert icmp_engine.device_ips[pc]['interfaces']['eth0'] == {'ip': '10.61.0.10', 'prefix': 24}
    assert 'No such device' in _cli(client, pc, 'ifconfig eth9 10.0.0.1')


def test_l2_access_ports_are_in_ifmib_after_l3_interfaces(client):
    sw, _pc = _lab(client, 62)
    m, idx = _mib(sw)
    names = list(idx)
    assert names.index('Vl263') < names.index('Gi1/0/4')          # IP のあるものが先(既存の ifIndex は変わらない)
    assert int(m['1.3.6.1.2.1.2.1.0']) == len(names)              # ifNumber
    i4, i5 = idx['Gi1/0/4'], idx['Gi1/0/5']
    assert (m[ADMIN + i4], m[OPER + i4]) in (('1', '1'), (1, 1))   # リンクあり・有効 → up
    assert str(m[OPER + i5]) == '2'                                # ケーブル無し(既定 shutdown)→ down
    _cli(client, sw, 'configure terminal', 'interface GigabitEthernet1/0/4', 'shutdown', 'end')
    m, _ = _mib(sw)
    assert (str(m[ADMIN + i4]), str(m[OPER + i4])) == ('2', '2')


def test_pc_ping_moves_the_access_port_counters_and_prints_linux_format(client):
    sw, pc = _lab(client, 63)
    m, idx = _mib(sw)
    i = idx['Gi1/0/4']
    before = (int(m[HC_IN + i]), int(m[HC_OUT + i]))
    out = _cli(client, pc, 'ping -c 20 -s 1400 10.63.0.254')
    assert out.splitlines()[0] == 'PING 10.63.0.254 (10.63.0.254) 1400(1428) bytes of data.'
    assert '1408 bytes from 10.63.0.254' in out and '20 received' in out
    m, _ = _mib(sw)
    assert (int(m[HC_IN + i]) - before[0], int(m[HC_OUT + i]) - before[1]) == (20 * 1428, 20 * 1428)
    bare = _cli(client, pc, 'ping 10.63.0.254')
    assert bare.splitlines()[0] == 'PING 10.63.0.254 (10.63.0.254) 56(84) bytes of data.'   # IOS 形式ではない
    assert 'Usage' in _cli(client, pc, 'ping -c x 10.63.0.254')
