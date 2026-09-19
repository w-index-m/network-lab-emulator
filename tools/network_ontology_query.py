#!/usr/bin/env python3
"""
ネットワーク版オントロジー質問応答（Microsoft Fabric IQのFabric IQ/
Ontologyの発想をこのエミュレータのデータに当てはめたもの）

Fabric IQは「業務データに散らばった実体・関係を1つのオントロジーとして
定義し、自然言語の質問をそれに変換(NL2Ontology)してから、実データ
（Lakehouse/Eventhouse/セマンティックモデル）に振り分けて答える」という
仕組み。ここでは同じ考え方を、このエミュレータに既にある複数のAPI
（トポロジー、LogicMonitorのAlert、Grafana Lokiの生ログ）に対して
適用する。

## オントロジー定義

    Device
      - id, hostname, type
      - connects_to -> Device[]  （CDP/LLDPで発見したリンク）
      - raises_alert -> Alert[]  （LogicMonitorに登録されたDeviceのAlert）

    Alert
      - id, severityLabel, resourceTemplateName, instanceName, detail
      - belongs_to -> Device
      - backed_by_logs -> LogLine[]  （Lokiに実際に届いた生のsyslog本文。
        Alert自体はログの中身を持たず、都度LogQLで問い合わせて取って
        くる＝Fabric IQの"コピーせずにデータ源に紐づける"のと同じ発想）

自然言語の質問は、まず`NL2Ontology`の役目を担う変換ステップ
（Ollamaがあれば使う、無ければキーワードベースの最小ルール）で
下記の構造化クエリ(JSON)に変換してから、実際にAPIへ振り分けて
実行する:

    {"entity": "Alert", "filters": {"severityLabel": "critical"}}
    {"entity": "Alert", "filters": {}, "backed_by_logs": true}
    {"entity": "Device", "relationship": "connects_to", "of": "core-c"}

使い方:
  python tools/network_ontology_query.py --emulator-url http://localhost:8000 \
      "アラートが出ている装置は？"
  python tools/network_ontology_query.py "core-cはどこに繋がっている？"
  python tools/network_ontology_query.py "重大度がcriticalなアラートは？"
  python tools/network_ontology_query.py --loki-url http://localhost:3100 \
      "アラートの根拠となるログは？"
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

try:
    import httpx
except ImportError:
    httpx = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from engine.logicmonitor import compute_lmv1_signature, LM_ACCESS_ID, LM_ACCESS_KEY

ONTOLOGY_SCHEMA_PROMPT = """\
あなたはネットワーク監視データに対する質問を、下記のオントロジーに
沿った構造化クエリ(JSON1個だけ)に変換するアシスタントです。

エンティティ:
  Device: id, hostname, type
    - connects_to -> Device[]
    - raises_alert -> Alert[]
  Alert: id, severityLabel(critical/warning), resourceTemplateName, instanceName, detail
    - belongs_to -> Device
    - backed_by_logs -> LogLine[]  （Lokiに実際に届いた生のsyslog本文。
      "コピーせずにデータ源に紐づける"というFabric IQのData Binding
      と同じ考え方で、Alert自体はログの中身を持たず、都度Lokiに
      LogQLで問い合わせて取ってくる）

出力できるJSONの形は次の3種類のみです:
  {"entity": "Alert", "filters": {"severityLabel": "critical"}}
  {"entity": "Alert", "filters": {}, "backed_by_logs": true}
  {"entity": "Device", "relationship": "connects_to", "of": "<device_id>"}

JSON以外は一切出力しないこと。質問: """

# Alertの前後どれだけの範囲をLoki側の"根拠ログ"とみなすか(秒)
_LOG_EVIDENCE_WINDOW_BEFORE_SEC = 120
_LOG_EVIDENCE_WINDOW_AFTER_SEC = 60


def _http_get(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=5) as r:
        return json.loads(r.read())


def _lm_get(base_url, path):
    epoch = str(int(time.time() * 1000))
    sig = compute_lmv1_signature(LM_ACCESS_KEY, 'GET', epoch, '', path)
    req = urllib.request.Request(
        base_url + '/santaba/rest' + path,
        headers={'Authorization': f'LMv1 {LM_ACCESS_ID}:{sig}:{epoch}'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def loki_query_range(loki_url: str, source_ip: str, start_epoch: float,
                     end_epoch: float, limit: int = 5) -> list[str]:
    """LogQLで`{job="netlab-syslog", source_ip="<ip>"}`を問い合わせ、
    ログ本文だけのリストを返す（新しい順）。
    tools/syslog_to_loki.pyが実際に投入するラベルに合わせている。"""
    import urllib.parse
    query = f'{{job="netlab-syslog", source_ip="{source_ip}"}}'
    qs = urllib.parse.urlencode({
        'query': query,
        'start': str(int(start_epoch * 1e9)),
        'end': str(int(end_epoch * 1e9)),
        'limit': str(limit),
        'direction': 'backward',
    })
    with urllib.request.urlopen(f'{loki_url}/loki/api/v1/query_range?{qs}', timeout=5) as r:
        data = json.loads(r.read())
    lines = []
    for stream in data.get('data', {}).get('result', []):
        for _ts, line in stream.get('values', []):
            lines.append(line)
    return lines


def _attach_log_evidence(base_url: str, loki_url: str, alerts: list[dict]) -> None:
    """各AlertのdeviceIdからLogicMonitor Deviceの`name`(=監視対象IP)を
    引き、その前後のsyslogをLokiから取ってきて`alert['logs']`に詰める。
    Device未紐付け、あるいはLokiが応答しない場合は空リストのまま。"""
    try:
        devices = {d['id']: d for d in _lm_get(base_url, '/device/devices')['items']}
    except Exception:
        devices = {}
    for a in alerts:
        a['logs'] = []
        device = devices.get(a.get('deviceId'))
        if device is None:
            continue
        source_ip = device.get('name')
        start_epoch = a['startEpoch'] - _LOG_EVIDENCE_WINDOW_BEFORE_SEC
        end_epoch = a['startEpoch'] + _LOG_EVIDENCE_WINDOW_AFTER_SEC
        try:
            a['logs'] = loki_query_range(loki_url, source_ip, start_epoch, end_epoch)
        except Exception:
            pass


# ══════════════════════════════════════════
# NL2Ontology（自然言語 → 構造化クエリ）
# ══════════════════════════════════════════

def nl_to_query_by_keyword(question: str) -> dict | None:
    """Ollamaが無い/失敗した場合のフォールバック。よくある聞き方の
    パターンだけをキーワードでマッチさせる最小限のルールベース。"""
    q = question.strip()
    # 「ログ」「根拠」「証拠」があれば、Alertに紐づくLokiの生ログも
    # 一緒に取ってくる(backed_by_logs)
    wants_logs = any(w in q for w in ('ログ', '根拠', '証拠'))
    if 'critical' in q.lower() or '重大' in q or '緊急' in q:
        return {'entity': 'Alert', 'filters': {'severityLabel': 'critical'},
                'backed_by_logs': wants_logs}
    if 'warning' in q.lower() or '警告' in q:
        return {'entity': 'Alert', 'filters': {'severityLabel': 'warning'},
                'backed_by_logs': wants_logs}
    if 'アラート' in q or 'alert' in q.lower():
        return {'entity': 'Alert', 'filters': {}, 'backed_by_logs': wants_logs}
    # \w はデフォルトでUnicode文字（ひらがな等）にもマッチしてしまい、
    # 装置IDの後の助詞まで飲み込んでしまうため、装置IDはASCII英数字・
    # ハイフン・ドットだけに限定する
    m = re.search(r'([A-Za-z0-9.\-]+)\s*(?:は|が)?.*(?:繋が|接続|つなが)', q)
    if m:
        return {'entity': 'Device', 'relationship': 'connects_to', 'of': m.group(1)}
    return None


def nl_to_query_via_ollama(question: str) -> dict | None:
    if httpx is None:
        return None
    try:
        r = httpx.post(f'{OLLAMA_URL}/api/chat', timeout=10.0, json={
            'model': OLLAMA_MODEL,
            'messages': [{'role': 'user', 'content': ONTOLOGY_SCHEMA_PROMPT + question}],
            'stream': False, 'options': {'temperature': 0.0},
        })
        if r.status_code != 200:
            return None
        content = r.json()['message']['content'].strip()
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception:
        return None


def nl_to_query(question: str) -> tuple[dict | None, str]:
    """(構造化クエリ, どちらで変換したか)を返す。両方失敗ならNone。"""
    q = nl_to_query_via_ollama(question)
    if q:
        return q, 'ollama'
    q = nl_to_query_by_keyword(question)
    if q:
        return q, 'keyword-fallback'
    return None, 'none'


# ══════════════════════════════════════════
# クエリ実行（実データへの振り分け = Fabric IQのData Binding相当）
# ══════════════════════════════════════════

def resolve_query(base_url: str, query: dict, loki_url: str = None) -> list[dict]:
    entity = query.get('entity')
    if entity == 'Alert':
        alerts = _lm_get(base_url, '/alert/alerts')['items']
        filters = query.get('filters') or {}
        for k, v in filters.items():
            alerts = [a for a in alerts if a.get(k) == v]
        if query.get('backed_by_logs') and loki_url:
            _attach_log_evidence(base_url, loki_url, alerts)
        return alerts
    if entity == 'Device' and query.get('relationship') == 'connects_to':
        topo = _http_get(base_url, '/api/topology/neighbors')
        of = query.get('of')
        neighbor_ids = set()
        for e in topo['edges']:
            if e['a'] == of:
                neighbor_ids.add(e['b'])
            elif e['b'] == of:
                neighbor_ids.add(e['a'])
        devices = topo['devices']
        return [{'id': did, **devices[did]} for did in neighbor_ids if did in devices]
    return []


def format_answer(query: dict, results: list[dict]) -> str:
    entity = query.get('entity')
    if entity == 'Alert':
        if not results:
            return '該当するAlertはありません。'
        lines = []
        for a in results:
            lines.append(f'  - [{a["severityLabel"]}] {a["deviceDisplayName"]}: '
                         f'{a["resourceTemplateName"]}/{a["instanceName"]} ({a["detail"]})')
            for log in a.get('logs', []):
                lines.append(f'      ログ: {log}')
        return f'{len(results)}件のAlertが見つかりました:\n' + '\n'.join(lines)
    if entity == 'Device':
        if not results:
            return '接続先が見つかりませんでした。'
        lines = [f'  - {d["id"]} ({d.get("hostname", "?")}, {d.get("type", "?")})'
                for d in results]
        return f'{len(results)}台に接続しています:\n' + '\n'.join(lines)
    return '質問を理解できませんでした。'


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('question')
    p.add_argument('--emulator-url', default='http://localhost:8000')
    p.add_argument('--loki-url', default=None,
                   help='指定するとAlertのbacked_by_logs解決でLokiに問い合わせる')
    args = p.parse_args()

    query, method = nl_to_query(args.question)
    print(f'[NL2Ontology: {method}] {json.dumps(query, ensure_ascii=False)}')
    if query is None:
        print('質問を構造化クエリに変換できませんでした。')
        return
    results = resolve_query(args.emulator_url, query, loki_url=args.loki_url)
    print(format_answer(query, results))


if __name__ == '__main__':
    main()
