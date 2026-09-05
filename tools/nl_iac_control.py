#!/usr/bin/env python3
"""
IaC (Ansible/Chef/Puppet/Salt) 自然文操作（Qwen経由）

「Ansibleで監視スタックを構築して」のような自然文をQwen(Ollama)に
解釈させ、どのIaCツールでどの操作（適用/状態確認）を行うかを判定した上で、
実際にそのツールのコマンドを実行する。

Qwenは「自然文 → {tool, action}」の変換役に徹し、実際のコマンド実行は
本ツールの決まった処理(subprocess)が担う。Qwenが直接シェルコマンドを
生成して実行するわけではない（存在しないコマンドの捏造を防ぐため、
実行するコマンド自体はツール側に固定で持つ）。

使い方:
  python tools/nl_iac_control.py "Ansibleで監視スタックを構築して"
  python tools/nl_iac_control.py "Saltでmonitoring_stackを適用して"
  python tools/nl_iac_control.py "全部のIaCツールの状態を確認して" --dry-run

環境変数:
  OLLAMA_URL   既定 http://localhost:11434
  OLLAMA_MODEL 既定 qwen2.5-netops
"""

import argparse
import json
import os
import re
import subprocess
import sys

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-netops")

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')

# 実行するコマンドはQwenに生成させず、ここに固定で持つ
# (tools/setup_monitoring_stack.sh / docs/monitoring-stack-guide.md と同一)
IAC_COMMANDS = {
    "ansible": {
        "apply": ["ansible-playbook", "-i", "ansible/inventory.ini", "ansible/site.yml"],
        "status": ["ansible-playbook", "-i", "ansible/inventory.ini", "ansible/site.yml",
                   "--check"],
    },
    "chef": {
        "apply": ["chef-solo", "-c", "chef/solo.rb", "-j", "chef/solo.json"],
        "status": ["chef-solo", "-c", "chef/solo.rb", "-j", "chef/solo.json", "--why-run"],
    },
    "puppet": {
        "apply": ["puppet", "apply", "puppet/manifests/site.pp",
                  "--modulepath", "puppet/modules"],
        "status": ["puppet", "apply", "puppet/manifests/site.pp",
                   "--modulepath", "puppet/modules", "--noop"],
    },
    "salt": {
        "apply": ["salt-call", "--local", "-c", "salt/", "state.apply", "monitoring_stack"],
        "status": ["salt-call", "--local", "-c", "salt/", "state.apply", "monitoring_stack",
                   "test=True"],
    },
}

KNOWN_TOOLS = list(IAC_COMMANDS.keys())

EXTRACTION_SYSTEM = f"""あなたはIaCツールの操作コマンドを解釈するパーサーです。
ユーザーの自然文指示から、実行するIaCツールと操作をJSON形式のみで
出力してください。説明や前置きは一切出力せず、JSONオブジェクト1つだけを
返します。

出力スキーマ:
{{
  "tool": {KNOWN_TOOLS} のいずれか。明示が無ければ "ansible",
  "action": "apply"（構築/適用/実行） または "status"（状態確認/dry-run/差分確認のみ）,
  "all_tools": true または false（「全部の」「すべての」IaCツールを対象にする指示ならtrue）
}}

「作って」「構築して」「適用して」「実行して」→ action="apply"
「確認して」「状態を見て」「差分だけ」「dry-run」→ action="status"

重要: ユーザーの文に "Ansible" "Chef" "Puppet" "Salt" のいずれか1つの
ツール名が明示されている場合、all_tools は必ず false にすること。
"全部" "すべて" "全ツール" のように複数対象を明示的に指す言葉が
ある場合のみ all_tools を true にする。
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
    }, timeout=90.0)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    params = _extract_json(content)

    params.setdefault("tool", "ansible")
    params.setdefault("action", "apply")
    params.setdefault("all_tools", False)

    # モデルが「全部」の指示に対して tool をリストで返すことがあるため、
    # その場合は all_tools=True とみなして正規化する
    if isinstance(params["tool"], list):
        params["all_tools"] = True
        params["tool"] = params["tool"][0] if params["tool"] else "ansible"

    if params["tool"] not in KNOWN_TOOLS:
        raise ValueError(f"未知のツール '{params['tool']}'。対応: {', '.join(KNOWN_TOOLS)}")
    if params["action"] not in ("apply", "status"):
        raise ValueError(f"未知のaction '{params['action']}'")

    # 指示文に特定ツール名が1つだけ明示されている場合は、モデルの
    # all_tools判定が誤っていても強制的にそのツールのみに絞る
    # (「Ansibleで」に対しall_tools=trueを返す誤判定の防御)
    mentioned = [t for t in KNOWN_TOOLS if t in instruction.lower()]
    if len(mentioned) == 1:
        params["tool"] = mentioned[0]
        params["all_tools"] = False

    return params


def run_iac(tool: str, action: str) -> int:
    cmd = IAC_COMMANDS[tool][action]
    print(f'\n{"=" * 70}')
    print(f'🔧 {tool} ({action}): {" ".join(cmd)}')
    print('=' * 70)

    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        print(f'❌ コマンドが見つかりません: {cmd[0]} '
              f'（{tool} がこの環境にインストールされていない可能性があります）')
        return 1
    except subprocess.TimeoutExpired:
        print('❌ タイムアウトしました（600秒）')
        return 1

    print(result.stdout[-4000:])
    if result.stderr:
        print('--- stderr ---')
        print(result.stderr[-2000:])

    if result.returncode == 0:
        print(f'✅ {tool} 完了（終了コード0）')
    else:
        print(f'❌ {tool} 失敗（終了コード{result.returncode}）')
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description='IaC自然文操作（Qwen経由）')
    parser.add_argument('instruction', help='自然文の指示（例: "Ansibleで監視スタックを構築して"）')
    parser.add_argument('--dry-run', action='store_true',
                        help='Qwenの解釈結果のみ表示し、実際のコマンド実行は行わない')
    args = parser.parse_args()

    try:
        params = interpret(args.instruction)
    except Exception as e:
        print(f'❌ 自然文の解釈に失敗しました: {e}')
        return 1

    tools = KNOWN_TOOLS if params["all_tools"] else [params["tool"]]
    print(f'🤖 Qwen解釈結果: tool={"+".join(tools)} action={params["action"]}')

    if args.dry_run:
        print(json.dumps(params, ensure_ascii=False, indent=2))
        return 0

    exit_code = 0
    for tool in tools:
        rc = run_iac(tool, params["action"])
        exit_code = exit_code or rc
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
