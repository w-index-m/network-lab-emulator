#!/usr/bin/env python3
"""
Syslog受信 + 簡易AI要約モニター（Splunk風ミニ監視ツール）

engine/syslog_sender.py が実UDPで送信するsyslogパケット（RFC 3164）を
受信し、一定間隔でログをまとめてAI（Ollama、なければルールベース）が
要約する。

使い方:
  # 受信のみ・要約は60秒間隔（デフォルト）
  python tools/syslog_ai_monitor.py

  # ポート/間隔を指定
  python tools/syslog_ai_monitor.py --port 5514 --interval 30

  # 実機/エミュレーターから見せるには、装置側で
  #   syslog server <このホストのIP> 5514
  # のように、このツールのlisten先を宛先に設定する
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import httpx
except ImportError:
    httpx = None

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

SEVERITY_NAMES = {
    0: 'emergencies', 1: 'alerts', 2: 'critical', 3: 'errors',
    4: 'warnings', 5: 'notifications', 6: 'informational', 7: 'debugging',
}

# %OSPF-..., %SPANTREE-..., %RIP-... のような装置ログの「イベント種別」を
# メッセージ本文から抽出するための正規表現
_EVENT_TAG_RE = re.compile(r'%([A-Z0-9_]+)-(\d)-([A-Z0-9_]+)')

_SYSLOG_RE = re.compile(
    r'^<(?P<pri>\d+)>(?P<ts>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
    r'(?P<host>\S+)\s+(?P<msg>.*)$'
)


@dataclass
class LogEntry:
    received_at: float
    hostname: str
    facility: int
    severity: int
    message: str
    event_tag: Optional[str] = None


class SyslogStore:
    """受信したログをメモリ + JSONLファイルに保持する"""

    def __init__(self, log_path: Optional[Path] = None, max_memory: int = 5000):
        self.entries: List[LogEntry] = []
        self.max_memory = max_memory
        self.log_path = log_path
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, entry: LogEntry):
        self.entries.append(entry)
        if len(self.entries) > self.max_memory:
            self.entries = self.entries[-self.max_memory:]
        if self.log_path:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'received_at': entry.received_at,
                    'hostname': entry.hostname,
                    'facility': entry.facility,
                    'severity': entry.severity,
                    'message': entry.message,
                    'event_tag': entry.event_tag,
                }, ensure_ascii=False) + '\n')

    def since(self, ts: float) -> List[LogEntry]:
        return [e for e in self.entries if e.received_at >= ts]


def parse_syslog_packet(data: bytes) -> Optional[LogEntry]:
    """RFC 3164形式のsyslogパケットをパース"""
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return None
    m = _SYSLOG_RE.match(text)
    if not m:
        # PRIが無い/形式が違う簡易メッセージもホスト不明で受理
        return LogEntry(received_at=time.time(), hostname='unknown',
                         facility=23, severity=6, message=text.strip())
    pri = int(m.group('pri'))
    facility, severity = divmod(pri, 8)
    hostname = m.group('host')
    msg = m.group('msg')
    tag_m = _EVENT_TAG_RE.search(msg)
    event_tag = tag_m.group(0) if tag_m else None
    return LogEntry(received_at=time.time(), hostname=hostname,
                     facility=facility, severity=severity,
                     message=msg, event_tag=event_tag)


class SyslogUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, store: SyslogStore, verbose: bool):
        self.store = store
        self.verbose = verbose

    def datagram_received(self, data: bytes, addr):
        entry = parse_syslog_packet(data)
        if not entry:
            return
        self.store.add(entry)
        if self.verbose:
            sev = SEVERITY_NAMES.get(entry.severity, str(entry.severity))
            ts = datetime.fromtimestamp(entry.received_at).strftime('%H:%M:%S')
            print(f'[{ts}] {addr[0]:<15} {entry.hostname:<16} [{sev:<13}] {entry.message}')


def _rule_based_summary(entries: List[LogEntry]) -> str:
    """Ollamaが使えない場合のフォールバック要約（統計＋簡易異常検知）"""
    if not entries:
        return '(この期間、新規ログはありません)'

    by_host = Counter(e.hostname for e in entries)
    by_sev = Counter(SEVERITY_NAMES.get(e.severity, str(e.severity)) for e in entries)
    by_tag = Counter(e.event_tag for e in entries if e.event_tag)

    lines = [
        f'件数: {len(entries)}件',
        f'装置別: ' + ', '.join(f'{h}={c}' for h, c in by_host.most_common(10)),
        f'重要度別: ' + ', '.join(f'{s}={c}' for s, c in by_sev.most_common()),
    ]
    if by_tag:
        lines.append('イベント種別Top5: ' + ', '.join(f'{t}={c}' for t, c in by_tag.most_common(5)))

    # 簡易異常検知: 同一装置で同一イベントタグが短時間に頻発（フラップ）
    flap_window = defaultdict(list)
    for e in entries:
        if e.event_tag:
            flap_window[(e.hostname, e.event_tag)].append(e.received_at)
    warnings = []
    for (host, tag), times in flap_window.items():
        if len(times) >= 3:
            span = max(times) - min(times)
            warnings.append(f'⚠ {host} で {tag} が {len(times)}回発生（{span:.0f}秒間）— フラップの可能性')
    # 重要度の高いログ（error以下）を個別ピックアップ
    critical = [e for e in entries if e.severity <= 3]
    for e in critical[:5]:
        warnings.append(f'⚠ [重要] {e.hostname}: {e.message}')

    if warnings:
        lines.append('--- 注目すべき点 ---')
        lines.extend(warnings)

    return '\n'.join(lines)


async def _ai_summary(entries: List[LogEntry]) -> Optional[str]:
    """Ollamaが利用可能なら、ログをまとめてAI要約させる"""
    if httpx is None or not entries:
        return None
    lines = [f'{datetime.fromtimestamp(e.received_at).strftime("%H:%M:%S")} '
              f'{e.hostname} [{SEVERITY_NAMES.get(e.severity, e.severity)}] {e.message}'
              for e in entries[-200:]]  # 直近200件までに制限
    prompt = (
        'あなたはネットワーク監視の専門家です。以下はネットワーク機器から届いた'
        'syslogログです。異常や注目すべきイベント（インターフェース断、'
        'ルーティングフラップ、認証失敗、STPトポロジ変化など）があれば'
        '簡潔な日本語で要約してください。異常がなければ「異常なし」とだけ'
        '答えてください。\n\n' + '\n'.join(lines)
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": "簡潔に、箇条書き中心で回答してください。"},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            })
            if r.status_code == 200:
                return r.json()["message"]["content"].strip()
    except Exception as e:
        print(f'[Ollama] エラー: {e}')
    return None


async def detect_ollama() -> bool:
    if httpx is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code == 200:
                return len(r.json().get("models", [])) > 0
    except Exception:
        pass
    return False


async def summary_loop(store: SyslogStore, interval: int, use_ai: bool):
    last_ts = time.time()
    while True:
        await asyncio.sleep(interval)
        now = time.time()
        entries = store.since(last_ts)
        last_ts = now

        print('\n' + '=' * 70)
        print(f'📊 要約レポート ({datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, '
              f'直近{interval}秒)')
        print('=' * 70)

        summary = None
        if use_ai:
            summary = await _ai_summary(entries)
            if summary:
                print(f'[AI要約 / {OLLAMA_MODEL}]')
        if not summary:
            summary = _rule_based_summary(entries)
            print('[ルールベース要約]' + (' (Ollama利用不可のためフォールバック)' if use_ai else ''))
        print(summary)
        print('=' * 70)


async def main_async(args):
    log_path = Path(args.log_file) if args.log_file else None
    store = SyslogStore(log_path=log_path)

    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: SyslogUdpProtocol(store, verbose=not args.quiet),
        local_addr=(args.bind, args.port)
    )

    use_ai = await detect_ollama()
    print('\n' + '=' * 70)
    print('🧪 Syslog AI モニター')
    print('=' * 70)
    print(f'  受信待ち受け : {args.bind}:{args.port} (UDP)')
    print(f'  要約間隔     : {args.interval}秒')
    print(f'  AI要約       : {"有効 (" + OLLAMA_MODEL + ")" if use_ai else "無効（ルールベースにフォールバック）"}')
    if log_path:
        print(f'  ログ保存先   : {log_path}')
    print('  装置側の設定例: syslog server <このホストIP> ' + str(args.port))
    print('=' * 70)

    try:
        await summary_loop(store, args.interval, use_ai)
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description='Syslog受信+簡易AI要約モニター')
    parser.add_argument('--bind', default='0.0.0.0', help='待ち受けIP')
    parser.add_argument('--port', type=int, default=5514,
                        help='待ち受けUDPポート（既定5514、標準514はroot権限が必要）')
    parser.add_argument('--interval', type=int, default=60, help='要約間隔（秒）')
    parser.add_argument('--log-file', default='tools/syslog_ai_monitor.log',
                        help='受信ログの保存先（JSONL）。空文字で無効化')
    parser.add_argument('--quiet', action='store_true', help='受信ログの逐次表示を抑制')
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print('\n終了します')


if __name__ == '__main__':
    main()
