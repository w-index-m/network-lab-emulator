"""
tools/network_ontology_query.py テスト

Microsoft Fabric IQのオントロジー（自然言語→構造化クエリ→実データへの
振り分け）の発想を、このエミュレータのトポロジー/LogicMonitor Alert
データに当てはめたもの。

- nl_to_query_by_keyword: Ollamaが無い場合のフォールバック変換
- resolve_query / format_answer: 実際のAPI(/api/topology/neighbors、
  /santaba/rest/alert/alerts)を叩いて構造化クエリを実行する部分
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module
from engine.logicmonitor import compute_lmv1_signature, LM_ACCESS_ID, LM_ACCESS_KEY
import tools.network_ontology_query as onq_module
from tools.network_ontology_query import (
    nl_to_query_by_keyword, resolve_query, format_answer, loki_query_range,
    summarize_logs_via_ollama,
)

client = TestClient(app_module.app)
client.__enter__()


def _monkeypatch_urllib(monkeypatch):
    """tools/network_ontology_query.pyはurllib.request経由でHTTPを
    叩く設計なので、テストではurllib.requestをTestClient呼び出しに
    差し替える（tests/test_bulk_device_import.pyの_ClientAdapterと
    同じ考え方）。"""
    import urllib.request
    from urllib.parse import urlsplit

    class _FakeResponse:
        def __init__(self, resp):
            self._resp = resp
        def read(self):
            return self._resp.content
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    real_urlopen = urllib.request.urlopen

    def _fake_urlopen(req_or_url, timeout=None):
        if isinstance(req_or_url, str):
            url, headers, method = req_or_url, {}, 'GET'
        else:
            url = req_or_url.full_url
            headers = dict(req_or_url.headers)
            method = req_or_url.get_method()
        # "http://127.0.0.1:0" はこのエミュレータを指す目印。それ以外
        # （モックLokiサーバー等）は本物のurlopenへ素通しする
        if '127.0.0.1:0' not in url:
            return real_urlopen(req_or_url, timeout=timeout)
        parts = urlsplit(url)
        path = parts.path + (('?' + parts.query) if parts.query else '')
        resp = client.request(method, path, headers=headers)
        return _FakeResponse(resp)

    monkeypatch.setattr(urllib.request, 'urlopen', _fake_urlopen)


def _dev(id_, type_, hostname):
    client.delete(f'/api/device/{id_}')
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': hostname})


def _link(a, a_if, b, b_if):
    return client.post('/api/link', json={'a': a, 'a_if': a_if, 'b': b, 'b_if': b_if})


def _lm_post(monkeypatch, path, body):
    import json as _json
    body_str = _json.dumps(body)
    epoch = str(int(time.time() * 1000))
    sig = compute_lmv1_signature(LM_ACCESS_KEY, 'POST', epoch, body_str, path)
    r = client.post('/santaba/rest' + path, content=body_str,
                    headers={'Content-Type': 'application/json',
                             'Authorization': f'LMv1 {LM_ACCESS_ID}:{sig}:{epoch}'})
    return r.json()


class TestKeywordFallback:
    def test_critical_keyword_builds_alert_filter(self):
        q = nl_to_query_by_keyword('重大度がcriticalなアラートは？')
        assert q == {'entity': 'Alert', 'filters': {'severityLabel': 'critical'},
                    'backed_by_logs': False}

    def test_warning_keyword_builds_alert_filter(self):
        q = nl_to_query_by_keyword('警告レベルのアラートを教えて')
        assert q == {'entity': 'Alert', 'filters': {'severityLabel': 'warning'},
                    'backed_by_logs': False}

    def test_generic_alert_keyword_has_no_filter(self):
        q = nl_to_query_by_keyword('アラートが出ている装置は？')
        assert q == {'entity': 'Alert', 'filters': {}, 'backed_by_logs': False}

    def test_log_keyword_sets_backed_by_logs(self):
        q = nl_to_query_by_keyword('アラートの根拠となるログは？')
        assert q == {'entity': 'Alert', 'filters': {}, 'backed_by_logs': True}

    def test_connects_to_extracts_ascii_device_id_only(self):
        """\\wはデフォルトでひらがな等のUnicode文字にもマッチするため、
        装置IDの後の助詞まで飲み込んでしまう不具合があった。ASCIIの
        装置IDだけを正しく切り出せることを固定する。"""
        q = nl_to_query_by_keyword('core-cはどこに繋がっている？')
        assert q == {'entity': 'Device', 'relationship': 'connects_to', 'of': 'core-c'}

    def test_connects_to_with_different_particle(self):
        q = nl_to_query_by_keyword('td-aは何に接続していますか')
        assert q == {'entity': 'Device', 'relationship': 'connects_to', 'of': 'td-a'}

    def test_unrecognized_question_returns_none(self):
        assert nl_to_query_by_keyword('今日の天気は？') is None


class TestResolveAndFormat:
    def test_resolve_alert_entity_from_live_api(self, monkeypatch):
        _monkeypatch_urllib(monkeypatch)
        _dev('onq-a', 'catalyst', 'Onq-A')
        r = _lm_post(monkeypatch, '/device/devices',
                    {'name': 'onq-a-ip', 'displayName': 'Onq-A', '_device_id': 'onq-a'})
        assert 'id' in r

        client.post('/api/cli', json={'device_id': 'onq-a', 'command': 'configure terminal'})
        client.post('/api/cli', json={'device_id': 'onq-a',
                                      'command': 'interface GigabitEthernet1/0/1'})
        client.post('/api/cli', json={'device_id': 'onq-a', 'command': 'shutdown'})

        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Alert', 'filters': {'severityLabel': 'critical'}})
        assert any(a['instanceName'] == 'GigabitEthernet1/0/1' for a in results)
        assert format_answer({'entity': 'Alert'}, results).startswith(f'{len(results)}件')

    def test_resolve_connects_to_from_live_topology(self, monkeypatch):
        _monkeypatch_urllib(monkeypatch)
        _dev('onq-b', 'catalyst', 'Onq-B')
        _dev('onq-c', 'nexus', 'Onq-Core')
        _link('onq-b', 'GigabitEthernet1/0/24', 'onq-c', 'GigabitEthernet1/0/1')

        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Device', 'relationship': 'connects_to',
                                 'of': 'onq-c'})
        assert any(d['id'] == 'onq-b' for d in results)
        answer = format_answer({'entity': 'Device'}, results)
        assert 'onq-b' in answer

    def test_format_answer_for_empty_alert_result(self):
        assert format_answer({'entity': 'Alert'}, []) == '該当するAlertはありません。'

    def test_format_answer_for_empty_device_result(self):
        assert format_answer({'entity': 'Device'}, []) == '接続先が見つかりませんでした。'

    def test_format_answer_for_unknown_entity(self):
        assert format_answer({'entity': 'Bogus'}, []) == '質問を理解できませんでした。'


# ══════════════════════════════════════════════════════════
# backed_by_logs（Alert -> Loki生ログ）
#
# Alert自体はログの中身を持たず、都度LogQLでLokiに問い合わせて
# 取ってくる、というFabric IQの"コピーせずにデータ源に紐づける"に
# あたる部分。本物のLokiはこの環境から入手できないため、Loki互換の
# モックHTTPサーバー(標準ライブラリのみ)を実際に起動して検証する
# （tools/ai_grafana_autopilot.pyのGrafanaモックと同じ手法）。
# ══════════════════════════════════════════════════════════
class _MockLokiHandler(BaseHTTPRequestHandler):
    received_queries = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == '/loki/api/v1/query_range':
            qs = parse_qs(parts.query)
            _MockLokiHandler.received_queries.append(qs.get('query', [''])[0])
            body = {
                'status': 'success',
                'data': {'resultType': 'streams', 'result': [
                    {'stream': {'job': 'netlab-syslog'},
                     'values': [['1700000000000000000',
                                'Sep 19 07:58:25 Onto-Log %LINK-3-UPDOWN: '
                                'Interface GigabitEthernet1/0/1, changed state to down']]},
                ]},
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_loki():
    _MockLokiHandler.received_queries = []
    server = HTTPServer(('127.0.0.1', 0), _MockLokiHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


class TestBackedByLogs:
    def test_loki_query_range_uses_source_ip_label_and_returns_log_lines(self, mock_loki):
        lines = loki_query_range(mock_loki, '127.0.0.1', 1000.0, 2000.0)
        assert lines == ['Sep 19 07:58:25 Onto-Log %LINK-3-UPDOWN: '
                         'Interface GigabitEthernet1/0/1, changed state to down']
        assert _MockLokiHandler.received_queries == [
            '{job="netlab-syslog", source_ip="127.0.0.1"}']

    def test_resolve_query_attaches_logs_when_backed_by_logs_is_true(self, monkeypatch, mock_loki):
        _monkeypatch_urllib(monkeypatch)
        _dev('onq-log', 'catalyst', 'Onq-Log')
        r = _lm_post(monkeypatch, '/device/devices',
                    {'name': '127.0.0.1', 'displayName': 'Onq-Log', '_device_id': 'onq-log'})
        assert 'id' in r

        client.post('/api/cli', json={'device_id': 'onq-log', 'command': 'configure terminal'})
        client.post('/api/cli', json={'device_id': 'onq-log',
                                      'command': 'interface GigabitEthernet1/0/1'})
        client.post('/api/cli', json={'device_id': 'onq-log', 'command': 'shutdown'})

        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Alert', 'filters': {'severityLabel': 'critical'},
                                 'backed_by_logs': True},
                                loki_url=mock_loki)
        assert results
        assert all('logs' in a for a in results)
        assert any(a['logs'] for a in results)
        answer = format_answer({'entity': 'Alert'}, results)
        assert 'ログ:' in answer

    def test_resolve_query_without_backed_by_logs_has_no_logs_key(self, monkeypatch):
        _monkeypatch_urllib(monkeypatch)
        results = resolve_query('http://127.0.0.1:0', {'entity': 'Alert', 'filters': {}})
        assert all('logs' not in a for a in results)

    def test_backed_by_logs_without_loki_url_is_a_noop(self, monkeypatch):
        """--loki-urlを渡さなければ、backed_by_logs:trueでもLokiには
        問い合わせない（このエミュレータ以外の外部URLを勝手に叩かない）。"""
        _monkeypatch_urllib(monkeypatch)
        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Alert', 'filters': {}, 'backed_by_logs': True})
        assert all('logs' not in a for a in results)


# ══════════════════════════════════════════════════════════
# AI要約（summarize_logs_via_ollama）
#
# tools/ai_grafana_autopilot.pyの_ai_summarizeと同じベストエフォート
# パターン。Ollama互換のモックHTTPサーバーに対して実際にPOSTし、
# 正しいプロンプト（Alert情報+ログ本文）が送られること、Ollamaが
# 応答しない場合は例外を投げずNoneに落ちることを確認する。
# ══════════════════════════════════════════════════════════
class _MockOllamaHandler(BaseHTTPRequestHandler):
    received_prompts = []
    reply_content = 'テスト要約です。'

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == '/api/chat':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            _MockOllamaHandler.received_prompts.append(body['messages'][0]['content'])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(
                {'message': {'content': _MockOllamaHandler.reply_content}}).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_ollama(monkeypatch):
    _MockOllamaHandler.received_prompts = []
    _MockOllamaHandler.reply_content = 'テスト要約です。'
    server = HTTPServer(('127.0.0.1', 0), _MockOllamaHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(onq_module, 'OLLAMA_URL', f'http://127.0.0.1:{port}')
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


class TestAiSummary:
    def test_summarize_posts_alert_and_logs_to_ollama(self, mock_ollama):
        alert = {'severityLabel': 'critical', 'deviceDisplayName': 'Onto-Log',
                 'resourceTemplateName': 'Interface Status',
                 'instanceName': 'GigabitEthernet1/0/1'}
        logs = ['Sep 19 07:58:25 Onto-Log %LINK-3-UPDOWN: ... changed state to down']
        summary = summarize_logs_via_ollama(alert, logs)
        assert summary == 'テスト要約です。'
        assert len(_MockOllamaHandler.received_prompts) == 1
        prompt = _MockOllamaHandler.received_prompts[0]
        assert 'Onto-Log' in prompt
        assert 'GigabitEthernet1/0/1' in prompt
        assert logs[0] in prompt

    def test_summarize_returns_none_for_empty_logs(self, mock_ollama):
        assert summarize_logs_via_ollama({'deviceDisplayName': 'x'}, []) is None
        assert _MockOllamaHandler.received_prompts == []

    def test_summarize_returns_none_when_ollama_unreachable(self, monkeypatch):
        monkeypatch.setattr(onq_module, 'OLLAMA_URL', 'http://127.0.0.1:1')
        summary = summarize_logs_via_ollama(
            {'deviceDisplayName': 'x'}, ['some log line'])
        assert summary is None

    def test_resolve_query_attaches_ai_summary_when_requested(self, monkeypatch, mock_loki,
                                                               mock_ollama):
        _monkeypatch_urllib(monkeypatch)
        _dev('onq-sum', 'catalyst', 'Onq-Sum')
        _lm_post(monkeypatch, '/device/devices',
                 {'name': '127.0.0.1', 'displayName': 'Onq-Sum', '_device_id': 'onq-sum'})
        client.post('/api/cli', json={'device_id': 'onq-sum', 'command': 'configure terminal'})
        client.post('/api/cli', json={'device_id': 'onq-sum',
                                      'command': 'interface GigabitEthernet1/0/1'})
        client.post('/api/cli', json={'device_id': 'onq-sum', 'command': 'shutdown'})

        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Alert', 'filters': {'severityLabel': 'critical'},
                                 'backed_by_logs': True},
                                loki_url=mock_loki, summarize=True)
        assert results
        assert any(a.get('ai_summary') == 'テスト要約です。' for a in results)
        answer = format_answer({'entity': 'Alert'}, results)
        assert 'AI要約:' in answer

    def test_resolve_query_without_summarize_flag_has_no_ai_summary_call(
            self, monkeypatch, mock_loki, mock_ollama):
        _monkeypatch_urllib(monkeypatch)
        _dev('onq-nosum', 'catalyst', 'Onq-NoSum')
        _lm_post(monkeypatch, '/device/devices',
                 {'name': '127.0.0.1', 'displayName': 'Onq-NoSum', '_device_id': 'onq-nosum'})
        client.post('/api/cli', json={'device_id': 'onq-nosum', 'command': 'configure terminal'})
        client.post('/api/cli', json={'device_id': 'onq-nosum',
                                      'command': 'interface GigabitEthernet1/0/1'})
        client.post('/api/cli', json={'device_id': 'onq-nosum', 'command': 'shutdown'})

        results = resolve_query('http://127.0.0.1:0',
                                {'entity': 'Alert', 'filters': {'severityLabel': 'critical'},
                                 'backed_by_logs': True},
                                loki_url=mock_loki, summarize=False)
        assert all(a.get('ai_summary') is None for a in results)
        assert _MockOllamaHandler.received_prompts == []
