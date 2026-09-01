#!/usr/bin/env python3
"""
自然言語 監視対象コントロールツール

「192.168.1.1のGi1/1を監視対象に追加して」のような自然言語の指示を、
IPアドレス・インターフェース名・アクション(追加/削除)に構造化し、
実際に仮想ラボの装置を特定して監視対象リスト（watchlist）に反映する。

パイプライン:
  自然言語コマンド
    → NLExtractor（Ollamaがあれば使用、無ければ正規表現ベースにフォールバック）
    → IPアドレスから装置を特定（各装置に `show ip interface brief` を実投入して照合）
    → watchlist（JSONファイル）に追加/削除
    → tools/ai_grafana_autopilot.py 等はこのwatchlistを参照してスコープを絞れる

使い方:
  python tools/nl_monitor_control.py "192.168.1.1のGi1/1を監視対象に追加して"
  python tools/nl_monitor_control.py "10.4.1.1のGigabitEthernet1/0/1を監視対象から外して"
  python tools/nl_monitor_control.py --list
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import httpx
except ImportError:
    httpx = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

WATCHLIST_PATH = Path(__file__).parent / 'monitor_watchlist.json'

_IPV4_RE = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
_IFACE_RE = re.compile(
    r'((?:Gi(?:gabitEthernet)?|Te(?:nGigabitEthernet)?|Fa(?:stEthernet)?|'
    r'Eth(?:ernet)?|Vlan|Loopback|lan|wan)\s*[\d./]+)', re.I)
_REMOVE_WORDS = ('外し', '削除', '除外', 'remove', 'delete')


class ParsedCommand:
    def __init__(self, ip: Optional[str], iface: Optional[str], action: str):
        self.ip = ip
        self.iface = iface
        self.action = action  # 'add' | 'remove'

    def __repr__(self):
        return f'<ParsedCommand ip={self.ip} iface={self.iface} action={self.action}>'


def _rule_based_parse(text: str) -> ParsedCommand:
    """正規表現ベースの抽出（Ollama不使用時のフォールバック）"""
    ip_m = _IPV4_RE.search(text)
    iface_m = _IFACE_RE.search(text)
    action = 'remove' if any(w in text for w in _REMOVE_WORDS) else 'add'
    return ParsedCommand(
        ip=ip_m.group(1) if ip_m else None,
        iface=iface_m.group(1).strip() if iface_m else None,
        action=action,
    )


async def _ai_parse(text: str) -> Optional[ParsedCommand]:
    """Ollamaがあれば、自然言語からJSON構造を抽出させる"""
    if httpx is None:
        return None
    prompt = (
        '次の指示から、IPアドレス・インターフェース名・操作(add=追加/remove=削除)を'
        'JSON形式のみで出力してください。他の文章は一切出力しないこと。\n'
        '形式: {"ip": "x.x.x.x", "interface": "GigabitEthernet1/0/1", "action": "add"}\n\n'
        f'指示: {text}'
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "options": {"temperature": 0.0},
            })
            if r.status_code != 200:
                return None
            content = r.json()["message"]["content"].strip()
            m = re.search(r'\{.*\}', content, re.S)
            if not m:
                return None
            obj = json.loads(m.group(0))
            return ParsedCommand(ip=obj.get('ip'), iface=obj.get('interface'),
                                 action=obj.get('action', 'add'))
    except Exception:
        return None


def parse_command(text: str, use_ai: bool) -> ParsedCommand:
    if use_ai:
        import asyncio
        result = asyncio.run(_ai_parse(text))
        if result and result.ip and result.iface:
            return result
    return _rule_based_parse(text)


class EmulatorClient:
    def __init__(self, base_url: str, token: str = ''):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _headers(self):
        h = {'Content-Type': 'application/json'}
        if self.token:
            h['X-Session-Token'] = self.token
        return h

    def cli(self, device_id: str, command: str) -> str:
        req = urllib.request.Request(
            f'{self.base_url}/api/cli',
            data=json.dumps({'device_id': device_id, 'command': command}).encode(),
            headers=self._headers(), method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('output', '')

    def list_device_ids(self) -> list:
        req = urllib.request.Request(f'{self.base_url}/api/status', headers=self._headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get('devices', [])

    def get_hostname(self, device_id: str) -> str:
        req = urllib.request.Request(f'{self.base_url}/api/snmp/dashboard', headers=self._headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        for d in data.get('devices', []):
            if d['device_id'] == device_id:
                return d.get('hostname', device_id)
        return device_id

    def find_device_by_ip(self, ip: str) -> Optional[tuple]:
        """全装置に `show ip interface brief` を投げてIPが一致する装置とIF名を特定する"""
        for device_id in self.list_device_ids():
            try:
                out = self.cli(device_id, 'show ip interface brief')
            except Exception:
                continue
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == ip:
                    return device_id, parts[0]
        return None


def load_watchlist() -> list:
    if WATCHLIST_PATH.exists():
        try:
            return json.loads(WATCHLIST_PATH.read_text(encoding='utf-8'))
        except Exception:
            return []
    return []


def save_watchlist(entries: list):
    WATCHLIST_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')


def _norm_iface(name: str) -> str:
    """短縮形/正式形の差を吸収して比較する（例: Gi1/0/1 と GigabitEthernet1/0/1）"""
    s = name.lower().replace(' ', '')
    for pref in ('gigabitethernet', 'tengigabitethernet', 'fastethernet', 'ethernet',
                'gi', 'te', 'fa', 'eth'):
        if s.startswith(pref):
            return s[len(pref):]
    return s


def add_watchlist_entry(device_id: str, hostname: str, iface: str, ip: str, note: str = '') -> bool:
    entries = load_watchlist()
    key_iface = _norm_iface(iface)
    for e in entries:
        if e['device_id'] == device_id and _norm_iface(e['interface']) == key_iface:
            return False  # 既に登録済み
    entries.append({
        'device_id': device_id, 'hostname': hostname, 'interface': iface, 'ip': ip,
        'note': note, 'added_at': time.time(),
    })
    save_watchlist(entries)
    return True


def remove_watchlist_entry(device_id: str, iface: str) -> bool:
    entries = load_watchlist()
    key_iface = _norm_iface(iface)
    new_entries = [e for e in entries
                   if not (e['device_id'] == device_id and _norm_iface(e['interface']) == key_iface)]
    removed = len(new_entries) != len(entries)
    if removed:
        save_watchlist(new_entries)
    return removed


def main():
    parser = argparse.ArgumentParser(description='自然言語 監視対象コントロールツール')
    parser.add_argument('command', nargs='?', help='自然言語の指示（例: "192.168.1.1のGi1/1を監視対象に追加して"）')
    parser.add_argument('--url', default='http://localhost:8000', help='エミュレーターURL')
    parser.add_argument('--token', default='', help='セッショントークン')
    parser.add_argument('--no-ai', action='store_true', help='Ollamaを使わず正規表現ベースのみで解析')
    parser.add_argument('--list', action='store_true', help='現在の監視対象リストを表示')
    args = parser.parse_args()

    if args.list:
        entries = load_watchlist()
        if not entries:
            print('監視対象リストは空です。')
            return 0
        print('\n📋 監視対象リスト')
        for e in entries:
            print(f'  - {e["hostname"]} ({e["device_id"]}) / {e["interface"]} / {e["ip"]}')
        return 0

    if not args.command:
        parser.print_help()
        return 1

    print(f'\n💬 指示: "{args.command}"')
    parsed = parse_command(args.command, use_ai=not args.no_ai)
    print(f'🔍 解析結果: IP={parsed.ip}, Interface={parsed.iface}, Action={parsed.action}')

    if not parsed.ip or not parsed.iface:
        print('❌ IPアドレスまたはインターフェース名を指示から抽出できませんでした。')
        return 1

    client = EmulatorClient(args.url, args.token)
    found = client.find_device_by_ip(parsed.ip)
    if not found:
        print(f'❌ IP {parsed.ip} を持つ装置がラボ内に見つかりませんでした。')
        return 1
    device_id, matched_iface_from_ip = found
    print(f'✅ 装置特定: {device_id}（{parsed.ip} は {matched_iface_from_ip}）')

    # インターフェース名は `show ip interface brief` の正式名をそのまま使う
    # （ユーザー指定のGi1/1等の短縮形と一致するか確認）
    out = client.cli(device_id, 'show ip interface brief')
    matched_iface = None
    for line in out.splitlines():
        parts = line.split()
        if parts and _norm_iface(parts[0]) == _norm_iface(parsed.iface):
            matched_iface = parts[0]
            break
    if not matched_iface:
        print(f'⚠️  指定されたインターフェース "{parsed.iface}" が {device_id} に見つかりません。'
              f'IPアドレスから見つかったインターフェースをそのまま使います。')
        matched_iface = parsed.iface

    hostname = client.get_hostname(device_id)

    if parsed.action == 'add':
        added = add_watchlist_entry(device_id, hostname, matched_iface, parsed.ip)
        if added:
            print(f'✅ 監視対象に追加しました: {device_id} / {matched_iface} / {parsed.ip}')
        else:
            print(f'ℹ️  既に監視対象に登録済みです: {device_id} / {matched_iface}')
    else:
        removed = remove_watchlist_entry(device_id, matched_iface)
        if removed:
            print(f'✅ 監視対象から削除しました: {device_id} / {matched_iface}')
        else:
            print(f'ℹ️  監視対象に見つかりませんでした: {device_id} / {matched_iface}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
