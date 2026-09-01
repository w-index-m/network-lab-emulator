#!/usr/bin/env python3
"""
OpenSCAP fail項目 AI是正アドバイザー

`oscap xccdf eval` の結果（results.xml）と、元のSCAP DataStream
（title/description/rationale/severityが入っている）、および
ComplianceAsCodeが提供する公式remediationスクリプト（bash）を突き合わせ、
fail（不適合）だった項目ごとに「何が問題で」「なぜ問題で」「どう直すか」
を日本語で利用者に提示する。Ollamaがあれば自然な説明文に言い換え、
無ければ構造化テンプレートでそのまま出す（フォールバック）。

使い方:
  # 1. スキャンを実行して結果ファイルを作る
  oscap xccdf eval --profile xccdf_org.ssgproject.content_profile_cis_level1_server \
      --results results.xml ssg-ubuntu2404-ds.xml

  # 2. このツールでfail項目のアドバイスを表示
  python tools/oscap_ai_advisor.py \
      --results results.xml \
      --datastream ssg-ubuntu2404-ds.xml \
      --fix-script bash/ubuntu2404-script-cis_level1_server.sh

  # Ollamaを使わずテンプレートのみで出す
  python tools/oscap_ai_advisor.py --no-ai --results results.xml --datastream ssg-ubuntu2404-ds.xml
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

_NS = {'x': 'http://checklists.nist.gov/xccdf/1.2'}

_SEVERITY_JA = {'low': '低', 'medium': '中', 'high': '高', 'unknown': '不明'}


@dataclass
class Finding:
    rule_id: str
    title: str = ''
    severity: str = 'unknown'
    description: str = ''
    rationale: str = ''
    fix_snippet: str = ''
    advice_ja: str = field(default='')


def _strip_xhtml(text: Optional[str]) -> str:
    """XCCDFのdescription/rationaleは中にxhtmlタグを含むことがあるため除去"""
    if not text:
        return ''
    return re.sub(r'<[^>]+>', '', text).strip()


def load_fail_rule_ids(results_path: str) -> list:
    tree = ET.parse(results_path)
    root = tree.getroot()
    tr = root.find('x:TestResult', _NS)
    if tr is None:
        tr = root  # resultsファイルがTestResult単体ルートの場合もある
    fails = []
    for rr in tr.findall('x:rule-result', _NS):
        result = rr.find('x:result', _NS)
        if result is not None and result.text == 'fail':
            fails.append((rr.get('idref'), rr.get('severity', 'unknown')))
    return fails


def load_rule_metadata(datastream_path: str, rule_ids: set) -> dict:
    """DataStream内のBenchmarkから該当ruleのtitle/description/rationaleを引く"""
    tree = ET.parse(datastream_path)
    root = tree.getroot()
    meta = {}
    for rule in root.iter('{http://checklists.nist.gov/xccdf/1.2}Rule'):
        rid = rule.get('id')
        if rid not in rule_ids:
            continue
        title_el = rule.find('x:title', _NS)
        desc_el = rule.find('x:description', _NS)
        rat_el = rule.find('x:rationale', _NS)
        meta[rid] = {
            'title': title_el.text if title_el is not None else rid,
            'description': _strip_xhtml(ET.tostring(desc_el, encoding='unicode') if desc_el is not None else ''),
            'rationale': _strip_xhtml(ET.tostring(rat_el, encoding='unicode') if rat_el is not None else ''),
        }
    return meta


def load_fix_snippets(fix_script_path: Optional[str], rule_ids: set) -> dict:
    """公式remediationスクリプト(bash)からルール毎のfixブロックを抽出する"""
    if not fix_script_path or not os.path.exists(fix_script_path):
        return {}
    text = open(fix_script_path, encoding='utf-8', errors='ignore').read()
    snippets = {}
    for rid in rule_ids:
        pattern = re.compile(
            re.escape(f"BEGIN fix") + r'.*?' + re.escape(f"for '{rid}'") +
            r'.*?\n(.*?)' + re.escape(f"END fix for '{rid}'"),
            re.S,
        )
        m = pattern.search(text)
        if m:
            # ヘッダ/フッタのコメント行を除いた実コマンド部分のみ抜き出す
            body = m.group(1)
            lines = [l for l in body.split('\n')
                     if l.strip() and not l.strip().startswith('###')
                     and 'Remediating rule' not in l]
            snippets[rid] = '\n'.join(lines).strip()
    return snippets


def _template_advice(f: Finding) -> str:
    sev = _SEVERITY_JA.get(f.severity, f.severity)
    lines = [
        f'【{f.title}】（重要度: {sev}）',
        f'現状: このルール（{f.rule_id}）を満たしていません。',
    ]
    if f.rationale:
        lines.append(f'なぜ重要か: {f.rationale}')
    if f.fix_snippet:
        lines.append('推奨される対処（公式remediationスクリプトより抜粋）:')
        lines.append(f.fix_snippet)
    else:
        lines.append('推奨される対処: 自動修正スクリプトが見つかりませんでした。手動確認が必要です。')
    return '\n'.join(lines)


async def _ai_advice(f: Finding) -> Optional[str]:
    if httpx is None:
        return None
    prompt = (
        'あなたはLinuxサーバーのセキュリティ担当者向けアシスタントです。'
        '以下のOpenSCAPスキャンで検出された不適合項目について、'
        '「何が問題か」「なぜ重要か」「どう直せば良いか」を日本語で簡潔に'
        '説明してください。手順のコマンドがあれば維持して提示してください。\n\n'
        f'項目: {f.title}\n'
        f'重要度: {f.severity}\n'
        f'説明: {f.description}\n'
        f'理由: {f.rationale}\n'
        f'修正コマンド:\n{f.fix_snippet or "(見つかりませんでした)"}\n'
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


def build_findings(results_path: str, datastream_path: str,
                    fix_script_path: Optional[str]) -> list:
    fails = load_fail_rule_ids(results_path)
    rule_ids = {rid for rid, _ in fails}
    meta = load_rule_metadata(datastream_path, rule_ids)
    fixes = load_fix_snippets(fix_script_path, rule_ids)

    findings = []
    for rid, severity in fails:
        m = meta.get(rid, {})
        findings.append(Finding(
            rule_id=rid, severity=severity,
            title=m.get('title', rid),
            description=m.get('description', ''),
            rationale=m.get('rationale', ''),
            fix_snippet=fixes.get(rid, ''),
        ))
    return findings


def advise(findings: list, use_ai: bool):
    import asyncio
    for f in findings:
        advice = None
        if use_ai:
            advice = asyncio.run(_ai_advice(f))
        f.advice_ja = advice if advice else _template_advice(f)


def main():
    parser = argparse.ArgumentParser(description='OpenSCAP fail項目 AI是正アドバイザー')
    parser.add_argument('--results', required=True, help='oscap xccdf eval が出力した results.xml')
    parser.add_argument('--datastream', required=True, help='評価に使ったSCAP DataStream(ds.xml)')
    parser.add_argument('--fix-script', default=None,
                        help='ComplianceAsCode公式remediationスクリプト(bash/*.sh)。'
                             '指定すると具体的な修正コマンドを提示できる')
    parser.add_argument('--no-ai', action='store_true', help='Ollamaを使わずテンプレートのみで出す')
    parser.add_argument('--json', action='store_true', help='JSON形式で出力（他ツール連携用）')
    args = parser.parse_args()

    findings = build_findings(args.results, args.datastream, args.fix_script)
    if not findings:
        print('✅ fail項目はありませんでした。')
        return 0

    advise(findings, use_ai=not args.no_ai)

    if args.json:
        print(json.dumps([{
            'rule_id': f.rule_id, 'title': f.title, 'severity': f.severity,
            'advice': f.advice_ja,
        } for f in findings], ensure_ascii=False, indent=2))
        return 0

    print(f'\n⚠️  {len(findings)} 件の不適合項目が見つかりました。\n')
    for i, f in enumerate(findings, 1):
        print(f'━━━ [{i}/{len(findings)}] ━━━')
        print(f.advice_ja)
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
