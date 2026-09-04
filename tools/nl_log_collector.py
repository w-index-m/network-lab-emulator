#!/usr/bin/env python3
"""
装置ログ採取 自然文操作（Qwen経由・エミュレーター向け）

「catalystのログ採って」のような自然文をQwen(Ollama)に解釈させ、
対象装置と採取プロファイル（採取するコマンド群）を決めさせる。

nl_route_control.py と同じ設計思想:
Qwenは「自然文 → 構造化パラメータ(JSON)」の変換役に徹し、実際の
ログ採取（/api/cliへのPOST）は既存のPython実装がそのまま担う。
Qwenが直接エミュレーターを操作するわけではない。

さらに一段安全側に倒している点として、Qwenには**実行するコマンド
文字列そのものを生成させない**。Qwenが選べるのは装置名と、事前に
定義した固定コマンド集合(PROFILES)の「プロファイル名」だけであり、
そこから実行コマンドを引く。自然文（プロンプトインジェクションを
含みうる外部入力）がそのままCLIコマンドとして実行される経路を
作らないため。

将来、実機（Catalyst等）にparamiko/telnetで接続する版に拡張する
際も、この「Qwenは選択肢を選ぶだけ」という制約はそのまま維持する
想定。

使い方:
  python tools/nl_log_collector.py "catalystのログ採って"
  python tools/nl_log_collector.py "catのSTPとインターフェースの状態を見たい"
  python tools/nl_log_collector.py "nexusのvPCまわり確認して" --dry-run

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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import httpx

from tools.routing_generator import RoutingGenerator

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-netops")
NETLAB_URL = os.getenv("NETLAB_URL", "http://localhost:8000")

KNOWN_DEVICES = ["catalyst", "cat", "catalyst-test", "nexus", "nexus-2",
                  "cisco", "isr", "asa", "sir-a", "sir-b", "sir", "srs", "apresia"]

# Qwenが選べるのはここに定義したプロファイル名だけ。
# 実行するコマンド列は自然文からではなくこの辞書から引く。
PROFILES = {
    "tech-support": [
        "show version",
        "show running-config",
        "show ip interface brief",
        "show interfaces status",
        "show vlan brief",
        "show ip route",
        "show spanning-tree",
        "show cdp neighbors",
        "show mac address-table",
        "show logging",
    ],
    "interfaces": [
        "show ip interface brief",
        "show interfaces status",
        "show interfaces trunk",
    ],
    "routing": [
        "show ip route",
        "show ip ospf neighbor",
        "show ip bgp summary",
        "show ip rip neighbor",
    ],
    "stp": [
        "show spanning-tree",
        "show spanning-tree summary",
        "show etherchannel summary",
    ],
    "vpc": [
        "show vpc",
        "show vpc brief",
        "show vpc peer-keepalive",
        "show vpc role",
    ],
    "neighbors": [
        "show cdp neighbors",
        "show lldp neighbors",
    ],
}

PROFILE_NAMES = sorted(PROFILES)

EXTRACTION_SYSTEM = f"""あなたはネットワークラボの装置ログ採取指示を解釈するパーサーです。
ユーザーの自然文指示から、ログ採取のパラメータをJSON形式のみで
出力してください。説明や前置きは一切出力せず、JSONオブジェクト1つだけを
返します。

出力スキーマ:
{{
  "devices": 対象装置IDの配列（明示されていれば文字列の配列、無ければ null）,
  "profile": 採取プロファイル名（下記から1つ、明示されていなければ null）
}}

選べる profile は次のいずれかのみ（これ以外の文字列を出力してはならない）:
{', '.join(PROFILE_NAMES)}

- tech-support: 全般的な状態確認（バージョン、設定、経路、STP、CDP等）
- interfaces: インターフェースの状態確認
- routing: ルーティングプロトコルの状態確認
- stp: スパニングツリー/EtherChannelの状態確認
- vpc: NexusのvPC状態確認
- neighbors: CDP/LLDPネイバー確認

重要な制約:
- 実行する具体的なCLIコマンドを自分で生成してはならない。profile名を
  選ぶことに徹する。
- 装置名が明示されていなければ devices は null にする（推測しない）。
- 対象装置の候補: {', '.join(KNOWN_DEVICES)}

例: 「catalystのログ採って」→
{{"devices": ["catalyst"], "profile": "tech-support"}}
例: 「catのSTPとインターフェースの状態を見たい」→
{{"devices": ["cat"], "profile": "stp"}}
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

    if not params.get("devices"):
        raise ValueError(
            "対象装置が自然文から特定できませんでした。装置名を明示してください"
            f"（対応装置: {', '.join(KNOWN_DEVICES)}）"
        )
    devices = params["devices"]
    if isinstance(devices, str):
        devices = [devices]
    unknown = [d for d in devices if d not in KNOWN_DEVICES]
    if unknown:
        raise ValueError(
            f"未知の装置 {unknown} が解釈結果に含まれています。"
            f"対応装置: {', '.join(KNOWN_DEVICES)}"
        )
    params["devices"] = devices

    profile = params.get("profile") or "tech-support"
    if profile not in PROFILES:
        raise ValueError(
            f"未知のprofile '{profile}'。選べるのは: {', '.join(PROFILE_NAMES)}"
        )
    params["profile"] = profile
    return params


def collect(gen: RoutingGenerator, device: str, profile: str) -> str:
    """1台分のログをプロファイルのコマンド列で採取し、テキストにまとめる"""
    lines = [f"# {device} — profile: {profile}",
             f"# 採取日時: {datetime.now().isoformat(timespec='seconds')}",
             ""]
    for cmd in PROFILES[profile]:
        lines.append(f"{device}# {cmd}")
        output = gen.cli(device, cmd)
        lines.append(output)
        lines.append("")
    return "\n".join(lines)


def execute(params: dict, out_dir: Path) -> int:
    gen = RoutingGenerator(base_url=NETLAB_URL)
    if not gen.check_connectivity():
        print('先に `python app.py` でエミュレーターを起動してください。')
        return 1

    devices = params["devices"]
    profile = params["profile"]

    print('\n' + '=' * 70)
    print(f'🤖 Qwen解釈結果: devices={devices} profile={profile}')
    print(f'   実行コマンド: {PROFILES[profile]}')
    print('=' * 70)

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    for device in devices:
        print(f'  採取中: {device} ({len(PROFILES[profile])}コマンド)...')
        text = collect(gen, device, profile)
        path = out_dir / f'{device}_{profile}_{ts}.log'
        path.write_text(text, encoding='utf-8')
        print(f'  ✅ 保存: {path}')
    return 0


def main():
    parser = argparse.ArgumentParser(description='装置ログ採取 自然文操作（Qwen経由）')
    parser.add_argument('instruction', help='自然文の指示（例: "catalystのログ採って"）')
    parser.add_argument('--out-dir', default='logs',
                        help='ログの保存先ディレクトリ（既定: ./logs）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Qwenの解釈結果のみ表示し、実際の採取は行わない')
    args = parser.parse_args()

    try:
        params = interpret(args.instruction)
    except Exception as e:
        print(f'❌ 自然文の解釈に失敗しました: {e}')
        return 1

    if args.dry_run:
        print(json.dumps(params, ensure_ascii=False, indent=2))
        print(f'実行コマンド: {PROFILES[params["profile"]]}')
        return 0

    return execute(params, Path(args.out_dir))


if __name__ == '__main__':
    sys.exit(main())
