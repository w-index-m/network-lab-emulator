#!/usr/bin/env python3
"""
AI Grafana Autopilot

人手を介さず、AI（Ollamaがあれば要約に利用、無ければルールベース）が
仮想ラボの異常（CPU高騰・インターフェースダウン・フラップ）を検知し、
Grafana Annotations API に自動でインシデントを書き込む。

Grafana Annotationsは「タイムライン上にマーカーを打つ」機能で、
ダッシュボードのどのグラフを見ていても該当時刻にマーカーが表示される。
アラートルールそのものより軽量で、"何が・いつ・なぜ起きたか"を
自動で記録するのに向いている。

使い方:
  # 監視対象のエミュレーターとGrafana APIキーを指定
  python tools/ai_grafana_autopilot.py \
      --emulator-url http://localhost:8000 \
      --grafana-url http://localhost:3000 \
      --grafana-token <Grafana Service Account Token>

  # Grafanaが無い/試したいだけの場合は --dry-run でHTTPを投げずログ表示のみ
  python tools/ai_grafana_autopilot.py --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import httpx
except ImportError:
    httpx = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

CPU_WARNING_THRESHOLD = 80
FLAP_WINDOW_SEC = 30
FLAP_THRESHOLD_CHANGES = 2


class Incident:
    def __init__(self, severity: str, device_id: str, hostname: str,
                 title: str, text: str, tags: list):
        self.severity = severity  # 'warning' | 'critical'
        self.device_id = device_id
        self.hostname = hostname
        self.title = title
        self.text = text
        self.tags = tags
        self.detected_at = time.time()

    def __repr__(self):
        return f'<Incident {self.severity} {self.hostname}: {self.title}>'


class AnomalyDetector:
    """/api/snmp/dashboard の履歴から異常を検知する（ルールベース）"""

    def __init__(self):
        # device_id -> 直近のoper_statusスナップショット履歴（フラップ検知用）
        self._if_status_history = defaultdict(list)

    def scan(self, dashboard: dict) -> list:
        incidents = []
        now = time.time()

        for d in dashboard.get('devices', []):
            device_id = d['device_id']
            hostname = d.get('hostname', device_id)

            # 1. CPU高騰
            cpu = d.get('cpu_percent')
            if cpu is not None and cpu >= CPU_WARNING_THRESHOLD:
                incidents.append(Incident(
                    severity='critical' if cpu >= 90 else 'warning',
                    device_id=device_id, hostname=hostname,
                    title=f'{hostname}: CPU使用率が高騰 ({cpu}%)',
                    text=f'CISCO-PROCESS-MIB相当のCPU使用率が {cpu}% に達しました。'
                         f'閾値({CPU_WARNING_THRESHOLD}%)を超過しています。',
                    tags=['netlab', 'cpu', d.get('type', '')],
                ))

            # 2. インターフェースダウン
            for iface in d.get('interfaces', []):
                if iface['oper_status'] != 1:
                    incidents.append(Incident(
                        severity='critical',
                        device_id=device_id, hostname=hostname,
                        title=f'{hostname}: {iface["descr"]} がダウン',
                        text=f'ifOperStatus=down（管理状態: '
                             f'{"up" if iface["admin_status"] == 1 else "down"}）',
                        tags=['netlab', 'interface-down', d.get('type', '')],
                    ))

            # 3. フラップ検知（短時間でのoper_status変化）
            key = device_id
            hist = self._if_status_history[key]
            snapshot = tuple(sorted((i['descr'], i['oper_status']) for i in d.get('interfaces', [])))
            hist.append((now, snapshot))
            self._if_status_history[key] = [h for h in hist if now - h[0] <= FLAP_WINDOW_SEC]
            changes = sum(1 for i in range(1, len(self._if_status_history[key]))
                         if self._if_status_history[key][i][1] != self._if_status_history[key][i - 1][1])
            if changes >= FLAP_THRESHOLD_CHANGES:
                incidents.append(Incident(
                    severity='warning',
                    device_id=device_id, hostname=hostname,
                    title=f'{hostname}: インターフェース状態がフラップ中',
                    text=f'直近{FLAP_WINDOW_SEC}秒で{changes}回、インターフェース状態が変化しました。',
                    tags=['netlab', 'flap', d.get('type', '')],
                ))

        return incidents


async def _ai_summarize(incidents: list) -> Optional[str]:
    """Ollamaがあれば、検知したインシデント群を1行で要約させる"""
    if httpx is None or not incidents:
        return None
    lines = [f'- [{i.severity}] {i.hostname}: {i.title}' for i in incidents]
    prompt = ('以下はネットワーク監視で検知した異常です。運用者向けに1-2文で '
              '簡潔に日本語要約してください:\n' + '\n'.join(lines))
    try:
        async_httpx = httpx.AsyncClient(timeout=15.0)
        async with async_httpx as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "options": {"temperature": 0.1},
            })
            if r.status_code == 200:
                return r.json()["message"]["content"].strip()
    except Exception:
        pass
    return None


class GrafanaClient:
    """Grafana Annotations API クライアント（人手を介さない自動投稿）"""

    def __init__(self, base_url: str, token: str, dry_run: bool = False):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.dry_run = dry_run

    def create_annotation(self, incident: Incident) -> dict:
        payload = {
            'time': int(incident.detected_at * 1000),
            'tags': incident.tags,
            'text': f'[{incident.severity.upper()}] {incident.title}\n{incident.text}',
        }
        if self.dry_run:
            print(f'  [DRY-RUN] POST /api/annotations would send: {json.dumps(payload, ensure_ascii=False)}')
            return {'dry_run': True, 'payload': payload}

        req = urllib.request.Request(
            f'{self.base_url}/api/annotations',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.token}',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {'error': f'HTTP {e.code}', 'body': e.read().decode(errors='replace')}
        except Exception as e:
            return {'error': str(e)}


def _fetch_dashboard(emulator_url: str, token: str) -> dict:
    req = urllib.request.Request(f'{emulator_url}/api/snmp/dashboard')
    if token:
        req.add_header('X-Session-Token', token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def main():
    parser = argparse.ArgumentParser(description='AI Grafana Autopilot — 異常検知+自動アノテーション投稿')
    parser.add_argument('--emulator-url', default='http://localhost:8000')
    parser.add_argument('--emulator-token', default='')
    parser.add_argument('--grafana-url', default='http://localhost:3000')
    parser.add_argument('--grafana-token', default='')
    parser.add_argument('--interval', type=int, default=10, help='監視間隔(秒)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Grafanaに実際に投稿せず、送信内容をログ表示のみ')
    args = parser.parse_args()

    if not args.dry_run and not args.grafana_token:
        print('⚠️  --grafana-token が未指定です。--dry-run で試すか、'
              'Grafana Service Account トークンを指定してください。')
        return 1

    detector = AnomalyDetector()
    grafana = GrafanaClient(args.grafana_url, args.grafana_token, dry_run=args.dry_run)
    seen_titles = {}  # title -> 最終投稿時刻（同一インシデントの連投抑制）

    print('\n' + '=' * 70)
    print('🤖 AI Grafana Autopilot')
    print('=' * 70)
    print(f'  エミュレーター: {args.emulator_url}')
    print(f'  Grafana       : {args.grafana_url}{" (DRY-RUN)" if args.dry_run else ""}')
    print(f'  監視間隔      : {args.interval}秒')
    print('=' * 70)

    try:
        while True:
            try:
                dashboard = _fetch_dashboard(args.emulator_url, args.emulator_token)
            except Exception as e:
                print(f'❌ エミュレーターへの接続失敗: {e}')
                time.sleep(args.interval)
                continue

            incidents = detector.scan(dashboard)
            now = time.time()
            new_incidents = [
                i for i in incidents
                if now - seen_titles.get(i.title, 0) > 60  # 同一インシデントは60秒抑制
            ]

            if new_incidents:
                ts = time.strftime('%H:%M:%S')
                print(f'\n[{ts}] 🔎 {len(new_incidents)}件の異常を検知')
                summary = asyncio.run(_ai_summarize(new_incidents))
                if summary:
                    print(f'  [AI要約] {summary}')
                for incident in new_incidents:
                    seen_titles[incident.title] = now
                    result = grafana.create_annotation(incident)
                    ok = 'error' not in result
                    icon = '✅' if ok else '❌'
                    print(f'  {icon} {incident}')
                    if not ok:
                        print(f'      → {result}')
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n終了します')
    return 0


if __name__ == '__main__':
    sys.exit(main())
