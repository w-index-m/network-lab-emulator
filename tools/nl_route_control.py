#!/usr/bin/env python3
"""
ルートインジェクター 自然文操作（Qwen経由）

「Catalystに50経路追加して」のような自然文をQwen(Ollama)に解釈させ、
tools/routing_generator.py の RoutingGenerator を実際に呼び出す。

Qwenは「自然文 → 構造化パラメータ(JSON)」の変換役に徹し、実際の
経路注入操作（/api/cliへのPOST）は既存のPython実装がそのまま担う。
Qwenが直接エミュレーターを操作するわけではない。

使い方:
  python tools/nl_route_control.py "Catalystに50経路追加して"
  python tools/nl_route_control.py "Nexusの注入した経路を消して"
  python tools/nl_route_control.py "10.60.0.0から/24で200本、next-hopは10.9.9.2でcatalystに"

環境変数:
  OLLAMA_URL   既定 http://localhost:11434
  OLLAMA_MODEL 既定 qwen2.5-netops
  NETLAB_URL   既定 http://localhost:8000
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import httpx

from tools.routing_generator import RoutingGenerator, _generate_networks

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-netops")
NETLAB_URL = os.getenv("NETLAB_URL", "http://localhost:8000")

KNOWN_DEVICES = ["catalyst", "nexus", "cisco", "asa", "sir-a", "sir-b", "srs", "apresia"]

EXTRACTION_SYSTEM = """あなたはネットワークラボの操作コマンドを解釈するパーサーです。
ユーザーの自然文指示から、経路インジェクション操作のパラメータをJSON形式のみで
出力してください。説明や前置きは一切出力せず、JSONオブジェクト1つだけを返します。

出力スキーマ:
{
  "device": 対象装置ID（明示されていれば文字列、無ければ null）,
  "action": "inject" または "cleanup",
  "count": 経路数（明示されていれば整数、無ければ null）,
  "base_network": 開始ネットワークアドレス（明示されていれば文字列、無ければ null）,
  "prefix": プレフィックス長（明示されていれば整数、無ければ null）,
  "next_hop": ネクストホップIP（明示されていれば文字列、無ければ null）
}

重要な制約:
- ユーザーの文に明示的に書かれていない値は、絶対に推測や創作をせず null にすること。
- base_network に prefix（/24など）を含めない。prefix は別フィールドに分離する。
- 対象装置の候補: catalyst, nexus, cisco, asa, sir-a, sir-b, srs, apresia
- 「消す」「削除」「クリーンアップ」「消して」などはaction="cleanup"。
  それ以外（「追加」「投入」「配信」「注入」など）はaction="inject"。

例: 「Catalystに50経路追加して」→
{"device": "catalyst", "action": "inject", "count": 50, "base_network": null, "prefix": null, "next_hop": null}
"""


def _extract_json(text: str) -> dict:
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        raise ValueError(f"JSONが見つかりませんでした: {text!r}")
    return json.loads(m.group(0))


def interpret(instruction: str) -> dict:
    """自然文をQwenに投げてパラメータJSONを得る"""
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
    params = _extract_json(content)

    # モデルが null を返したフィールドにのみ既定値を適用する
    # (モデルが値を捏造していないことは呼び出し側で別途保証できないため、
    #  重要な数値・アドレス系は null でなければそのまま採用する)
    defaults = {
        "device": "catalyst", "action": "inject", "count": 100,
        "base_network": "10.50.0.0", "prefix": 24, "next_hop": "10.9.9.2",
    }
    for key, default in defaults.items():
        if params.get(key) is None:
            params[key] = default

    if params["device"] not in KNOWN_DEVICES:
        raise ValueError(
            f"未知の装置 '{params['device']}' が解釈結果に含まれています。"
            f"対応装置: {', '.join(KNOWN_DEVICES)}"
        )
    if params["action"] not in ("inject", "cleanup"):
        raise ValueError(f"未知のaction '{params['action']}'")
    params["count"] = int(params["count"])

    # モデルが base_network に "10.60.0.0/24" のようにprefixを混在させることが
    # あるため、混在していれば分離する（防御的サニタイズ）
    if "/" in str(params["base_network"]):
        net_part, _, prefix_part = str(params["base_network"]).partition("/")
        params["base_network"] = net_part
        if prefix_part.isdigit():
            params["prefix"] = int(prefix_part)
    params["prefix"] = int(params["prefix"])
    return params


def execute(params: dict) -> int:
    gen = RoutingGenerator(base_url=NETLAB_URL)
    if not gen.check_connectivity():
        print('先に `python app.py` でエミュレーターを起動してください。')
        return 1

    device = params["device"]
    before = gen.route_count(device)
    networks = list(_generate_networks(params["base_network"], params["prefix"], params["count"]))
    verb = '削除' if params["action"] == "cleanup" else '注入'

    print('\n' + '=' * 70)
    print(f'🤖 Qwen解釈結果: device={device} action={params["action"]} '
          f'count={params["count"]} base={params["base_network"]}/{params["prefix"]} '
          f'next_hop={params["next_hop"]}')
    print('=' * 70)
    print(f'  注入前の経路数: {before}')
    print(f'  {len(networks)}経路を{verb}中...')

    gen.cli(device, 'configure terminal')
    for network, netmask in networks:
        if params["action"] == "cleanup":
            gen.cli(device, f'no ip route {network} {netmask} {params["next_hop"]}')
        else:
            gen.cli(device, f'ip route {network} {netmask} {params["next_hop"]}')
    gen.cli(device, 'end')

    after = gen.route_count(device)
    print(f'  注入後の経路数: {after}')
    print(f'✅ 完了（差分: {(after or 0) - (before or 0):+d}）')
    return 0


def main():
    parser = argparse.ArgumentParser(description='ルートインジェクター 自然文操作（Qwen経由）')
    parser.add_argument('instruction', help='自然文の指示（例: "Catalystに50経路追加して"）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Qwenの解釈結果のみ表示し、実際の注入は行わない')
    args = parser.parse_args()

    try:
        params = interpret(args.instruction)
    except Exception as e:
        print(f'❌ 自然文の解釈に失敗しました: {e}')
        return 1

    if args.dry_run:
        print(json.dumps(params, ensure_ascii=False, indent=2))
        return 0

    return execute(params)


if __name__ == '__main__':
    sys.exit(main())
