"""
モデル駆動型テレメトリ（MDT / telemetry ietf subscription）のテスト

実機構文（Cisco IOS-XE, gRPC Dial-Out）:
    telemetry ietf subscription 101
     encoding encode-kvgpb
     filter xpath /process-cpu-ios-xe-oper:cpu-usage/cpu-utilization/five-seconds
     source-address 10.1.1.5
     stream yang-push
     update-policy periodic 500
     receiver ip address 10.1.1.3 57500 protocol grpc-tcp

period は**センチ秒**（500 = 5秒）。実機は stream / encoding / filter /
receiver / update-policy が揃って初めて State: Valid になる。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient      # noqa: E402

import app as app_module                       # noqa: E402

client = TestClient(app_module.app)

DEV = 't-mdt'
XPATH = '/process-cpu-ios-xe-oper:cpu-usage/cpu-utilization/five-seconds'


def _cli(cmd, dev=DEV):
    return client.post('/api/cli',
                       json={'device_id': dev, 'command': cmd}).json()['output']


def _setup(dev=DEV, full=True):
    app_module.device_sessions.pop(dev, None)
    client.post('/api/device',
                json={'id': dev, 'type': 'catalyst', 'hostname': dev})
    cmds = ['configure terminal', 'telemetry ietf subscription 101']
    if full:
        cmds += ['encoding encode-kvgpb',
                 f'filter xpath {XPATH}',
                 'source-address 10.77.0.1',
                 'stream yang-push',
                 'update-policy periodic 500',
                 'receiver ip address 10.77.0.99 57500 protocol grpc-tcp']
    cmds.append('end')
    for c in cmds:
        _cli(c, dev)
    return dev


def _sub(dev=DEV, sid=101):
    return app_module.device_sessions[dev].mdt_subscriptions[sid]


# ── 設定 ───────────────────────────────────────────────
def test_subscription_fields_are_stored():
    _setup()
    s = _sub()
    assert s['encoding'] == 'encode-kvgpb'
    assert s['xpath'] == XPATH
    assert s['source_address'] == '10.77.0.1'
    assert s['stream'] == 'yang-push'
    assert s['trigger'] == 'periodic'
    assert s['period'] == 500
    assert s['receivers'] == [{'address': '10.77.0.99', 'port': 57500,
                               'protocol': 'grpc-tcp', 'state': 'Connected'}]


def test_xpath_case_is_preserved():
    """xpathはモデル名の大小文字を保つこと（小文字化すると別物になる）"""
    dev = _setup(full=False)
    for c in ('configure terminal', 'telemetry ietf subscription 101',
              'filter xpath /Cisco-IOS-XE-memory-oper:memory-statistics', 'end'):
        _cli(c, dev)
    assert _sub(dev)['xpath'] == '/Cisco-IOS-XE-memory-oper:memory-statistics'


def test_on_change_trigger():
    dev = _setup(full=False)
    for c in ('configure terminal', 'telemetry ietf subscription 101',
              'update-policy on-change', 'end'):
        _cli(c, dev)
    s = _sub(dev)
    assert s['trigger'] == 'on-change'
    assert s['period'] is None


def test_period_below_minimum_is_rejected():
    """実機の最小値は100センチ秒(1秒)"""
    dev = _setup(full=False)
    _cli('configure terminal', dev)
    _cli('telemetry ietf subscription 101', dev)
    out = _cli('update-policy periodic 50', dev)
    assert 'at least 100' in out
    assert _sub(dev)['period'] is None


def test_receiver_can_be_replaced_and_removed():
    dev = _setup()
    for c in ('configure terminal', 'telemetry ietf subscription 101',
              'receiver ip address 10.77.0.99 57500 protocol cntp-tcp', 'end'):
        _cli(c, dev)
    # 同じ address/port は置き換え（重複しない）
    assert len(_sub(dev)['receivers']) == 1
    assert _sub(dev)['receivers'][0]['protocol'] == 'cntp-tcp'
    for c in ('configure terminal', 'telemetry ietf subscription 101',
              'no receiver ip address 10.77.0.99 57500', 'end'):
        _cli(c, dev)
    assert _sub(dev)['receivers'] == []


def test_subscription_can_be_removed():
    dev = _setup()
    for c in ('configure terminal',
              'no telemetry ietf subscription 101', 'end'):
        _cli(c, dev)
    assert 101 not in app_module.device_sessions[dev].mdt_subscriptions


def test_multiple_subscriptions():
    dev = _setup()
    for c in ('configure terminal', 'telemetry ietf subscription 202',
              'stream yang-push', 'end'):
        _cli(c, dev)
    subs = app_module.device_sessions[dev].mdt_subscriptions
    assert set(subs) == {101, 202}


# ── 表示 ───────────────────────────────────────────────
def test_show_subscription_all():
    dev = _setup()
    out = _cli('show telemetry ietf subscription all', dev)
    assert 'Telemetry subscription brief' in out
    assert 'ID               Type        State       Filter type' in out
    assert '101' in out and 'Configured' in out and 'Valid' in out


def test_show_subscription_detail():
    dev = _setup()
    out = _cli('show telemetry ietf subscription 101 detail', dev)
    assert 'Subscription ID: 101' in out
    assert 'State: Valid' in out
    assert 'Stream: yang-push' in out
    assert f'XPath: {XPATH}' in out
    assert 'Update Trigger: periodic' in out
    assert 'Period: 500' in out
    assert 'Encoding: encode-kvgpb' in out
    assert 'Receivers:' in out
    assert '10.77.0.99       57500            grpc-tcp' in out


def test_show_subscription_receiver():
    dev = _setup()
    out = _cli('show telemetry ietf subscription 101 receiver', dev)
    assert 'Address: 10.77.0.99' in out
    assert 'Port: 57500' in out
    assert 'Protocol: grpc-tcp' in out
    assert 'State: Connected' in out


def test_incomplete_subscription_is_invalid():
    """receiver等が揃っていないうちは Invalid"""
    dev = _setup(full=False)
    out = _cli('show telemetry ietf subscription 101 detail', dev)
    assert 'State: Invalid' in out
    assert 'show telemetry' and 'Invalid' in _cli(
        'show telemetry ietf subscription all', dev)


def test_show_unknown_subscription():
    dev = _setup()
    assert 'not found' in _cli(
        'show telemetry ietf subscription 999 detail', dev)


def test_running_config_round_trips():
    """running-configがそのまま投入し直せる形で出ること"""
    dev = _setup()
    out = _cli('show running-config', dev)
    assert 'telemetry ietf subscription 101' in out
    assert ' encoding encode-kvgpb' in out
    assert f' filter xpath {XPATH}' in out
    assert ' stream yang-push' in out
    assert ' update-policy periodic 500' in out
    assert (' receiver ip address 10.77.0.99 57500 protocol grpc-tcp') in out
