"""
AI ログ解析(engine/log_ai.py)と /api/ai/*

外部の AI には出ない: 問い合わせ部分は差し替え(ask)か、手元で立てた Messages API の代役(公式 SDK を
ANTHROPIC_BASE_URL で向ける)で確かめる。
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import app as A  # noqa: E402
from engine import log_ai  # noqa: E402
from engine.log_ai import LogAnalyst, analyze_rules, prepare, provider_config, redact, validate  # noqa: E402

DEVS = [{'id': 'cr1', 'hostname': 'CR1', 'type': 'cisco'}, {'id': 'cr2', 'hostname': 'CR2', 'type': 'cisco'}]
ROWS = [
    {'t': 100, 'host': 'CR2', 'sev': 3, 'msg': '*Nov 17 06:04:55.300: %LINK-3-UPDOWN: Interface GigabitEthernet0/0/0, changed state to down'},
    {'t': 101, 'host': 'CR2', 'sev': 5, 'msg': '%OSPF-5-ADJCHG: Process 1, Nbr 10.240.12.1 on GigabitEthernet0/0/0 from FULL to DOWN'},
    {'t': 102, 'host': 'CR1', 'sev': 6, 'msg': 'ping ok'},
    {'t': 103, 'host': 'CR1', 'sev': 4, 'msg': '%SEC_LOGIN-4-LOGIN_FAILED: Login failed [user: admin] [Source: 198.51.100.4]'},
    {'t': 104, 'host': 'CR1', 'sev': 4, 'msg': '%SEC_LOGIN-4-LOGIN_FAILED: Login failed [user: admin] [Source: 198.51.100.4]'},
    {'t': 105, 'host': 'CR1', 'sev': 5, 'msg': 'snmp-server community s3cret RO / username bob password 0 hunter2'},
]


@pytest.fixture(scope='module')
def client():
    return TestClient(A.app)


def test_prepare_dedupes_prioritises_and_redacts():
    text, ip_map, st = prepare(ROWS, DEVS, max_lines=4, redact_ips=True)
    assert st == {'rows': 6, 'unique': 5, 'sent': 4, 'dropped': 1}
    assert '×2' in text and 'ping ok' not in text                 # 重要度の低いものから落とす
    assert 's3cret' not in text and 'hunter2' not in text and '<REDACTED>' in text
    assert '198.51.100.4' not in text and '10.240.12.1' not in text and ip_map['198.51.100.4'].startswith('IP_')
    assert '*Nov 17' not in text                                 # 時刻の接頭辞は外す
    assert text.startswith('装置一覧: CR1(cisco), CR2(cisco)')
    assert redact('key-string 7 ABC') == 'key-string 7 <REDACTED>'


def test_provider_auto_selection():
    assert provider_config({})['provider'] == 'rules'
    c = provider_config({'ANTHROPIC_API_KEY': 'x'})
    assert c == {'provider': 'claude', 'model': 'claude-opus-5-5', 'cloud': True}
    g = provider_config({'GROQ_API_KEY': 'g'})
    assert g['provider'] == 'openai' and 'groq' in g['base_url'] and g['model'] == 'llama-3.1-8b-instant'
    assert provider_config({'OLLAMA_URL': 'http://h:11434'})['cloud'] is False
    assert provider_config({'ANTHROPIC_API_KEY': 'x', 'NIRVANA_AI_PROVIDER': 'ollama'})['provider'] == 'ollama'


def test_validate_blocks_dangerous_and_restores_ips():
    raw = {'summary': 'IP_1 から総当たり', 'findings': [{'severity': 'weird', 'device': 'cr1', 'title': 't', 'cause': 'c', 'evidence': ['IP_1']}],
           'actions': [{'device': 'CR1', 'title': 'acl', 'why': '', 'mode': 'config', 'commands': ['configure terminal', 'access-list 99 deny host IP_1', 'end'], 'rollback': ['no access-list 99']},
                       {'device': 'CR1', 'title': 'boom', 'why': '', 'mode': 'exec', 'commands': ['write erase', 'reload'], 'rollback': []},
                       {'device': 'ZZ', 'title': 'x', 'why': '', 'mode': 'exec', 'commands': ['show version'], 'rollback': []},
                       {'device': 'CR2', 'title': 'look', 'why': '', 'mode': 'exec', 'commands': ['show ip ospf neighbor'], 'rollback': []}]}
    v = validate(raw, DEVS, {'198.51.100.4': 'IP_1'})
    assert v['summary'].startswith('198.51.100.4') and v['findings'][0]['severity'] == 'info' and v['findings'][0]['device_id'] == 'cr1'
    a = v['actions']
    assert a[0]['commands'] == ['access-list 99 deny host 198.51.100.4'] and a[0]['risk'] == 'caution' and a[0]['executable']
    assert a[1]['risk'] == 'blocked' and not a[1]['executable'] and a[1]['blocked'] == ['write erase', 'reload']
    assert a[2]['device_id'] is None and not a[2]['executable']
    assert a[3]['risk'] == 'read' and a[3]['executable']


def test_rules_and_fallback_when_ai_fails():
    r = analyze_rules(ROWS)
    titles = [f['title'] for f in r['findings']]
    assert 'インタフェース断' in titles and 'OSPF 隣接断' in titles and titles.count('ログイン失敗') == 1
    assert any('show ip ospf interface GigabitEthernet0/0/0' in a['commands'] for a in r['actions'])
    an = LogAnalyst()

    def boom(cfg, text):
        raise RuntimeError('quota exceeded')
    rec = an.analyze(ROWS, DEVS, env={'ANTHROPIC_API_KEY': 'x'}, ask=boom)
    assert rec['provider'] == 'rules' and 'quota' in rec['error'] and rec['findings']
    seen = {}

    def ok(cfg, text):
        seen['text'] = text
        return {'summary': 's', 'findings': [], 'actions': [{'device': 'CR1', 'title': 't', 'why': 'w', 'mode': 'exec',
                                                           'commands': ['show login failures'], 'rollback': []}]}, 'claude-opus-5-5'
    rec2 = an.analyze(ROWS, DEVS + [{'id': 'x9', 'hostname': 'UNUSED', 'type': 'cisco'}], env={'ANTHROPIC_API_KEY': 'x'}, ask=ok)
    assert rec2['provider'] == 'claude' and rec2['actions'][0]['executable'] and rec2['redacted_ips'] == 2
    assert 'UNUSED' not in seen['text'] and an.get(rec2['id']) is rec2           # ログに出た装置だけを AI に教える
    rec3 = an.analyze(ROWS, DEVS, env={})
    assert rec3['provider'] == 'rules' and 'AI が設定されていません' in rec3['error']


class _FakeMessages:
    """Messages API の代役。受け取った本文を記録し、構造化出力どおりの JSON を返す"""

    def __init__(self, answer, stop_reason='end_turn'):
        self.bodies, self.headers = [], []
        me = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                me.bodies.append(body)
                me.headers.append(dict(self.headers))
                out = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                       'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
                       'stop_reason': stop_reason, 'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
                b = json.dumps(out).encode()
                self.send_response(200)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_address[1]}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def test_claude_provider_through_official_sdk(monkeypatch):
    pytest.importorskip('anthropic')
    fake = _FakeMessages({'summary': 'CR2 の WAN 断', 'findings': [], 'actions': [
        {'device': 'CR2', 'title': '戻す', 'why': 'w', 'mode': 'config', 'commands': ['interface GigabitEthernet0/0/0', 'no shutdown'],
         'rollback': ['interface GigabitEthernet0/0/0', 'shutdown']}]})
    monkeypatch.setenv('ANTHROPIC_BASE_URL', fake.url)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')          # SDK は鍵を環境変数から読む
    rec = LogAnalyst().analyze(ROWS, DEVS, env={'ANTHROPIC_API_KEY': 'sk-ant-test'})
    assert rec['provider'] == 'claude' and rec['error'] is None and rec['actions'][0]['risk'] == 'caution', rec['error']
    b, h = fake.bodies[0], {k.lower(): v for k, v in fake.headers[0].items()}
    assert b['model'] == 'claude-opus-5-5' and b['fallbacks'] == 'default' and 'server-side-fallback-2026-07-01' in h['anthropic-beta']
    assert b['output_config']['effort'] == 'medium' and b['output_config']['format']['type'] == 'json_schema'
    assert set(b['output_config']['format']['schema']['required']) == {'summary', 'findings', 'actions'}
    assert '198.51.100.4' not in json.dumps(b, ensure_ascii=False)                # クラウドへは伏せて送る
    # 断られた場合はルールに切り替え、理由を残す
    ref = _FakeMessages({'summary': '', 'findings': [], 'actions': []}, stop_reason='refusal')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', ref.url)
    rec2 = LogAnalyst().analyze(ROWS, DEVS, env={'ANTHROPIC_API_KEY': 'sk-ant-test'})
    assert rec2['provider'] == 'rules' and 'refusal' in rec2['error']


def test_ai_api_analyze_and_apply(client, monkeypatch):
    d = f'ai-{uuid.uuid4().hex[:5]}'
    client.post('/api/device', json={'id': d, 'type': 'cisco', 'hostname': d.upper()})
    for c in ('configure terminal', 'interface GigabitEthernet0/0/1', 'ip address 10.251.77.1 255.255.255.0', 'shutdown', 'end'):
        client.post('/api/cli', json={'device_id': d, 'command': c})
    monkeypatch.setattr(log_ai, 'provider_config', lambda env=None: {'provider': 'claude', 'model': 'claude-opus-5-5', 'cloud': True})

    def ask(cfg, text):
        assert d.upper() in text and 'GigabitEthernet0/0/1' in text
        return {'summary': 'IF 断', 'findings': [{'severity': 'high', 'device': d.upper(), 'title': 'IF 断', 'cause': 'shutdown', 'evidence': []}],
                'actions': [{'device': d.upper(), 'title': '戻す', 'why': '', 'mode': 'config',
                             'commands': ['interface GigabitEthernet0/0/1', 'no shutdown'], 'rollback': ['interface GigabitEthernet0/0/1', 'shutdown']},
                            {'device': d.upper(), 'title': '再起動', 'why': '', 'mode': 'exec', 'commands': ['reload'], 'rollback': []}]}, 'claude-opus-5-5'
    monkeypatch.setitem(log_ai.ASK, 'claude', ask)
    st = client.get('/api/ai/status').json()
    assert st['provider'] == 'claude' and st['apply'] is True
    seq0 = A._flow_recorder.seq
    r = client.post('/api/ai/logs/analyze', json={'minutes': 5, 'device': d}).json()
    assert r['provider'] == 'claude' and r['actions'][0]['device_id'] == d and r['actions'][1]['risk'] == 'blocked'
    assert any(a['rule'] == 'ai_finding' and a['seq'] > seq0 for a in A._flow_recorder.alerts)
    assert client.post('/api/ai/logs/apply', json={'id': r['id'], 'action': 0}).status_code == 400          # confirm 無し
    assert client.post('/api/ai/logs/apply', json={'id': r['id'], 'action': 1, 'confirm': True}).status_code == 400   # reload
    assert client.post('/api/ai/logs/apply', json={'id': 9999, 'action': 0, 'confirm': True}).status_code == 404
    out = client.post('/api/ai/logs/apply', json={'id': r['id'], 'action': 0, 'confirm': True}).json()
    assert [o['command'] for o in out['outputs']] == ['configure terminal', 'interface GigabitEthernet0/0/1', 'no shutdown', 'end']
    brief = client.post('/api/cli', json={'device_id': d, 'command': 'show ip interface brief'}).json()['output']
    assert any('GigabitEthernet0/0/1' in ln and ln.rstrip().endswith('up') for ln in brief.splitlines())
    monkeypatch.setenv('NIRVANA_AI_APPLY', '0')
    assert client.post('/api/ai/logs/apply', json={'id': r['id'], 'action': 0, 'confirm': True}).status_code == 403
    assert client.get('/api/ai/logs/history').json()['history'][0]['id'] == r['id']


def test_page_has_ai_panel_and_new_petals():
    html = open(os.path.join(ROOT, 'static', 'cyber_view.html'), encoding='utf-8').read()
    for s in ('id="mAi"', 'id="aipanel"', '/api/ai/logs/analyze', '/api/ai/logs/apply', "AI: { label: 'AI'", "WORLD: { label: 'WORLD'",
              "ai_finding: 'AI'", "if (!SOURCES[sname]) sname = 'IDS'"):
        assert s in html, s
    time.sleep(0)
