"""
tools/syslog_to_elasticsearch.py テスト

- parse_syslog: RFC3164形式のsyslogから重大度・ファシリティタグを
  正しく取り出せること
- build_bulk_payload: Elasticsearchの_bulk API形式(NDJSON)を
  正しく組み立てられること
- push_to_elasticsearch: Elasticsearch互換のモックHTTPサーバーに
  対して実際にPOSTし、_bulk形式のペイロードが届くことを実HTTP通信で
  確認する（tools/ai_grafana_autopilot.pyのGrafanaモックと同じ手法）
"""

import json
import sys
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.syslog_to_elasticsearch import (
    parse_syslog, build_bulk_payload, index_name, push_to_elasticsearch,
)


def test_parse_syslog_extracts_facility_tag_and_severity():
    data = b'<187>*Sep 19 04:20:46.906: %LINEPROTO-5-UPDOWN: Line protocol changed'
    entry = parse_syslog(data, ('10.1.1.1', 12345))
    assert entry['source_ip'] == '10.1.1.1'
    assert entry['facility_tag'] == 'LINEPROTO'
    assert entry['severity'] == 5
    assert entry['severity_name'] == 'notice'


def test_parse_syslog_falls_back_to_pri_severity_without_facility_tag():
    data = b'<14>plain message with no cisco-style facility tag'
    entry = parse_syslog(data, ('10.1.1.2', 999))
    assert entry['facility_tag'] == 'SYSLOG'
    assert entry['severity'] == 6  # 14 % 8 = 6 (info)
    assert entry['severity_name'] == 'info'


def test_parse_syslog_defaults_pri_when_missing_angle_brackets():
    entry = parse_syslog(b'no priority header here', ('10.1.1.3', 1))
    assert entry['severity_name'] == 'info'  # デフォルトPRI=14 -> severity 6


def test_index_name_uses_dot_separated_date():
    when = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert index_name('netlab-syslog', when) == 'netlab-syslog-2026.09.19'


def test_build_bulk_payload_is_valid_ndjson_with_action_and_source():
    entry = parse_syslog(b'<187>%LINK-3-UPDOWN: test message', ('10.1.1.1', 0))
    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    payload = build_bulk_payload('netlab-syslog', entry, when)
    lines = payload.decode().strip('\n').split('\n')
    assert len(lines) == 2
    action = json.loads(lines[0])
    source = json.loads(lines[1])
    assert action == {'index': {'_index': 'netlab-syslog-2026.09.19'}}
    assert source['source_ip'] == '10.1.1.1'
    assert source['facility_tag'] == 'LINK'
    assert source['message'] == '%LINK-3-UPDOWN: test message'
    assert source['@timestamp'] == '2026-09-19T00:00:00+00:00'


# ── Elasticsearch互換モックサーバー ──────────────────────
class _MockEsHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == '/_bulk':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode()
            _MockEsHandler.received.append({
                'body': body,
                'content_type': self.headers.get('Content-Type', ''),
            })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'errors': False, 'items': []}).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_es():
    _MockEsHandler.received = []
    server = HTTPServer(('127.0.0.1', 0), _MockEsHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


def test_push_to_elasticsearch_posts_ndjson_bulk_body(mock_es):
    entry = parse_syslog(b'<187>%LINK-3-UPDOWN: interface down', ('10.2.2.2', 0))
    status = push_to_elasticsearch(mock_es, 'netlab-syslog', entry)

    assert status == 200
    assert len(_MockEsHandler.received) == 1
    req = _MockEsHandler.received[0]
    assert req['content_type'] == 'application/x-ndjson'
    lines = req['body'].strip('\n').split('\n')
    assert len(lines) == 2
    action = json.loads(lines[0])
    source = json.loads(lines[1])
    assert action['index']['_index'].startswith('netlab-syslog-')
    assert source['source_ip'] == '10.2.2.2'
    assert source['message'] == '%LINK-3-UPDOWN: interface down'


def test_push_to_elasticsearch_raises_on_unreachable_server():
    entry = parse_syslog(b'<187>%LINK-3-UPDOWN: x', ('10.2.2.3', 0))
    with pytest.raises(Exception):
        push_to_elasticsearch('http://127.0.0.1:1', 'netlab-syslog', entry)
