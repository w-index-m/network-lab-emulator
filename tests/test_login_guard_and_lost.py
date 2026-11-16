"""
IOS login block-for / 接続スキャン検知(IDS) / SNMP 応答なし(消失)の回帰テスト

- login block-for: 実 SSH リスナーへ本物の paramiko で誤パスワードを繰り返すと Quiet Mode に
  入り、以降の接続を拒否、%SEC_LOGIN-1-QUIET_MODE_ON を出す。期限で QUIET_MODE_OFF
- conn_scan: 実リスナーへ短時間に多数の TCP 接続 → IDS アラート(装置の場所に出る)
- 応答なし: Prometheus の up{job="cisco-snmp"}=0 の装置を「応答なし」にし、リンクは
  state=lost / POLL=lost、状態ボードのネットワーク観点は異常
"""

import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine import login_guard  # noqa: E402
from engine.rules import DeviceState  # noqa: E402
from engine.traffic_analytics import recorder  # noqa: E402

paramiko = pytest.importorskip('paramiko')
_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


class TestLoginBlockForCli:
    def test_config_show_and_running_config(self, client):
        dev = f'lg-cli-{_RUN}'
        client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': 'LG-CLI'})
        assert 'NOT enabled' in _cli(client, dev, 'show login')
        _cli(client, dev, 'configure terminal', 'login block-for 30 attempts 3 within 60',
             'login on-failure log', 'end')
        out = _cli(client, dev, 'show login')
        assert 'If more than 3 login failures occur in 60 seconds' in out and 'Normal-Mode' in out
        rc = _cli(client, dev, 'show running-config')
        assert 'login block-for 30 attempts 3 within 60' in rc and 'login on-failure log' in rc
        _cli(client, dev, 'configure terminal', 'no login block-for', 'end')
        assert 'login block-for' not in _cli(client, dev, 'show running-config')


class TestLoginGuardUnit:
    def test_quiet_mode_on_and_off(self):
        st = DeviceState('cisco', 'U1')
        dev = f'lg-unit-{_RUN}'
        logs = []
        prev = login_guard._logger
        login_guard.set_logger(lambda d, f, sev, m, txt: logs.append((sev, m)))
        try:
            login_guard.handle_config(st, 'login block-for 20 attempts 3 within 60')
            t0 = 1000.0
            assert not login_guard.record_failure(dev, st, 'x', '1.1.1.1', now=t0)
            assert not login_guard.record_failure(dev, st, 'x', '1.1.1.1', now=t0 + 1)
            assert login_guard.record_failure(dev, st, 'x', '1.1.1.1', now=t0 + 2)
            assert (1, 'QUIET_MODE_ON') in logs
            assert login_guard.is_quiet(dev, st, now=t0 + 10) > 0
            assert login_guard.is_quiet(dev, st, now=t0 + 23) == 0
            assert (5, 'QUIET_MODE_OFF') in logs
        finally:
            login_guard.set_logger(prev)
            login_guard.reset(dev)


@pytest.fixture
def ssh_dev():
    """実 SSH リスナー(SshCliServer)を空きポートで直接起動する"""
    from engine.ssh_cli_agent import SshCliServer
    st = DeviceState('cisco', f'LG-SSH-{_RUN}')
    dev = f'lg-ssh-{_RUN}'
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    srv = SshCliServer(dev, '127.0.0.1', st, lambda d, cmd: 'ok', port=port)
    srv.start()
    yield dev, st, port
    srv.stop() if hasattr(srv, 'stop') else None
    login_guard.reset(dev)


def _try_login(port, pw):
    t = paramiko.Transport(('127.0.0.1', port))
    try:
        t.connect(username='admin', password=pw)
        return t.is_authenticated()
    except (paramiko.AuthenticationException, paramiko.SSHException, OSError, EOFError):
        return False
    finally:
        t.close()


def test_real_ssh_brute_force_triggers_quiet_mode(ssh_dev):
    dev, st, port = ssh_dev
    logs = []
    prev = login_guard._logger
    login_guard.set_logger(lambda d, f, sev, m, txt: logs.append((d, sev, m, txt)))
    try:
        login_guard.handle_config(st, 'login block-for 30 attempts 3 within 60')
        login_guard.handle_config(st, 'login on-failure log')
        assert _try_login(port, 'admin')                     # 正しいパスワードは通る
        for _ in range(3):
            assert not _try_login(port, 'wrong')
        assert any(m == 'QUIET_MODE_ON' and '[Source: 127.0.0.1]' in txt for _d, _s, m, txt in logs)
        assert sum(1 for x in logs if x[2] == 'LOGIN_FAILED') == 3
        assert not _try_login(port, 'admin')                 # Quiet Mode 中は正しいパスワードでも拒否
        assert 'Quiet-Mode' in login_guard.show_login(dev, st)
    finally:
        login_guard.set_logger(prev)


def test_quiet_mode_log_reaches_syslog_view(client):
    """app に登録されたロガー経由で show logging と /cyber-view の syslog・アラートに出る"""
    dev = f'lg-log-{_RUN}'
    client.post('/api/device', json={'id': dev, 'type': 'cisco', 'hostname': 'LG-LOG'})
    _cli(client, dev, 'configure terminal', 'login block-for 30 attempts 2 within 60', 'end')
    st = app_module.device_sessions[dev]
    recorder.clear()
    try:
        login_guard.record_failure(dev, st, 'root', '203.0.113.99')
        login_guard.record_failure(dev, st, 'root', '203.0.113.99')
        assert '%SEC_LOGIN-1-QUIET_MODE_ON' in _cli(client, dev, 'show logging')
        assert any(a['rule'] == 'syslog_error' and a['src'] == dev for a in recorder.alerts)
    finally:
        login_guard.reset(dev)


def test_conn_scan_alert_from_real_tcp_connects(ssh_dev):
    dev, _st, port = ssh_dev
    recorder.clear()
    for _ in range(12):
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    end = time.time() + 3
    while time.time() < end and not any(a['rule'] == 'conn_scan' for a in recorder.alerts):
        time.sleep(0.05)
    a = next(a for a in recorder.alerts if a['rule'] == 'conn_scan')
    assert a['source'] == 'IDS' and a['src'] == dev and a['dst'] == '127.0.0.1'
    assert f'port {port}' in a['detail']


class TestNoResponse:
    def test_prometheus_up_zero_marks_device_lost(self, client, monkeypatch):
        saved = dict(app_module.device_sessions)
        links = {k: set(v) for k, v in app_module.vnet.links.items()}
        app_module.device_sessions.clear()
        app_module.vnet.links.clear()
        r1, r2 = f'nr-r1-{_RUN}', f'nr-r2-{_RUN}'

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if 'up%7B' in self.path or 'up{' in self.path:
                    res = [{'metric': {'instance': '10.247.12.1'}, 'value': [0, '0']},
                           {'metric': {'instance': '10.247.12.2'}, 'value': [0, '1']}]
                else:
                    res = []
                body = json.dumps({'status': 'success', 'data': {'result': res}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'NR-R1'})
            client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'NR-R2'})
            client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                           'iface_b': 'GigabitEthernet0/0/0'})
            for d, ip in ((r1, '10.247.12.1'), (r2, '10.247.12.2')):
                _cli(client, d, 'configure terminal', 'interface GigabitEthernet0/0/0',
                     f'ip address {ip} 255.255.255.252', 'no shutdown', 'end')
            recorder.clear()
            monkeypatch.setenv('PROMETHEUS_URL', f'http://127.0.0.1:{srv.server_port}')
            d = client.get('/api/traffic/links').json()
            ln = d['links'][0]
            assert ln['state'] == 'lost' and ln['evidence']['POLL'] == 'lost' and ln['lost'] == ['NR-R1']
            assert d['unreachable'] == [{'id': r1, 'hostname': 'NR-R1', 'instance': '10.247.12.1'}]
            h = {x['key']: x for x in client.get('/api/traffic/health').json()['categories']}
            assert h['network']['state'] == 'crit' and '応答なし 1台' in h['network']['summary']
            assert h['network']['details'][0] == '応答なし: NR-R1 (10.247.12.1)'
        finally:
            srv.shutdown()
            app_module.device_sessions.clear()
            app_module.device_sessions.update(saved)
            app_module.vnet.links.clear()
            app_module.vnet.links.update(links)
