#!/usr/bin/env python3
"""
旧世代Cisco ISRルータ 切り分けツール

network-lab-emulatorの `cisco`（ISR4321相当、IOS-XEだがCatalyst 9300より
前の世代のルータ）に対して、複数のshowコマンドを自動実行し、CPU・メモリ・
インターフェース・ログを横断的にチェックして「問題があるか」「あるなら
何が疑わしいか」を日本語で提示する。

`tools/syslog_ai_monitor.py` / `tools/oscap_ai_advisor.py` と同じ設計:
Ollamaがあれば自然文で言い換え、無ければルールベースのテンプレートで
そのまま出す。

使い方:
  python tools/cisco_router_triage.py --device-id cisco
  python tools/cisco_router_triage.py --device-id cisco --no-ai
  python tools/cisco_router_triage.py --device-id cisco --json
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

# ── 閾値 ──────────────────────────────────────────────────
CPU_WARN = 50
CPU_CRIT = 80
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 90


@dataclass
class Finding:
    severity: str  # 'ok' | 'warning' | 'critical'
    title: str
    detail: str
    advice_ja: str = field(default='')


class EmulatorClient:
    def __init__(self, base_url: str, token: str = ''):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def cli(self, device_id: str, command: str) -> str:
        import urllib.request
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['X-Session-Token'] = self.token
        req = urllib.request.Request(
            f'{self.base_url}/api/cli',
            data=json.dumps({'device_id': device_id, 'command': command}).encode(),
            headers=headers, method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('output', '')


# ── パーサー群 ────────────────────────────────────────────
_CPU_RE = re.compile(
    r'five seconds:\s*(\d+)%/(\d+)%;\s*one minute:\s*(\d+)%;\s*five minutes:\s*(\d+)%')
_MEM_RE = re.compile(
    r'Total:\s*(\d+)MB,\s*Used:\s*(\d+)MB,\s*Free:\s*(\d+)MB')
_IFACE_LINE_RE = re.compile(
    r'^(\S+)\s+\S+\s+\S+\s+\S+\s+(up|down|administratively down)\s+(up|down)', re.M | re.I)
_ERR_COUNTER_RE = re.compile(
    r'^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', re.M)


def parse_cpu(output: str) -> Optional[dict]:
    m = _CPU_RE.search(output)
    if not m:
        return None
    return {
        'five_sec': int(m.group(1)), 'five_sec_interrupt': int(m.group(2)),
        'one_min': int(m.group(3)), 'five_min': int(m.group(4)),
    }


def parse_memory(output: str) -> Optional[dict]:
    m = _MEM_RE.search(output)
    if not m:
        return None
    total, used, free = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return {'total_mb': total, 'used_mb': used, 'free_mb': free,
            'used_pct': round(100 * used / total, 1) if total else 0}


def parse_interfaces_down(output: str) -> list:
    down = []
    for m in _IFACE_LINE_RE.finditer(output):
        iface, admin, oper = m.group(1), m.group(2), m.group(3)
        if 'down' in admin.lower() or 'down' in oper.lower():
            down.append({'interface': iface, 'admin': admin, 'oper': oper})
    return down


def parse_interface_errors(output: str) -> list:
    errored = []
    for m in _ERR_COUNTER_RE.finditer(output):
        iface = m.group(1)
        counters = [int(m.group(i)) for i in range(2, 8)]
        if any(counters):
            errored.append({'interface': iface, 'total_errors': sum(counters)})
    return errored


def parse_recent_errors(output: str) -> list:
    """show logging の中から %...-3-... 以上の重大度(0-3)のメッセージを拾う"""
    errors = []
    for line in output.splitlines():
        m = re.search(r'%\S+-([0-7])-\S+:\s*(.*)', line)
        if m and int(m.group(1)) <= 3:
            errors.append(line.strip())
    return errors


# ── 診断ロジック ──────────────────────────────────────────
def diagnose(client: EmulatorClient, device_id: str) -> list:
    findings = []

    cpu_out = client.cli(device_id, 'show processes cpu')
    cpu = parse_cpu(cpu_out)
    if cpu:
        if cpu['five_min'] >= CPU_CRIT:
            findings.append(Finding(
                'critical', 'CPU使用率が高い状態が継続',
                f"5分平均CPU使用率が{cpu['five_min']}%です(生出力: {cpu_out.strip()})。"
                f"一時的なスパイクではなく継続的な高負荷です。"))
        elif cpu['one_min'] >= CPU_WARN:
            findings.append(Finding(
                'warning', 'CPU使用率がやや高い',
                f"1分平均CPU使用率が{cpu['one_min']}%です(生出力: {cpu_out.strip()})。"))
        else:
            findings.append(Finding('ok', 'CPU使用率は正常範囲',
                                     f"5分平均{cpu['five_min']}%（生出力: {cpu_out.strip()}）"))
    else:
        findings.append(Finding('warning', 'CPU使用率を取得できず',
                                 f"'show processes cpu'の出力を解析できませんでした: {cpu_out[:200]}"))

    mem_out = client.cli(device_id, 'show memory statistics')
    mem = parse_memory(mem_out)
    if mem:
        if mem['used_pct'] >= MEM_CRIT_PCT:
            findings.append(Finding(
                'critical', 'メモリ使用率が危険域',
                f"使用率{mem['used_pct']}%（{mem['used_mb']}MB/{mem['total_mb']}MB）。"
                f"メモリリークやバッファ枯渇の可能性があります。"))
        elif mem['used_pct'] >= MEM_WARN_PCT:
            findings.append(Finding(
                'warning', 'メモリ使用率がやや高い',
                f"使用率{mem['used_pct']}%（{mem['used_mb']}MB/{mem['total_mb']}MB）"))
        else:
            findings.append(Finding('ok', 'メモリ使用率は正常範囲',
                                     f"使用率{mem['used_pct']}%（{mem['used_mb']}MB/{mem['total_mb']}MB）"))
    else:
        findings.append(Finding('warning', 'メモリ情報を取得できず',
                                 f"'show memory statistics'の出力を解析できませんでした: {mem_out[:200]}"))

    ifbrief_out = client.cli(device_id, 'show ip interface brief')
    down_ifaces = parse_interfaces_down(ifbrief_out)
    if down_ifaces:
        names = ', '.join(f"{d['interface']}({d['oper']})" for d in down_ifaces)
        findings.append(Finding('warning', 'downしているインターフェースあり',
                                 f"{len(down_ifaces)}件down: {names}"))
    else:
        findings.append(Finding('ok', '全インターフェースup', ifbrief_out.strip()[:200]))

    err_out = client.cli(device_id, 'show interfaces counters errors')
    errored_ifaces = parse_interface_errors(err_out)
    if errored_ifaces:
        names = ', '.join(f"{e['interface']}(累積{e['total_errors']})" for e in errored_ifaces)
        findings.append(Finding('warning', 'エラーカウンタが増加しているインターフェースあり',
                                 f"{names}。物理層異常（ケーブル/SFP/デュプレックス不一致）の"
                                 f"可能性があります。"))
    else:
        findings.append(Finding('ok', 'インターフェースエラーカウンタは正常', ''))

    log_out = client.cli(device_id, 'show logging')
    recent_errors = parse_recent_errors(log_out)
    if recent_errors:
        findings.append(Finding('warning', '重大度の高いログメッセージあり',
                                 '\n  '.join(recent_errors[:5])))
    else:
        findings.append(Finding('ok', '重大度の高いログメッセージなし', ''))

    return findings


_SEVERITY_ICON = {'ok': '✅', 'warning': '⚠️ ', 'critical': '🔴'}
_SEVERITY_JA = {'ok': '正常', 'warning': '注意', 'critical': '重大'}


def _template_advice(f: Finding) -> str:
    icon = _SEVERITY_ICON.get(f.severity, '')
    sev = _SEVERITY_JA.get(f.severity, f.severity)
    lines = [f'{icon} 【{sev}】{f.title}']
    if f.detail:
        lines.append(f'  {f.detail}')
    return '\n'.join(lines)


async def _ai_advice(f: Finding) -> Optional[str]:
    if httpx is None or f.severity == 'ok':
        return None
    prompt = (
        'あなたはCiscoルータの障害切り分けを支援するアシスタントです。'
        '以下の診断結果について、次に何を確認すべきか・どう対処すべきかを'
        '日本語で簡潔に(3行程度で)アドバイスしてください。\n\n'
        f'項目: {f.title}\n重要度: {f.severity}\n詳細: {f.detail}\n'
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "options": {"temperature": 0.2},
            })
            if r.status_code != 200:
                return None
            return r.json()["message"]["content"].strip()
    except Exception:
        return None


def advise(findings: list, use_ai: bool):
    import asyncio
    for f in findings:
        ai_text = asyncio.run(_ai_advice(f)) if use_ai else None
        template = _template_advice(f)
        f.advice_ja = f'{template}\n  → AI提案: {ai_text}' if ai_text else template


def main():
    parser = argparse.ArgumentParser(description='旧世代Cisco ISRルータ 切り分けツール')
    parser.add_argument('--device-id', default='cisco', help='対象デバイスID(既定: cisco)')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーターURL')
    parser.add_argument('--token', default='', help='セッショントークン')
    parser.add_argument('--no-ai', action='store_true', help='Ollamaを使わずテンプレートのみで出す')
    parser.add_argument('--json', action='store_true', help='JSON形式で出力')
    args = parser.parse_args()

    client = EmulatorClient(args.url, args.token)
    findings = diagnose(client, args.device_id)
    advise(findings, use_ai=not args.no_ai)

    if args.json:
        print(json.dumps([{
            'severity': f.severity, 'title': f.title, 'detail': f.detail,
            'advice': f.advice_ja,
        } for f in findings], ensure_ascii=False, indent=2))
        return 0

    n_crit = sum(1 for f in findings if f.severity == 'critical')
    n_warn = sum(1 for f in findings if f.severity == 'warning')
    print(f'\n🔍 {args.device_id} の切り分け結果 '
          f'(重大: {n_crit}件, 注意: {n_warn}件, 全{len(findings)}項目)\n')
    for f in findings:
        print(f.advice_ja)
        print()

    return 1 if n_crit else 0


if __name__ == '__main__':
    sys.exit(main())
