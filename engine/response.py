"""
警報からの対処(遮断・隔離)— NIRVANA改 の自動防御 / NIRVANA-R × SES の遮断・隔離のエッセンス

警報の六角形から「この相手をどう止めるか」の候補を出し、選ぶと本物の CLI コマンドを装置に入れる。
入れたコマンドと、元に戻すコマンドを記録しておき、解除できる。自動対処のルール(この種類の警報が出たら、
この対処を自動で行う)も持てる。

候補の作り方(装置やトポロジーを知っている app.py 側の `_response_plan` が組み立てる):
- 疑わしいのがラボの装置 → 隣の装置の、その装置に向いたインタフェースを shutdown(隔離)
- 疑わしいのが装置でないアドレス(外部・本物のクライアント)
  - 管理アクセスの遮断: line vty に access-class(標準 ACL で deny host)→ 本物の SSH / Telnet の接続を即切断
  - ブラックホール: ルータに `ip route <ip> 255.255.255.255 Null0` → そのアドレスとの通信を捨てる
"""

import threading
import time

ACL_NAME = 'NIRVANA-BLOCK'
# 自動対処に選べる種類
AUTO_ACTIONS = ('isolate', 'vty_block', 'blackhole')


class ResponseStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.records = []                # {id, t, alert_seq, rule, kind, label, target, steps:[(dev, cmd)], undo:[(dev, cmd)], status, auto, outputs}
        self.auto_rules = []             # {rule, action}
        self._seq = 0
        self._acl_seq = {}               # dev -> 次の deny の行番号
        self.handled = set()             # (rule, src, kind) 自動対処済み
        self.last_alert_seq = 0

    def next_acl_seq(self, dev):
        with self._lock:
            n = self._acl_seq.get(dev, 0) + 1
            self._acl_seq[dev] = n
            return n

    def add(self, rec):
        with self._lock:
            self._seq += 1
            rec = {**rec, 'id': self._seq, 't': time.time(), 'status': 'running'}
            self.records.append(rec)
            if len(self.records) > 500:
                self.records = self.records[-500:]
            return rec

    def get(self, rid):
        with self._lock:
            return next((r for r in self.records if r['id'] == rid), None)

    def active(self):
        with self._lock:
            return [r for r in self.records if r['status'] == 'active']

    def set_auto(self, rules):
        clean = []
        for r in rules or []:
            if not isinstance(r, dict) or r.get('action') not in AUTO_ACTIONS or not r.get('rule'):
                raise ValueError(f'自動対処のルールが不正です: {r}')
            clean.append({'rule': str(r['rule']), 'action': r['action']})
        with self._lock:
            self.auto_rules = clean

    def auto_for(self, rule):
        with self._lock:
            return [r['action'] for r in self.auto_rules if r['rule'] in (rule, '*')]

    def snapshot(self):
        with self._lock:
            recs = list(self.records[-60:])
            active = [r for r in self.records if r['status'] == 'active']
        blocked = sorted({r['target'] for r in active if r['kind'] in ('vty_block', 'blackhole')})
        isolated = sorted({r['target'] for r in active if r['kind'] == 'isolate'})
        return {'records': list(reversed(recs)), 'auto_rules': list(self.auto_rules),
                'blocked_ips': blocked, 'isolated': isolated}


store = ResponseStore()
