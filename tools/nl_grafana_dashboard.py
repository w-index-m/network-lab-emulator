#!/usr/bin/env python3
"""
Grafanaダッシュボード自動生成 自然文操作（Qwen経由）

「Catalystの経路数の推移をグラフにして」のような自然文をQwen(Ollama)に
解釈させ、PromQLクエリを含むパネル定義(JSON)を生成させた上で、
Grafana HTTP API (POST /api/dashboards/db) で実際にダッシュボードを作成する。

Grafana Explore UIの自動操作（クリック/入力）は壊れやすいことがこのラボの
過去の検証で分かっているため、確実に描画される「dashboards/db API経由での
JSON投入」方式を採用している。Qwenは「自然文 → PromQLパネル定義」の
変換役に徹し、実際のAPI呼び出しは本ツールの決まった処理が担う。

前提: tools/prometheus_exporter.py が出す `netlab_route_count` /
`netlab_interface_up` などのメトリクスをPrometheusが既にscrapeしている
こと（tools/setup_monitoring_stack.sh setup で構築済みの想定）。

使い方:
  python tools/nl_grafana_dashboard.py "Catalystの経路数の推移をグラフにして"
  python tools/nl_grafana_dashboard.py "全装置のインターフェースUP数を表示して" --dry-run

環境変数:
  OLLAMA_URL     既定 http://localhost:11434
  OLLAMA_MODEL   既定 qwen2.5-netops
  GRAFANA_URL    既定 http://localhost:3000
  GRAFANA_USER   既定 admin
  GRAFANA_PASS   既定 admin
"""

import argparse
import json
import os
import re
import sys

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-netops")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_USER = os.getenv("GRAFANA_USER", "admin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "admin")

# tools/prometheus_exporter.py が実際にexportしているメトリクス名
# （Qwenに存在しない指標名を捏造させないよう、候補として明示的に渡す）
# 共通ラベル: device_id="<id>", hostname="<host>", type="<devtype>"
# インターフェース系メトリクスにはさらに interface="<ifname>" が付く
KNOWN_METRICS = """\
- netlab_device_up{device_id="<id>"}                       1固定(登録済み)
- netlab_device_health{device_id="<id>"}                    1=HEALTHY, 0=ATTENTION
- netlab_sys_uptime_seconds{device_id="<id>"}                稼働時間(秒)
- netlab_cpu_percent{device_id="<id>"}                       CPU使用率(%)
- netlab_route_count{device_id="<id>"}                       RIBの最良経路数
- netlab_interface_admin_status{device_id,interface}         1=up,2=down (設定上)
- netlab_interface_oper_status{device_id,interface}          1=up,2=down (実際の状態)
- netlab_interface_in_octets_total{device_id,interface}      受信バイト累積カウンタ
- netlab_interface_out_octets_total{device_id,interface}     送信バイト累積カウンタ
- netlab_interface_speed_bps{device_id,interface}             インターフェース速度(bps)
"""

EXTRACTION_SYSTEM = f"""あなたはGrafanaダッシュボードのパネル定義を生成するアシスタントです。
ユーザーの自然文指示から、PromQLクエリを含むダッシュボード定義をJSON形式のみで
出力してください。説明や前置きは一切出力せず、JSONオブジェクト1つだけを返します。

利用可能なメトリクス（これ以外の指標名を創作しないこと）:
{KNOWN_METRICS}

出力スキーマ:
{{
  "title": "ダッシュボードのタイトル（日本語可）",
  "panels": [
    {{
      "title": "パネルのタイトル",
      "expr": "PromQLクエリ（上記メトリクスのみ使用）",
      "type": "timeseries" または "stat"
    }}
  ]
}}

装置IDの候補: catalyst, nexus, cisco, asa, sir-a, sir-b, srs, apresia
ユーザーが装置名を明示していれば expr のラベルセレクタ device_id="<id>" に反映する。
明示が無ければ全装置分（ラベルセレクタなし）にする。
"""


def _extract_json(text: str) -> dict:
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        raise ValueError(f"JSONが見つかりませんでした: {text!r}")
    return json.loads(m.group(0))


def interpret(instruction: str) -> dict:
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": instruction},
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }, timeout=60.0)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    spec = _extract_json(content)

    if not spec.get("title"):
        spec["title"] = instruction[:60]
    if not spec.get("panels"):
        raise ValueError("Qwenがパネル定義を1つも生成しませんでした")
    for p in spec["panels"]:
        p.setdefault("type", "timeseries")
        if not p.get("expr"):
            raise ValueError(f"パネル '{p.get('title')}' にPromQL式がありません")
    return spec


def _grafana_auth():
    return (GRAFANA_USER, GRAFANA_PASS)


def _find_prometheus_datasource_uid() -> str:
    r = httpx.get(f"{GRAFANA_URL}/api/datasources", auth=_grafana_auth(), timeout=10.0)
    r.raise_for_status()
    for ds in r.json():
        if ds.get("type") == "prometheus":
            return ds["uid"]
    raise RuntimeError(
        "PrometheusデータソースがGrafanaに登録されていません。"
        "先に tools/setup_monitoring_stack.sh setup を実行してください。"
    )


def _build_dashboard_json(spec: dict, ds_uid: str) -> dict:
    panels = []
    for i, p in enumerate(spec["panels"]):
        panel_type = p["type"]
        panel = {
            "id": i + 1,
            "title": p["title"],
            "type": panel_type,
            "datasource": {"type": "prometheus", "uid": ds_uid},
            "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
            "targets": [{
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": p["expr"],
                "refId": "A",
            }],
            "fieldConfig": {"defaults": {}, "overrides": []},
        }
        if panel_type == "timeseries":
            panel["fieldConfig"]["defaults"] = {"custom": {"drawStyle": "line"}}
        panels.append(panel)

    return {
        "dashboard": {
            "id": None,
            "uid": None,
            "title": spec["title"],
            "panels": panels,
            "timezone": "browser",
            "schemaVersion": 39,
            "refresh": "10s",
            "time": {"from": "now-15m", "to": "now"},
        },
        "overwrite": True,
        "message": "nl_grafana_dashboard.py (Qwen経由) で自動生成",
    }


def create_dashboard(spec: dict) -> str:
    ds_uid = _find_prometheus_datasource_uid()
    payload = _build_dashboard_json(spec, ds_uid)
    r = httpx.post(f"{GRAFANA_URL}/api/dashboards/db", json=payload,
                    auth=_grafana_auth(), timeout=15.0)
    r.raise_for_status()
    result = r.json()
    return f"{GRAFANA_URL}{result.get('url', '')}"


def main():
    parser = argparse.ArgumentParser(description='Grafanaダッシュボード自動生成（Qwen経由）')
    parser.add_argument('instruction', help='自然文の指示（例: "Catalystの経路数の推移をグラフにして"）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Qwenの解釈結果のみ表示し、実際のダッシュボード作成は行わない')
    args = parser.parse_args()

    try:
        spec = interpret(args.instruction)
    except Exception as e:
        print(f'❌ 自然文の解釈に失敗しました: {e}')
        return 1

    print('\n' + '=' * 70)
    print(f'🤖 Qwen解釈結果: {spec["title"]}')
    for p in spec["panels"]:
        print(f'  - [{p["type"]}] {p["title"]}: {p["expr"]}')
    print('=' * 70)

    if args.dry_run:
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return 0

    try:
        url = create_dashboard(spec)
    except Exception as e:
        print(f'❌ Grafanaへのダッシュボード作成に失敗しました: {e}')
        return 1

    print(f'✅ ダッシュボード作成完了: {url}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
