"""
tools/ai_grafana_autopilot.py テスト

- AnomalyDetector: CPU高騰/インターフェースダウン/フラップの検知ロジック
- GrafanaClient.create_annotation: 本物のGrafana互換モックHTTPサーバーに対して
  実際にPOSTし、Grafana Annotations APIの形式に沿ったペイロードが送られるかを検証
"""

import json
import sys
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.ai_grafana_autopilot import AnomalyDetector, GrafanaClient, Incident


def _dashboard(cpu=20, oper_status=1, device_id='r1', hostname='R1', dtype='cisco'):
    return {
        'devices': [{
            'device_id': device_id, 'hostname': hostname, 'type': dtype,
            'cpu_percent': cpu,
            'interfaces': [
                {'descr': 'Gi0/0', 'admin_status': 1, 'oper_status': oper_status,
                 'in_octets': 0, 'out_octets': 0},
            ],
        }]
    }


def test_detects_high_cpu():
    d = AnomalyDetector()
    incidents = d.scan(_dashboard(cpu=85))
    assert any('CPU' in i.title for i in incidents)
    assert incidents[0].severity == 'warning'


def test_detects_critical_cpu():
    d = AnomalyDetector()
    incidents = d.scan(_dashboard(cpu=95))
    cpu_incidents = [i for i in incidents if 'CPU' in i.title]
    assert cpu_incidents[0].severity == 'critical'


def test_no_incident_for_normal_cpu():
    d = AnomalyDetector()
    incidents = d.scan(_dashboard(cpu=30))
    assert not any('CPU' in i.title for i in incidents)


def test_detects_interface_down():
    d = AnomalyDetector()
    incidents = d.scan(_dashboard(oper_status=2))
    assert any('ダウン' in i.title for i in incidents)
    down_incidents = [i for i in incidents if 'ダウン' in i.title]
    assert down_incidents[0].severity == 'critical'


def test_detects_flap():
    d = AnomalyDetector()
    # 状態を素早く切り替えて閾値を超える変化回数を作る
    d.scan(_dashboard(oper_status=1))
    d.scan(_dashboard(oper_status=2))
    d.scan(_dashboard(oper_status=1))
    incidents = d.scan(_dashboard(oper_status=2))
    assert any('フラップ' in i.title for i in incidents)


# ── Grafana互換モックサーバー ──────────────────────
class _MockGrafanaHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == '/api/annotations':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            _MockGrafanaHandler.received.append({
                'body': body,
                'auth': self.headers.get('Authorization', ''),
            })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': 1, 'message': 'Annotation added'}).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_grafana():
    _MockGrafanaHandler.received = []
    server = HTTPServer(('127.0.0.1', 0), _MockGrafanaHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


def test_create_annotation_posts_expected_payload(mock_grafana):
    client = GrafanaClient(mock_grafana, token='test-token-123')
    incident = Incident(
        severity='critical', device_id='cat-core', hostname='cat-core',
        title='cat-core: CPU使用率が高騰 (92%)',
        text='CPU使用率が92%に達しました。',
        tags=['netlab', 'cpu', 'catalyst'],
    )
    result = client.create_annotation(incident)

    assert 'error' not in result
    assert len(_MockGrafanaHandler.received) == 1
    req = _MockGrafanaHandler.received[0]
    assert req['auth'] == 'Bearer test-token-123'
    assert req['body']['tags'] == ['netlab', 'cpu', 'catalyst']
    assert 'CRITICAL' in req['body']['text']
    assert 'CPU使用率が92%' in req['body']['text']
    assert isinstance(req['body']['time'], int)


def test_dry_run_does_not_send_http_request(mock_grafana):
    client = GrafanaClient(mock_grafana, token='', dry_run=True)
    incident = Incident(
        severity='warning', device_id='r1', hostname='r1',
        title='test', text='test', tags=['netlab'],
    )
    result = client.create_annotation(incident)
    assert result.get('dry_run') is True
    assert len(_MockGrafanaHandler.received) == 0


def test_invalid_token_returns_error_not_exception(mock_grafana):
    """モックサーバーが落ちている場合でも例外を投げずエラー辞書を返す"""
    client = GrafanaClient('http://127.0.0.1:1', token='x')  # 接続できないポート
    incident = Incident(severity='warning', device_id='r1', hostname='r1',
                        title='t', text='t', tags=[])
    result = client.create_annotation(incident)
    assert 'error' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
