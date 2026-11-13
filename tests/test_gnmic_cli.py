"""
本物のgnmic(openconfig/gnmic CLI)からエミュレータのgNMIリスナー
(engine/gnmi_agent.py)を操作する結合テスト。

capabilities / get / set / subscribe(once・stream sample・stream on-change)を
実CLIで叩き、装置状態(DeviceState)との往復を確かめる。on-changeは
以前はSAMPLE扱い(既定10秒周期)で変化が即時に届かなかった実バグの回帰防止。

gnmicがPATH(または~/go/bin)に無い環境ではファイルごとskipする。
"""

import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytest.importorskip('grpc')
from engine.gnmi_agent import GnmiServer  # noqa: E402
from engine.rules import DeviceState  # noqa: E402

_home_bin = os.path.expanduser('~/go/bin/gnmic')
GNMIC = shutil.which('gnmic') or (_home_bin if os.path.exists(_home_bin) else None)
pytestmark = pytest.mark.skipif(not GNMIC, reason='gnmic not installed')

IP, PORT = '127.0.33.1', 50853
BASE = [GNMIC or 'gnmic', '-a', f'{IP}:{PORT}', '--insecure', '-u', 'admin', '-p', 'admin']


@pytest.fixture
def dev():
    st = DeviceState('cisco', 'GNMIC-R1')
    srv = GnmiServer('gnmic-test', IP, st, port=PORT)
    srv.start()
    yield st
    srv.stop()


def _run(*args, timeout=20):
    try:
        r = subprocess.run(BASE + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b'') + (e.stderr or b'')
        return None, out.decode() if isinstance(out, bytes) else out


def test_capabilities(dev):
    rc, out = _run('capabilities')
    assert rc == 0, out
    assert 'gNMI version: 0.8.0' in out
    assert 'JSON_IETF' in out


def test_get_hostname_and_interface(dev):
    rc, out = _run('get', '--path', '/native/hostname')
    assert rc == 0 and json.loads(out)[0]['updates'][0]['values']['native/hostname'] == 'GNMIC-R1'
    ifn = next(iter(dev.interfaces))
    rc, out = _run('get', '--path', f'/interfaces/interface[name={ifn}]')
    assert rc == 0, out
    val = json.loads(out)[0]['updates'][0]['values']['interfaces/interface']
    assert val['name'] == ifn and val['ietf-ip:ipv4']['address'][0]['ip'] == dev.interfaces[ifn]['ip']


def test_get_unknown_path_fails(dev):
    rc, out = _run('get', '--path', '/foo/bar')
    assert rc != 0 and 'NotFound' in out


def test_set_updates_device_state(dev):
    ifn = next(iter(dev.interfaces))
    rc, out = _run('set', '--update-path', f'/interfaces/interface[name={ifn}]/description',
                   '--update-value', 'set-by-gnmic')
    assert rc == 0, out
    assert dev.interfaces[ifn]['desc'] == 'set-by-gnmic'
    rc, out = _run('set', '--update-path', '/native/hostname', '--update-value', 'RENAMED')
    assert rc == 0 and dev.hostname == 'RENAMED'


def test_set_unknown_interface_fails(dev):
    rc, out = _run('set', '--update-path', '/interfaces/interface[name=NoSuch1]/description',
                   '--update-value', 'x')
    assert rc != 0 and 'does not exist' in out


def test_subscribe_once(dev):
    rc, out = _run('subscribe', '--path', '/native/hostname', '--mode', 'once')
    assert rc == 0 and '"GNMIC-R1"' in out and 'sync-response' in out


def test_subscribe_stream_on_change_delivers_change_immediately(dev):
    threading.Timer(1.0, lambda: setattr(dev, 'hostname', 'CHANGED-1')).start()
    _rc, out = _run('subscribe', '--path', '/native/hostname', '--mode', 'stream',
                    '--stream-mode', 'on-change', '--format', 'event', timeout=3.5)
    assert '"GNMIC-R1"' in out and '"CHANGED-1"' in out
    # on-changeは変化が無ければ繰り返し送らない
    assert out.count('"/native/hostname"') == 2


def test_subscribe_stream_sample(dev):
    _rc, out = _run('subscribe', '--path', '/native/hostname', '--mode', 'stream',
                    '--stream-mode', 'sample', '--sample-interval', '1s', '--format', 'event',
                    timeout=3.5)
    assert out.count('"/native/hostname"') >= 3
