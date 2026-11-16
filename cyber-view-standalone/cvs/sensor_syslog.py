"""
他社のセキュリティ製品の syslog を「外部センサー」として読む — NIRVANA-R の syslog 連携のエッセンス

UTM・次世代 FW・IDS が syslog で出すアラートを、メーカーごとの書式から読み取り、
{product, src, dst, name, severity, detail} にする。ニルヴァーナはこれを警報(花びら SENSOR)にし、
画面の該当箇所に出す。

対応している書式:
- FortiGate(FortiOS): key=value 形式の `type="utm" subtype="ips"`(と anomaly / virus / webfilter の block)
- Cisco ASA: %ASA-4-733101(Host is attacking)/ %ASA-4-733100(drop rate exceeded)/ %ASA-4-4000nn(IPS シグネチャ)
- Snort / Suricata(fast 形式): `[1:2010935:3] ET SCAN ... [Priority: 2] {TCP} 1.2.3.4:123 -> 5.6.7.8:22`
- Palo Alto(CSV の THREAT ログ): 4列目が THREAT。送信元・宛先は 8・9列目

標準ライブラリだけ(ラボ版・スタンドアロン版で同じファイルを使う)。
"""

import csv
import re

IP = r'\d{1,3}(?:\.\d{1,3}){3}'
_KV = re.compile(r'(\w+)=("([^"]*)"|\S+)')


def _sev(x, default='medium'):
    x = str(x or '').lower()
    if x in ('critical', 'high', 'alert', 'emergency', '1'):
        return 'high'
    if x in ('medium', 'warning', '2'):
        return 'medium'
    if x in ('low', 'info', 'information', 'notice', '3', '4'):
        return 'low'
    return default


def parse(line):
    """1行 → dict(セキュリティ製品のアラートでなければ None)"""
    if not line:
        return None
    # FortiGate
    if 'subtype=' in line and ('type="utm"' in line or 'type=utm' in line):
        kv = {m.group(1): (m.group(3) if m.group(3) is not None else m.group(2)) for m in _KV.finditer(line)}
        sub = kv.get('subtype', '')
        if sub in ('ips', 'anomaly') or (sub in ('virus', 'webfilter', 'app-ctrl') and kv.get('action') in ('blocked', 'block', 'dropped')):
            name = kv.get('attack') or kv.get('virus') or kv.get('msg') or sub
            return {'product': 'FortiGate', 'src': kv.get('srcip'), 'dst': kv.get('dstip'), 'name': f'{sub}: {name}',
                    'severity': _sev(kv.get('crlevel') or kv.get('severity') or kv.get('level')),
                    'detail': f"{kv.get('srcip')} → {kv.get('dstip')}:{kv.get('dstport', '')} {name}({kv.get('action', '')})"}
    # Cisco ASA
    m = re.search(r'%ASA-(\d)-(733101|733100|4000\d\d):\s*(.*)', line)
    if m:
        sev, code, msg = m.groups()
        ips = re.findall(IP, msg)
        if code == '733101':
            src = re.search(r'Host (' + IP + r') is attacking', msg)
            return {'product': 'Cisco ASA', 'src': src.group(1) if src else (ips[0] if ips else None), 'dst': None,
                    'name': 'threat-detection: 攻撃元', 'severity': 'high', 'detail': msg[:200]}
        if code == '733100':
            return {'product': 'Cisco ASA', 'src': None, 'dst': None, 'name': 'threat-detection: しきい値超過',
                    'severity': 'medium', 'detail': msg[:200]}
        sig = re.search(r'IPS:(\d+)\s+(.+?)\s+from\s+(' + IP + r')\s+to\s+(' + IP + r')', msg)
        if sig:
            return {'product': 'Cisco ASA', 'src': sig.group(3), 'dst': sig.group(4), 'name': f'IPS {sig.group(1)} {sig.group(2)}',
                    'severity': 'high' if int(sev) <= 2 else 'medium', 'detail': msg[:200]}
    # Snort / Suricata(fast 形式)
    m = re.search(r'\[\d+:(\d+):\d+\]\s+(.+?)\s+\[\*\*\]?.*?(?:\[Priority:\s*(\d)\])?.*?\{(\w+)\}\s+(' + IP + r')(?::\d+)?\s+->\s+(' + IP + r')(?::(\d+))?', line)
    if m:
        sid, msg, prio, proto, src, dst, dport = m.groups()
        msg = msg.replace('[**]', '').strip()
        return {'product': 'Snort/Suricata', 'src': src, 'dst': dst, 'name': f'SID {sid} {msg}',
                'severity': _sev(prio, 'medium'), 'detail': f'{src} → {dst}{":" + dport if dport else ""} {proto} {msg}'}
    # Palo Alto(CSV)
    if ',THREAT,' in line:
        try:
            f = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            f = []
        i = next((k for k, x in enumerate(f) if x == 'THREAT'), None)
        if i is not None and len(f) > i + 30:
            src, dst = f[i + 4], f[i + 5]
            name = next((x for x in f[i + 25:i + 34] if '(' in x and ')' in x), 'THREAT')
            return {'product': 'Palo Alto', 'src': src, 'dst': dst, 'name': name, 'severity': 'high',
                    'detail': f'{src} → {dst} {name}'}
    return None
