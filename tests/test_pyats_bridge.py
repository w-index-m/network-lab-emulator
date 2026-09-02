"""
tools/telnet_bridge.py 経由で、本物のpyATS/unicon/Genieから
このエミュレーターへ実際に接続・execute・parseできることを確認する
テスト。

pyats/unicon/genie（このリポジトリのvenv外、別途 /opt/pyats-venv 等に
インストールされる想定）と、telnetクライアントバイナリ(uniconが
`telnet`コマンドを直接spawnする)が無い環境では自動skipする。

実行にはエミュレーター本体(app.py)がNETLAB_AUTH_DISABLE=1で起動して
いる必要があるため、フィクスチャでサブプロセス起動する。
"""

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from pyats.topology import loader as pyats_loader
    HAS_PYATS = True
except ImportError:
    HAS_PYATS = False

HAS_TELNET_BIN = shutil.which('telnet') is not None

pytestmark = pytest.mark.skipif(
    not (HAS_PYATS and HAS_TELNET_BIN),
    reason="pyats or telnet client binary not installed",
)

EMULATOR_PORT = 8098
BRIDGE_PORT = 2399


def _wait_port(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope='module')
def emulator_and_bridge():
    repo_root = os.path.join(os.path.dirname(__file__), '..')
    env = os.environ.copy()
    env['NETLAB_AUTH_DISABLE'] = '1'

    app_proc = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'app:app', '--port', str(EMULATOR_PORT)],
        cwd=repo_root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not _wait_port('127.0.0.1', EMULATOR_PORT):
        app_proc.terminate()
        pytest.skip('emulator did not start')

    bridge_proc = subprocess.Popen(
        [sys.executable, 'tools/telnet_bridge.py', '--device', 'catalyst',
         '--port', str(BRIDGE_PORT),
         '--emulator-url', f'http://127.0.0.1:{EMULATOR_PORT}'],
        cwd=repo_root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not _wait_port('127.0.0.1', BRIDGE_PORT):
        app_proc.terminate()
        bridge_proc.terminate()
        pytest.skip('telnet bridge did not start')

    yield

    bridge_proc.terminate()
    app_proc.terminate()
    bridge_proc.wait(timeout=5)
    app_proc.wait(timeout=5)


@pytest.fixture
def testbed_yaml(tmp_path):
    content = f"""
testbed:
  name: netlab-test
devices:
  catalyst:
    os: ios
    type: switch
    connections:
      cli:
        protocol: telnet
        ip: 127.0.0.1
        port: {BRIDGE_PORT}
"""
    p = tmp_path / 'testbed.yaml'
    p.write_text(content)
    return str(p)


def test_pyats_connect_and_execute(emulator_and_bridge, testbed_yaml):
    tb = pyats_loader.load(testbed_yaml)
    dev = tb.devices['catalyst']
    dev.connect(log_stdout=False, learn_hostname=True)
    try:
        assert dev.connected
        out = dev.execute('show version')
        assert 'Cisco IOS' in out
    finally:
        dev.disconnect()


def test_genie_parse_show_ip_interface_brief(emulator_and_bridge, testbed_yaml):
    tb = pyats_loader.load(testbed_yaml)
    dev = tb.devices['catalyst']
    dev.connect(log_stdout=False, learn_hostname=True)
    try:
        parsed = dev.parse('show ip interface brief')
        assert 'GigabitEthernet1/0/1' in parsed['interface']
        assert parsed['interface']['GigabitEthernet1/0/1']['ip_address'] == '10.9.9.1'
    finally:
        dev.disconnect()


def test_genie_parse_show_version(emulator_and_bridge, testbed_yaml):
    tb = pyats_loader.load(testbed_yaml)
    dev = tb.devices['catalyst']
    dev.connect(log_stdout=False, learn_hostname=True)
    try:
        parsed = dev.parse('show version')
        assert 'version' in parsed
    finally:
        dev.disconnect()
