"""
Cisco ASAの"threat-detection scanning-threat"(スキャン検知/自動シャン)。

ZGrab2/nmapのような短時間多数接続のスキャンを検知する機能として、
ASAの`threat-detection scanning-threat`(実機の正式機能)を実装した
——「④ZGrab2のスキャンをCatalyst側で検知する...今ある中ならASAの方が
面白い？」というユーザー提案への対応。

CLI設定(engine/rules.py `_asa_process`)のテストと、実SSHリスナー
(engine/ssh_cli_agent.py)への実際のTCP接続試行をレート判定してシャン
する、エンドツーエンドのテストの両方を持つ。後者は本物のuvicornサーバ
を立て、生のTCPソケットで接続を繰り返し実際にシャンされる(以降の
接続が即座に閉じられる)ことを確認する——tests/test_zgrab2_ssh_banner.py
と同じ自己完結サーバfixtureパターンだが、外部バイナリ(zgrab2)には
依存しない(素のsocket.create_connectionのみ)。
"""

import json
import os
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')

from fastapi.testclient import TestClient
import app as app_module

client = TestClient(app_module.app)


def _dev(id_, type_='asa'):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


def _cli(id_, cmd):
    return client.post('/api/cli', json={'device_id': id_, 'command': cmd}).json()['output']


class TestThreatDetectionConfigCli:
    def test_scanning_threat_enables_without_shun(self):
        _dev('td-cli-1')
        _cli('td-cli-1', 'configure terminal')
        _cli('td-cli-1', 'threat-detection scanning-threat')
        state = app_module.device_sessions['td-cli-1']
        assert state.threat_detection['enabled'] is True
        assert state.threat_detection['shun'] is False

    def test_scanning_threat_shun_enables_shun(self):
        _dev('td-cli-2')
        _cli('td-cli-2', 'configure terminal')
        _cli('td-cli-2', 'threat-detection scanning-threat shun')
        state = app_module.device_sessions['td-cli-2']
        assert state.threat_detection['enabled'] is True
        assert state.threat_detection['shun'] is True

    def test_shun_except_records_exception(self):
        _dev('td-cli-3')
        _cli('td-cli-3', 'configure terminal')
        _cli('td-cli-3', 'threat-detection scanning-threat shun except ip-address 10.0.0.5 255.255.255.255')
        state = app_module.device_sessions['td-cli-3']
        assert state.threat_detection['shun'] is True
        assert ('10.0.0.5', '255.255.255.255') in state.threat_detection['shun_except']

    def test_rate_command_sets_rate_fields(self):
        _dev('td-cli-4')
        _cli('td-cli-4', 'configure terminal')
        _cli('td-cli-4', 'threat-detection rate scanning-threat rate-interval 10 average-rate 2 burst-rate 3')
        state = app_module.device_sessions['td-cli-4']
        assert state.threat_detection['rate_interval'] == 10
        assert state.threat_detection['average_rate'] == 2
        assert state.threat_detection['burst_rate'] == 3

    def test_shun_duration_command(self):
        _dev('td-cli-5')
        _cli('td-cli-5', 'configure terminal')
        _cli('td-cli-5', 'threat-detection shun duration 120')
        state = app_module.device_sessions['td-cli-5']
        assert state.threat_detection['shun_duration'] == 120

    def test_no_threat_detection_disables(self):
        _dev('td-cli-6')
        _cli('td-cli-6', 'configure terminal')
        _cli('td-cli-6', 'threat-detection scanning-threat shun')
        _cli('td-cli-6', 'no threat-detection scanning-threat')
        state = app_module.device_sessions['td-cli-6']
        assert state.threat_detection['enabled'] is False
        assert state.threat_detection['shun'] is False

    def test_show_rate_reports_not_enabled_by_default(self):
        _dev('td-cli-7')
        out = _cli('td-cli-7', 'show threat-detection rate')
        assert 'not enabled' in out

    def test_show_shun_reports_no_shunned_hosts_by_default(self):
        _dev('td-cli-8')
        out = _cli('td-cli-8', 'show threat-detection shun')
        assert out == 'No shunned hosts.'

    def test_running_config_reflects_shun_and_rate(self):
        _dev('td-cli-9')
        _cli('td-cli-9', 'configure terminal')
        _cli('td-cli-9', 'threat-detection scanning-threat shun')
        _cli('td-cli-9', 'threat-detection rate scanning-threat rate-interval 10 average-rate 2 burst-rate 3')
        out = _cli('td-cli-9', 'show running-config')
        assert 'threat-detection scanning-threat shun' in out
        assert 'threat-detection rate scanning-threat rate-interval 10 average-rate 2 burst-rate 3' in out


class TestThreatDetectionEngineDirect:
    """engine.threat_detection.ThreatDetectionEngineそのものの振る舞い
    (スライディングウィンドウでの閾値判定/シャン/期限切れ)を、実SSH
    リスナーを介さず直接固定する。"""

    def test_attempts_under_threshold_are_not_blocked(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 60,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 100,
               'burst_rate': 100}
        for _ in range(5):
            assert eng.record_connection_attempt('k', '203.0.113.50', cfg) is False

    def test_exceeding_average_rate_triggers_shun(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 60,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 1,
               'burst_rate': 1}
        results = [eng.record_connection_attempt('k', '203.0.113.50', cfg) for _ in range(15)]
        assert any(results), '閾値超過後は遮断されるはず'
        assert eng.is_shunned('k', '203.0.113.50', cfg) is True

    def test_shun_disabled_only_reports_no_actual_block(self):
        """shun無し(検知のみ)設定では、閾値を超えても接続自体は遮断しない。"""
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': False, 'shun_duration': 60,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 1,
               'burst_rate': 1}
        results = [eng.record_connection_attempt('k', '203.0.113.50', cfg) for _ in range(15)]
        assert not any(results)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is False

    def test_other_source_ip_unaffected(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 60,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 1,
               'burst_rate': 1}
        for _ in range(15):
            eng.record_connection_attempt('k', '203.0.113.50', cfg)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is True
        assert eng.is_shunned('k', '198.51.100.9', cfg) is False

    def test_shun_except_exempts_source(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 60,
               'shun_except': [('203.0.113.50', '255.255.255.255')],
               'rate_interval': 10, 'average_rate': 1, 'burst_rate': 1}
        for _ in range(15):
            eng.record_connection_attempt('k', '203.0.113.50', cfg)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is False

    def test_shun_expires_after_duration(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 1,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 1,
               'burst_rate': 1}
        for _ in range(15):
            eng.record_connection_attempt('k', '203.0.113.50', cfg)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is True
        time.sleep(1.2)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is False

    def test_clear_shun_removes_entry(self):
        from engine.threat_detection import ThreatDetectionEngine
        eng = ThreatDetectionEngine()
        cfg = {'enabled': True, 'shun': True, 'shun_duration': 60,
               'shun_except': [], 'rate_interval': 10, 'average_rate': 1,
               'burst_rate': 1}
        for _ in range(15):
            eng.record_connection_attempt('k', '203.0.113.50', cfg)
        assert eng.is_shunned('k', '203.0.113.50', cfg) is True
        eng.clear_shun('k', '203.0.113.50')
        assert eng.is_shunned('k', '203.0.113.50', cfg) is False


# ── エンドツーエンド: 実SSHリスナーへの実際のTCP接続試行で検証 ──

PORT = 8130
BASE = f'http://127.0.0.1:{PORT}'
DEV = 'td-live-asa'
MGMT_IP = '203.0.113.1'  # ASAのデフォルトoutside IP


def _live_cli(cmd, dev=DEV):
    import urllib.request
    req = urllib.request.Request(
        BASE + '/api/cli', method='POST',
        data=json.dumps({'device_id': dev, 'command': cmd}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['output']


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    log = tmp_path_factory.mktemp('netlab-td') / 'server.log'
    env = dict(os.environ, NETLAB_AUTH_DISABLE='1')
    import urllib.request
    with open(log, 'wb') as fh:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app:app',
             '--host', '127.0.0.1', '--port', str(PORT)],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        try:
            for _ in range(120):
                try:
                    urllib.request.urlopen(BASE + '/api/health', timeout=5).read()
                    break
                except Exception:
                    if proc.poll() is not None:          # pragma: no cover
                        pytest.fail('サーバが起動しませんでした:\n'
                                    + log.read_text(errors='replace')[-2000:])
                    time.sleep(0.5)
            else:                                        # pragma: no cover
                pytest.fail('サーバが時間内に起動しませんでした')
            yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:            # pragma: no cover
                proc.kill()


@pytest.fixture(scope='module')
def scanning_threat_asa(server):
    import urllib.request
    req = urllib.request.Request(
        BASE + '/api/device', method='POST',
        data=json.dumps({'id': DEV, 'type': 'asa', 'hostname': DEV}).encode(),
        headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=30).read()
    for cmd in ('configure terminal',
                'crypto key generate rsa modulus 2048',
                # 短い観測窓・低い閾値にして、テスト内で現実的な時間で
                # 閾値超過させる(実機の既定値 600秒/5接続/秒 のままでは
                # テストが終わらない)
                'threat-detection rate scanning-threat rate-interval 5 average-rate 3 burst-rate 3',
                'threat-detection scanning-threat shun',
                'end'):
        _live_cli(cmd)
    time.sleep(2)  # 実SSHリスナーのbind待ち
    yield MGMT_IP


def _raw_connect_and_check_open(ip, port=22, timeout=3.0):
    """生のTCPソケットで接続し、SSHバナー(先頭行)が返るかどうかを見る。
    遮断(shun)されていれば接続直後に切断されバナーが来ない。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                data = sock.recv(64)
            except (socket.timeout, OSError):
                data = b''
            return data
    except (ConnectionRefusedError, OSError):
        return b''


class TestThreatDetectionLiveShun:
    def test_repeated_rapid_connections_eventually_get_shunned(self, scanning_threat_asa):
        ip = scanning_threat_asa
        banners = [_raw_connect_and_check_open(ip) for _ in range(20)]
        got_banner = [b for b in banners if b.startswith(b'SSH-2.0')]
        got_blocked = [b for b in banners if b == b'']
        assert got_banner, '最初のうちは通常通りSSHバナーが返るはず'
        assert got_blocked, '閾値超過後は接続が即座に遮断されるはず(フェイクの検知になっていないか)'

    def test_show_threat_detection_shun_reflects_the_blocked_source(self, scanning_threat_asa):
        out = _live_cli('show threat-detection shun')
        assert 'Shunned Hosts' in out and out.strip().count('\n') >= 1, \
            f'テスト接続元がシャンされているはず: {out}'

    def test_show_threat_detection_rate_reports_triggered(self, scanning_threat_asa):
        out = _live_cli('show threat-detection rate')
        assert 'Yes' in out

    def test_clear_threat_detection_shun_allows_reconnection(self, scanning_threat_asa):
        """clearは即座にシャンを解除するが、まだ観測窓(rate-interval)の
        中に直前の大量接続が残っていれば、次の接続で即再シャンされる
        のが実機同様の正しい挙動(見た目だけclearするフェイクではない)。
        窓が流れるのを待ってから再接続を確認する。"""
        ip = scanning_threat_asa
        _live_cli('clear threat-detection shun')
        out = _live_cli('show threat-detection shun')
        assert out == 'No shunned hosts.'
        time.sleep(6)  # rate-interval(5秒)が過ぎるのを待つ
        banner = _raw_connect_and_check_open(ip)
        assert banner.startswith(b'SSH-2.0'), 'シャン解除+観測窓経過後は再び接続できるはず'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
