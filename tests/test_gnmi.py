"""
gNMI（gRPC）のテスト

engine/gnmi_agent.py のデータモデル・パス解決・RPC実装を検証する。
protoは openconfig/gnmi の gnmi.proto 原本を grpc_tools.protoc で
コンパイルしたもの（engine/gnmi_proto/）を使う。

実クライアント(grpc)との相互接続は、装置IPへのbindとポート占有が必要で
CI環境では不安定なため、ここではサービス実装を直接呼ぶ。
実際のgRPC越しの Capabilities / Get / Set / Subscribe(ONCE/POLL/STREAM)
の実行結果は docs/gnmi-telemetry.md に記録している。
"""

import json
import os
import sys

import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..',
                                'engine', 'gnmi_proto'))

from engine.gnmi_agent import (                      # noqa: E402
    _HAS_GRPC, GNMI_VERSION, format_show_gnxi_state, get_value, path_elems,
    path_to_str, set_value,
)
from engine.rules import DeviceState                 # noqa: E402

pytestmark = pytest.mark.skipif(not _HAS_GRPC,
                                reason='grpcio が利用できない環境')

if _HAS_GRPC:
    import gnmi_pb2
    from engine.gnmi_agent import GnmiServicer


def _dev():
    st = DeviceState('catalyst', 'GN1')
    st.interfaces = {
        'GigabitEthernet1/0/1': {'ip': '10.77.0.1', 'prefix': 24,
                                 'status': 'up', 'desc': 'uplink-to-core'},
        'GigabitEthernet1/0/2': {'ip': '', 'prefix': 0, 'status': 'down'},
    }
    return st


def P(*segs):
    p = gnmi_pb2.Path()
    for s in segs:
        e = p.elem.add()
        if '[' in s:
            name, k = s.split('[', 1)
            e.name = name
            kk, vv = k.rstrip(']').split('=', 1)
            e.key[kk] = vv
        else:
            e.name = s
    return p


class _Ctx:
    """grpc.ServicerContext の最低限のスタブ"""
    def __init__(self):
        self.code = None
        self.details = None

    def abort(self, code, details):
        self.code, self.details = code, details
        raise RuntimeError(details)

    def is_active(self):
        return True


# ── パス解決 ───────────────────────────────────────────
def test_path_elems_keeps_interface_name_with_slashes():
    """インタフェース名のスラッシュでパスが壊れないこと

    パスを文字列に潰して "/" で分割すると GigabitEthernet1/0/1 が
    バラバラになり、経路が解決できなくなる（実際に発生した）。
    """
    elems = path_elems(P('ietf-interfaces:interfaces',
                         'interface[name=GigabitEthernet1/0/1]'))
    assert elems[1][0] == 'interface'
    assert elems[1][1]['name'] == 'GigabitEthernet1/0/1'


def test_path_to_str_is_only_for_display():
    s = path_to_str(P('Cisco-IOS-XE-native:native', 'hostname'))
    assert s == 'Cisco-IOS-XE-native:native/hostname'


# ── Get ────────────────────────────────────────────────
def test_get_hostname():
    st = _dev()
    assert get_value(st, path_elems(
        P('Cisco-IOS-XE-native:native', 'hostname'))) == 'GN1'


def test_get_single_interface_with_slash_in_name():
    st = _dev()
    v = get_value(st, path_elems(
        P('ietf-interfaces:interfaces',
          'interface[name=GigabitEthernet1/0/1]')))
    assert v['name'] == 'GigabitEthernet1/0/1'
    assert v['description'] == 'uplink-to-core'
    assert v['ietf-ip:ipv4']['address'][0]['ip'] == '10.77.0.1'
    assert v['ietf-ip:ipv4']['address'][0]['netmask'] == '255.255.255.0'


def test_get_interface_leaf():
    st = _dev()
    v = get_value(st, path_elems(
        P('ietf-interfaces:interfaces',
          'interface[name=GigabitEthernet1/0/1]', 'description')))
    assert v == 'uplink-to-core'


def test_get_all_interfaces():
    st = _dev()
    v = get_value(st, path_elems(P('ietf-interfaces:interfaces')))
    names = [i['name'] for i in v['interface']]
    assert 'GigabitEthernet1/0/1' in names
    # shutdown中は enabled=false
    gi2 = next(i for i in v['interface']
               if i['name'] == 'GigabitEthernet1/0/2')
    assert gi2['enabled'] is False


def test_get_unknown_path_returns_none():
    st = _dev()
    assert get_value(st, path_elems(P('no-such-model:foo'))) is None
    assert get_value(st, path_elems(
        P('ietf-interfaces:interfaces', 'interface[name=Gi9/9/9]'))) is None


# ── Set ────────────────────────────────────────────────
def test_set_description_updates_device_state():
    st = _dev()
    ok, err = set_value(st, path_elems(
        P('ietf-interfaces:interfaces',
          'interface[name=GigabitEthernet1/0/1]', 'description')),
        'configured-by-gNMI')
    assert ok, err
    assert st.interfaces['GigabitEthernet1/0/1']['desc'] == 'configured-by-gNMI'


def test_set_hostname():
    st = _dev()
    ok, _ = set_value(st, path_elems(
        P('Cisco-IOS-XE-native:native', 'hostname')), 'renamed')
    assert ok and st.hostname == 'renamed'


def test_set_interface_container_writes_ip_and_enabled():
    st = _dev()
    ok, err = set_value(st, path_elems(
        P('ietf-interfaces:interfaces',
          'interface[name=GigabitEthernet1/0/2]')),
        {'description': 'set-by-gnmi', 'enabled': True,
         'ietf-ip:ipv4': {'address': [{'ip': '192.0.2.9',
                                       'netmask': '255.255.255.0'}]}})
    assert ok, err
    info = st.interfaces['GigabitEthernet1/0/2']
    assert info['ip'] == '192.0.2.9'
    assert info['prefix'] == 24
    assert info['status'] == 'up'
    assert info['desc'] == 'set-by-gnmi'


def test_set_unknown_interface_fails():
    st = _dev()
    ok, err = set_value(st, path_elems(
        P('ietf-interfaces:interfaces', 'interface[name=Gi9/9/9]'),), 'x')
    assert not ok and 'does not exist' in err


def test_delete_description():
    st = _dev()
    ok, _ = set_value(st, path_elems(
        P('ietf-interfaces:interfaces',
          'interface[name=GigabitEthernet1/0/1]', 'description')),
        None, delete=True)
    assert ok
    assert st.interfaces['GigabitEthernet1/0/1']['desc'] == ''


def test_hostname_cannot_be_deleted():
    st = _dev()
    ok, err = set_value(st, path_elems(
        P('Cisco-IOS-XE-native:native', 'hostname')), None, delete=True)
    assert not ok and 'cannot be deleted' in err


# ── RPC ────────────────────────────────────────────────
def test_capabilities_advertises_models_and_encodings():
    svc = GnmiServicer('gn1', _dev())
    resp = svc.Capabilities(gnmi_pb2.CapabilityRequest(), _Ctx())
    assert resp.gNMI_version == GNMI_VERSION
    names = [m.name for m in resp.supported_models]
    assert 'ietf-interfaces' in names
    assert 'Cisco-IOS-XE-native' in names
    assert gnmi_pb2.JSON_IETF in resp.supported_encodings


def test_get_rpc_returns_json_ietf():
    svc = GnmiServicer('gn1', _dev())
    req = gnmi_pb2.GetRequest(
        path=[P('Cisco-IOS-XE-native:native', 'hostname')],
        encoding=gnmi_pb2.JSON_IETF)
    resp = svc.Get(req, _Ctx())
    val = resp.notification[0].update[0].val.json_ietf_val.decode()
    assert json.loads(val) == 'GN1'


def test_get_rpc_aborts_on_unknown_path():
    svc = GnmiServicer('gn1', _dev())
    ctx = _Ctx()
    with pytest.raises(RuntimeError):
        svc.Get(gnmi_pb2.GetRequest(path=[P('no-such:model')]), ctx)
    assert 'path not found' in ctx.details


def test_set_rpc_applies_update_and_reports_op():
    st = _dev()
    svc = GnmiServicer('gn1', st)
    req = gnmi_pb2.SetRequest()
    u = req.update.add()
    u.path.CopyFrom(P('ietf-interfaces:interfaces',
                      'interface[name=GigabitEthernet1/0/1]', 'description'))
    u.val.json_ietf_val = json.dumps('via-rpc').encode()
    resp = svc.Set(req, _Ctx())
    assert resp.response[0].op == gnmi_pb2.UpdateResult.UPDATE
    assert st.interfaces['GigabitEthernet1/0/1']['desc'] == 'via-rpc'


def test_set_rpc_delete_reports_delete_op():
    st = _dev()
    svc = GnmiServicer('gn1', st)
    req = gnmi_pb2.SetRequest()
    req.delete.add().CopyFrom(P('ietf-interfaces:interfaces',
                                'interface[name=GigabitEthernet1/0/1]',
                                'description'))
    resp = svc.Set(req, _Ctx())
    assert resp.response[0].op == gnmi_pb2.UpdateResult.DELETE
    assert st.interfaces['GigabitEthernet1/0/1']['desc'] == ''


def test_subscribe_once_sends_updates_then_sync():
    svc = GnmiServicer('gn1', _dev())
    sl = gnmi_pb2.SubscriptionList(mode=gnmi_pb2.SubscriptionList.ONCE,
                                   encoding=gnmi_pb2.JSON_IETF)
    sl.subscription.add().path.CopyFrom(
        P('Cisco-IOS-XE-native:native', 'hostname'))
    out = list(svc.Subscribe(
        iter([gnmi_pb2.SubscribeRequest(subscribe=sl)]), _Ctx()))
    assert json.loads(
        out[0].update.update[0].val.json_ietf_val.decode()) == 'GN1'
    assert out[-1].sync_response is True


def test_subscribe_poll_responds_to_each_poll():
    svc = GnmiServicer('gn1', _dev())
    sl = gnmi_pb2.SubscriptionList(mode=gnmi_pb2.SubscriptionList.POLL,
                                   encoding=gnmi_pb2.JSON_IETF)
    sl.subscription.add().path.CopyFrom(
        P('Cisco-IOS-XE-native:native', 'hostname'))
    reqs = [gnmi_pb2.SubscribeRequest(subscribe=sl),
            gnmi_pb2.SubscribeRequest(poll=gnmi_pb2.Poll())]
    out = list(svc.Subscribe(iter(reqs), _Ctx()))
    # 購読時の1回 + pollでの1回、それぞれ sync_response が付く
    assert sum(1 for r in out if r.sync_response) == 2


def test_subscribe_skips_paths_that_do_not_resolve():
    svc = GnmiServicer('gn1', _dev())
    sl = gnmi_pb2.SubscriptionList(mode=gnmi_pb2.SubscriptionList.ONCE,
                                   encoding=gnmi_pb2.JSON_IETF)
    sl.subscription.add().path.CopyFrom(P('no-such:model'))
    out = list(svc.Subscribe(
        iter([gnmi_pb2.SubscribeRequest(subscribe=sl)]), _Ctx()))
    assert len(out) == 1 and out[0].sync_response is True


# ── show gnxi state ────────────────────────────────────
def test_show_gnxi_state_when_disabled():
    assert 'not enabled' in format_show_gnxi_state(_dev())


def test_show_gnxi_state_detail():
    st = _dev()
    st.gnxi_enabled = True
    st.gnxi_server = True
    out = format_show_gnxi_state(st, detail=True)
    assert 'Server: Enabled' in out
    assert 'Server port: 50052' in out
    assert 'JSON_IETF' in out
    assert f'gNMI version: {GNMI_VERSION}' in out
