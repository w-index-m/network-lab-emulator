"""
STP を有効にした「後で」張ったリンクの扱い(Catalyst 3台の三角形)

- 後から張ったリンクも STP ポートになり、三角形のどこかが Blocking になる
  (以前は起動時にあったリンクしかポートにならず、BPDU を捨てていた)
- STP が動いているスイッチだけのループでは MAC フラップ/ストームのログを出さない
  (実機は冗長リンクを Blocking にするのでループにならない)
- shutdown したリンクを STP ポートとして作り直さない(復旧時に相手側の
  %LINK-3-UPDOWN ... up が出ず、/cyber-view の SYSLOG 判定が down のまま残っていた)
"""

import os
import sys
import time
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.traffic_analytics import recorder  # noqa: E402

_RUN = uuid.uuid4().hex[:6]
BLOCK = ('BLK', 'Altn', 'BLOCKING', 'Blocking', 'Discarding')


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


def _wait(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


@pytest.fixture
def triangle(client):
    ids = [f'stl-{x}-{_RUN}-{uuid.uuid4().hex[:4]}' for x in 'abc']
    a, b, c = ids
    for d in ids:
        client.post('/api/device', json={'id': d, 'type': 'catalyst', 'hostname': d.upper()})
        _cli(client, d, 'configure terminal', 'spanning-tree mode rapid-pvst', 'end')
    _cli(client, a, 'configure terminal', 'spanning-tree vlan 1 priority 4096', 'end')
    recorder.clear()
    client.post('/api/link', json={'a': a, 'b': b, 'iface_a': 'GigabitEthernet1/0/1', 'iface_b': 'GigabitEthernet1/0/1'})
    client.post('/api/link', json={'a': b, 'b': c, 'iface_a': 'GigabitEthernet1/0/2', 'iface_b': 'GigabitEthernet1/0/1'})
    client.post('/api/link', json={'a': c, 'b': a, 'iface_a': 'GigabitEthernet1/0/2', 'iface_b': 'GigabitEthernet1/0/2'})
    return client, ids


def test_links_added_after_stp_start_join_and_block(triangle):
    c, (a, b, cc) = triangle

    def blocked():
        outs = [_cli(c, d, 'show spanning-tree') for d in (b, cc)]
        return any(k in o for o in outs for k in BLOCK) and outs
    outs = _wait(blocked)
    assert outs and any(k in o for o in outs for k in BLOCK), outs
    assert 'Gi1/0/2' in _cli(c, b, 'show spanning-tree')     # 後から張った B-C もポートになっている


def test_no_mac_flap_when_loop_switches_run_stp(triangle):
    _c, ids = triangle
    time.sleep(1)
    assert not [x for x in recorder.alerts if x['rule'] == 'mac_flap' and x['src'] in ids]
    assert not [x for x in recorder.logs if x['dev'] in ids and 'MACFLAP' in x['msg']]


def test_recovery_clears_syslog_evidence(triangle):
    c, (a, b, _cc) = triangle
    time.sleep(2)
    _cli(c, a, 'configure terminal', 'interface GigabitEthernet1/0/1', 'shutdown', 'end')
    time.sleep(2.5)        # BPDU 送信(1秒周期)を挟んでも落ちたリンクのポートを作り直さないこと
    _cli(c, a, 'configure terminal', 'interface GigabitEthernet1/0/1', 'no shutdown', 'end')

    def link():
        for ln in c.get('/api/traffic/links').json()['links']:
            if {ln['a'], ln['b']} == {a, b}:
                return ln
    # 「断でない」と「SYSLOG の根拠が up」は別々の経路で届くので、両方そろうまで待つ(CI の遅い環境で前者だけ先に来た)
    ln = _wait(lambda: (lambda x: x if x and not x['down'] and x['evidence'].get('SYSLOG') == 'up' else None)(link()), 20)
    assert ln and not ln['down'] and ln['evidence']['SYSLOG'] == 'up', link()


def test_blocking_log_is_not_repeated(triangle):
    """Blocking のログは状態が変わったときだけ(以前は BPDU のたびに約2秒ごとに出ていた)"""
    _c, ids = triangle
    time.sleep(6)
    n = [x for x in recorder.logs if x['dev'] in ids and 'PORTDPVSTP' in x['msg'] and 'BLOCKING' in x['msg']]
    assert 1 <= len(n) <= 2, [x['msg'] for x in n]
