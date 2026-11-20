"""
gNMI: OpenConfig の interfaces/interface/state(oper-status・counters)

- カウンタは SNMP の IF-MIB(ifHCIn/OutOctets)と同じデータプレーンのカウンタから取る(値が一致する)
- uint64 は RFC 7951 どおり文字列
- shutdown なら admin/oper とも DOWN、リンクがあれば oper UP
- IF 名を指定しない /interfaces/interface/state/counters は IF ごとに interface[name=...] を付けて分ける
- 本物の gRPC で Get と STREAM(SAMPLE 1秒)。ping でカウンタが増えるのが流れてくる
"""
import os
import sys
import time
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
grpc = pytest.importorskip('grpc')
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from engine import gnmi_agent  # noqa: E402
from engine.gnmi_agent import expand_paths, get_value  # noqa: E402
from engine.protocols import snmp_agent  # noqa: E402

gnmi_pb2, gnmi_pb2_grpc = gnmi_agent.gnmi_pb2, gnmi_agent.gnmi_pb2_grpc
GI4 = 'GigabitEthernet1/0/4'
HC_IN = '1.3.6.1.2.1.31.1.1.1.6.'
IFNAME = '1.3.6.1.2.1.31.1.1.1.1.'


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def _lab(c, net):
    tag = uuid.uuid4().hex[:5]
    sw, pc = f'gc-sw-{tag}', f'gc-pc-{tag}'
    c.post('/api/device', json={'id': sw, 'type': 'catalyst', 'hostname': f'GSW-{tag}'})
    c.post('/api/device', json={'id': pc, 'type': 'pc', 'hostname': f'GPC-{tag}'})
    c.post('/api/link', json={'a': pc, 'b': sw, 'iface_a': 'eth0', 'iface_b': GI4})
    _cli(c, sw, 'configure terminal', 'vlan 263', 'exit',
         'interface Vlan263', f'ip address 10.{net}.0.254 255.255.255.0', 'no shutdown', 'exit',
         f'interface {GI4}', 'switchport mode access', 'switchport access vlan 263', 'no shutdown', 'end')
    _cli(c, pc, f'ifconfig eth0 10.{net}.0.10 netmask 255.255.255.0')
    return sw, pc


def P(*elems):
    p = gnmi_pb2.Path()
    for e in elems:
        name, _, rest = e.partition('[')
        pe = p.elem.add()
        pe.name = name
        if rest:
            k, v = rest.rstrip(']').split('=', 1)
            pe.key[k] = v
    return p


def _elems(*e):
    return gnmi_agent.path_elems(P(*e))


def test_counters_follow_pings_and_match_snmp(client):
    sw, pc = _lab(client, 71)
    st = app_module.device_sessions[sw]
    path = _elems('interfaces', f'interface[name={GI4}]', 'state', 'counters')
    assert get_value(st, path, sw)['in-octets'] == '0'
    _cli(client, pc, 'ping -c 20 -s 1400 10.71.0.254')
    c = get_value(st, path, sw)
    assert c['in-octets'] == c['out-octets'] == str(20 * 1428)       # 文字列(RFC 7951 の uint64)
    assert c['in-unicast-pkts'] == '20' and c['in-errors'] == '0'
    mib = {o: v for o, _t, v in snmp_agent._build_mib(sw)}
    idx = next(o[len(IFNAME):] for o, v in mib.items() if o.startswith(IFNAME) and v == 'Gi1/0/4')
    assert mib[HC_IN + idx] == c['in-octets']                        # SNMP と同じ値


def test_oper_and_admin_status(client):
    sw, _pc = _lab(client, 72)
    st = app_module.device_sessions[sw]
    s = get_value(st, _elems('interfaces', f'interface[name={GI4}]', 'state'), sw)
    assert (s['admin-status'], s['oper-status'], s['enabled']) == ('UP', 'UP', True)
    s5 = get_value(st, _elems('interfaces', 'interface[name=GigabitEthernet1/0/5]', 'state'), sw)
    assert s5['oper-status'] == 'DOWN'                               # ケーブル無し
    _cli(client, sw, 'configure terminal', f'interface {GI4}', 'shutdown', 'end')
    s = get_value(st, _elems('interfaces', f'interface[name={GI4}]', 'state'), sw)
    assert (s['admin-status'], s['oper-status']) == ('DOWN', 'DOWN')
    assert get_value(st, _elems('interfaces', f'interface[name={GI4}]', 'state', 'oper-status'), sw) == 'DOWN'


def test_wildcard_is_split_per_interface(client):
    sw, pc = _lab(client, 73)
    st = app_module.device_sessions[sw]
    _cli(client, pc, 'ping -c 3 10.73.0.254')
    found = expand_paths(st, _elems('interfaces', 'interface', 'state', 'counters', 'in-octets'), sw)
    by_if = {e[1][1]['name']: v for e, v in found}
    assert by_if[GI4] == str(3 * 84) and by_if['GigabitEthernet1/0/5'] == '0'
    # 名前付きのモジュール接頭辞でも同じ
    assert get_value(st, _elems('openconfig-interfaces:interfaces', f'interface[name={GI4}]',
                                'state', 'counters', 'in-octets'), sw) == str(3 * 84)


def test_ietf_view_without_state_is_unchanged(client):
    """既存の ietf-interfaces の形(state を付けない)は変えない"""
    sw, _pc = _lab(client, 74)
    v = get_value(app_module.device_sessions[sw], _elems('ietf-interfaces:interfaces', f'interface[name={GI4}]'), sw)
    assert 'state' not in v and v['name'] == GI4


def test_real_grpc_get_and_one_second_stream(client):
    sw, pc = _lab(client, 75)
    st = app_module.device_sessions[sw]
    srv = gnmi_agent.GnmiServer(sw, '127.0.75.1', st, port=50875)
    srv.start()
    ch = grpc.insecure_channel('127.0.75.1:50875')
    try:
        stub = gnmi_pb2_grpc.gNMIStub(ch)
        r = stub.Get(gnmi_pb2.GetRequest(path=[P('interfaces', 'interface', 'state', 'counters', 'in-octets')],
                                         encoding=gnmi_pb2.JSON_IETF), timeout=5)
        names = [u.path.elem[1].key['name'] for u in r.notification[0].update]
        assert GI4 in names and len(names) == len(st.interfaces)

        sub = gnmi_pb2.SubscriptionList(mode=gnmi_pb2.SubscriptionList.STREAM, encoding=gnmi_pb2.JSON_IETF)
        s = sub.subscription.add()
        s.path.CopyFrom(P('interfaces', f'interface[name={GI4}]', 'state', 'counters', 'in-octets'))
        s.mode = gnmi_pb2.SubscriptionMode.SAMPLE
        s.sample_interval = 1_000_000_000
        stream = stub.Subscribe(iter([gnmi_pb2.SubscribeRequest(subscribe=sub)]), timeout=8)
        vals, pinged, t0 = [], False, time.time()
        for resp in stream:
            if resp.HasField('update'):
                vals.append(int(resp.update.update[0].val.json_ietf_val.decode().strip('"')))
                if not pinged and len(vals) >= 2:
                    _cli(client, pc, 'ping -c 5 -s 1000 10.75.0.254')
                    pinged = True
            if len(vals) >= 4 or time.time() - t0 > 7:
                break
        stream.cancel()
        assert len(vals) >= 4, vals                                   # 1秒ごとに届く
        assert vals[0] == 0 and vals[-1] == 5 * 1028, vals            # ping 分だけ増える
    finally:
        ch.close()
        srv.stop()
