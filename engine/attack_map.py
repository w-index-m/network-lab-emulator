"""
MITRE ATT&CK との対応と攻撃の進行度 — NIRVANA改(2023年)の横断分析機能のエッセンス

警報やログを ATT&CK の「戦術(Tactic)」と「手法(Technique)」に結びつけ、
攻撃を受けている相手(装置・組織)ごとに、ATT&CK の段階のどこまで進んだかを数える。
画面(/cyber-view の ATT&CK)では、組織を青い輪、進行度を中央の橙の円柱で表し、
手法ごとの時系列を表にする。

ラボ版・スタンドアロン版・ネクサス(複数拠点の中央)の共通部品。標準ライブラリだけ。
"""

import threading
import time
from collections import deque

# ATT&CK Enterprise の戦術(並びは攻撃の進む順)
TACTICS = [
    ('TA0043', 'Reconnaissance', '偵察'),
    ('TA0042', 'Resource Development', 'リソース開発'),
    ('TA0001', 'Initial Access', '初期アクセス'),
    ('TA0002', 'Execution', '実行'),
    ('TA0003', 'Persistence', '永続化'),
    ('TA0004', 'Privilege Escalation', '権限昇格'),
    ('TA0005', 'Defense Evasion', '防御回避'),
    ('TA0006', 'Credential Access', '認証情報アクセス'),
    ('TA0007', 'Discovery', '探索'),
    ('TA0008', 'Lateral Movement', '横展開'),
    ('TA0009', 'Collection', '収集'),
    ('TA0011', 'Command and Control', 'C2'),
    ('TA0010', 'Exfiltration', '窃取'),
    ('TA0040', 'Impact', '影響'),
]
TACTIC_IDX = {t[0]: i for i, t in enumerate(TACTICS)}

TECHNIQUES = {
    'T1595': ('Active Scanning', '能動的スキャン'),
    'T1584': ('Compromise Infrastructure', '基盤の乗っ取り(踏み台探し)'),
    'T1110': ('Brute Force', '総当たり'),
    'T1078': ('Valid Accounts', '正規アカウントの悪用'),
    'T1046': ('Network Service Discovery', 'ネットワークサービスの探索'),
    'T1018': ('Remote System Discovery', 'リモートシステムの探索'),
    'T1021': ('Remote Services', 'リモートサービス'),
    'T1190': ('Exploit Public-Facing Application', '公開アプリの脆弱性の悪用'),
    'T1105': ('Ingress Tool Transfer', 'ツールの持ち込み(マルウェア)'),
    'T1095': ('Non-Application Layer Protocol', 'アプリ層以外のプロトコル'),
    'T1048': ('Exfiltration Over Alternative Protocol', '別プロトコルでの持ち出し'),
    'T1498': ('Network Denial of Service', 'ネットワークの DoS'),
}

# 警報の種類 → (戦術, 手法, 被害者はどちらか: 'dst'=宛先 / 'src'=送信元 / 'org'=組織全体)
RULE_MAP = {
    'conn_scan': [('TA0043', 'T1595', 'dst')],
    'fw_shun': [('TA0043', 'T1595', 'dst')],
    'darknet_scan': [('TA0043', 'T1595', 'org')],
    'darknet_reflection': [('TA0042', 'T1584', 'org')],
    'darknet_backscatter': [('TA0040', 'T1498', 'org')],
    'scan': [('TA0007', 'T1046', 'src')],
    'unreach_burst': [('TA0007', 'T1018', 'src')],
    'large_icmp': [('TA0011', 'T1095', 'src')],
    'flood': [('TA0040', 'T1498', 'dst')],
    # ログイン・流量から作るもの(app 側の検知)
    'login_after_fail': [('TA0001', 'T1078', 'dst')],
    'login_brute': [('TA0006', 'T1110', 'dst')],
    'exfil': [('TA0010', 'T1048', 'src')],
}


def sensor_rows(text):
    """セキュリティ製品のアラート文から手法を推定する(シグネチャ名のキーワード)"""
    t = (text or '').lower()
    if any(k in t for k in ('scan', 'sweep', 'probe', 'attacking', 'drop rate')):
        return [('TA0043', 'T1595', 'dst')]
    if any(k in t for k in ('brute', 'login', 'password', 'auth')):
        return [('TA0006', 'T1110', 'dst')]
    if any(k in t for k in ('virus', 'malware', 'trojan')):
        return [('TA0011', 'T1105', 'dst')]
    if any(k in t for k in ('dos', 'flood')):
        return [('TA0040', 'T1498', 'dst')]
    return [('TA0001', 'T1190', 'dst')]


class AttackTracker:
    def __init__(self, org='LAB', maxlen=5000):
        self._lock = threading.Lock()
        self.org = org
        self.events = deque(maxlen=maxlen)
        self.seq = 0
        self.compromised = {}            # 装置 -> 正規アカウントで入られた時刻(横展開の判定に使う)

    def record(self, rule, src, dst, detail='', now=None, victim=None, org=None):
        """警報 1件を ATT&CK の手法に結びつける。結びついた手法のイベントのリストを返す"""
        out = []
        now = time.time() if now is None else now
        rows = list(RULE_MAP.get(rule, [])) or (sensor_rows(detail) if rule == 'ext_sensor' else [])
        # 正規アカウントで入られた装置が探索を始めた → 横展開
        if rule in ('scan', 'unreach_burst', 'conn_scan') and src in self.compromised:
            rows.append(('TA0008', 'T1021', 'src'))
        for tactic, tech, who in rows:
            target = victim or {'dst': dst or src, 'src': src or dst, 'org': org or self.org}[who]
            with self._lock:
                self.seq += 1
                ev = {'seq': self.seq, 't': now, 'tactic': tactic, 'technique': tech,
                      'name': TECHNIQUES.get(tech, (tech, tech))[1], 'rule': rule,
                      'src': src, 'target': target, 'org': org or self.org, 'detail': detail}
                self.events.append(ev)
                if tech == 'T1078' and target:
                    self.compromised[target] = now
            out.append(ev)
        return out

    def snapshot(self, since=0, window=24 * 3600, now=None, extra=None):
        """extra: ほかの拠点のイベント(ネクサスの中央で、拠点ごとの輪を並べるため)"""
        now = time.time() if now is None else now
        with self._lock:
            evs = [e for e in list(self.events) + list(extra or []) if e.get('t') and now - e['t'] <= window]
            new = [e for e in evs if e['seq'] > since]
        matrix = []
        for tid, en, ja in TACTICS:
            techs = {}
            for e in evs:
                if e['tactic'] != tid:
                    continue
                x = techs.setdefault(e['technique'], {'id': e['technique'], 'name': e['name'],
                                                      'en': TECHNIQUES.get(e['technique'], ('',))[0],
                                                      'count': 0, 'first': e['t'], 'last': e['t'], 'targets': set()})
                x['count'] += 1
                x['last'] = max(x['last'], e['t'])
                x['targets'].add(e['target'])
            matrix.append({'id': tid, 'en': en, 'ja': ja,
                           'techniques': [{**v, 'targets': sorted(v['targets'])} for v in techs.values()]})
        return {'tactics': [{'id': t[0], 'en': t[1], 'ja': t[2]} for t in TACTICS], 'matrix': matrix,
                'targets': progress(evs), 'orgs': org_progress(evs), 'events': new[-300:], 'seq': self.seq}


def progress(evs):
    """相手ごとの進行度(到達した戦術と、いちばん進んだ段階)"""
    by = {}
    for e in evs:
        x = by.setdefault((e['org'], e['target']), {'org': e['org'], 'target': e['target'], 'tactics': set(), 'last': 0})
        x['tactics'].add(e['tactic'])
        x['last'] = max(x['last'], e['t'])
    out = []
    for x in by.values():
        idx = max(TACTIC_IDX[t] for t in x['tactics'])
        out.append({'org': x['org'], 'target': x['target'], 'tactics': sorted(x['tactics'], key=TACTIC_IDX.get),
                    'max': idx, 'max_id': TACTICS[idx][0], 'max_ja': TACTICS[idx][2], 'max_en': TACTICS[idx][1],
                    'last': x['last']})
    return sorted(out, key=lambda x: (-x['max'], -x['last']))


def org_progress(evs):
    """組織ごとの進行度(組織の中のいずれかの相手が到達した戦術の和)"""
    by = {}
    for e in evs:
        x = by.setdefault(e['org'], {'org': e['org'], 'tactics': set(), 'targets': set(), 'count': 0, 'last': 0})
        x['tactics'].add(e['tactic'])
        x['targets'].add(e['target'])
        x['count'] += 1
        x['last'] = max(x['last'], e['t'])
    out = []
    for x in by.values():
        idx = max(TACTIC_IDX[t] for t in x['tactics'])
        out.append({'org': x['org'], 'tactics': sorted(x['tactics'], key=TACTIC_IDX.get), 'max': idx,
                    'max_ja': TACTICS[idx][2], 'max_en': TACTICS[idx][1], 'targets': len(x['targets']),
                    'count': x['count'], 'last': x['last']})
    return sorted(out, key=lambda x: x['org'])


tracker = AttackTracker()
