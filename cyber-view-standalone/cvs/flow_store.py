"""
NetFlow / sFlow で受けたフローを保存し、送信元・宛先・通信量でまとめる(スタンドアロン版)

設定(config.yaml の sources.flow):
  store: flows.db        # 保存先(SQLite。相対パスは設定ファイルの場所から)。書かなければ保存しない
  keep_days: 30          # これより古いフローは消す(1時間ごとに確認)

保存するもの(1フロー1行): 時刻・送り元(装置)・送信元/宛先 IP・プロトコル・ポート・バイト数・パケット数・種類(netflow9 等)。
書き込みは裏のスレッドで2秒ごとにまとめて行う(フローが多くても受け口を止めない)。標準ライブラリだけ。

まとめ方(summary の group):
  pair     送信元 → 宛先 ごと(既定)
  src      送信元ごと(誰がたくさん送っているか)
  dst      宛先ごと(誰がたくさん受けているか)
  service  プロトコル/サービスのポートごと(小さいほうのポートをサービスとみなす。例 tcp/443)
"""
import csv
import io
import os
import sqlite3
import threading
import time

GROUPS = {
    'pair': ('src, dst', ['src', 'dst']),
    'src': ('src', ['src']),
    'dst': ('dst', ['dst']),
    'service': ("proto || '/' || CASE WHEN sport < dport THEN sport ELSE dport END",
                ['service']),
}
COLUMNS = ['t', 'exporter', 'src', 'dst', 'proto', 'sport', 'dport', 'bytes', 'packets', 'via']


class FlowStore:
    def __init__(self, path, keep_days=30, flush_every=2.0):
        self.path = str(path)
        self.keep = float(keep_days) * 86400
        self.flush_every = float(flush_every)
        self._buf, self._lock = [], threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_prune = 0.0
        self.saved = 0
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS flows (t REAL, exporter TEXT, src TEXT, dst TEXT, proto TEXT, '
                       'sport INTEGER, dport INTEGER, bytes INTEGER, packets INTEGER, via TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS flows_t ON flows (t)')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        return db

    # ── 書き込み ──
    def add(self, fl):
        row = (float(fl.get('t') or time.time()), fl.get('exporter', ''), fl['src'], fl['dst'], str(fl.get('proto', '')),
               int(fl.get('sport') or 0), int(fl.get('dport') or 0), int(fl.get('bytes') or 0),
               int(fl.get('packets') or 0), fl.get('via', ''))
        with self._lock:
            self._buf.append(row)

    def flush(self, now=None):
        with self._lock:
            rows, self._buf = self._buf, []
        now = time.time() if now is None else now
        with self._db() as db:
            if rows:
                db.executemany('INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
                self.saved += len(rows)
            if now - self._last_prune > 3600:
                db.execute('DELETE FROM flows WHERE t < ?', (now - self.keep,))
                self._last_prune = now
        return len(rows)

    def start(self):
        def loop():
            while not self._stop.wait(self.flush_every):
                try:
                    self.flush()
                except sqlite3.Error:
                    pass          # ディスクが一時的に使えなくても受け口は止めない(次の回にまとめて書く)
        self._thread = threading.Thread(target=loop, name='flow-store', daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.flush()
        except sqlite3.Error:
            pass

    # ── 読み出し ──
    def summary(self, since, until=None, group='pair', limit=50, ip=None):
        """[{src, dst(または service), bytes, packets, flows, first, last}] を通信量の多い順に。合計も返す"""
        if group not in GROUPS:
            raise ValueError(f'group は {", ".join(GROUPS)} のどれか')
        expr, keys = GROUPS[group]
        until = time.time() if until is None else until
        where, args = 't >= ? AND t <= ?', [since, until]
        if ip:
            where += ' AND (src = ? OR dst = ?)'
            args += [ip, ip]
        self.flush()
        with self._db() as db:
            rows = db.execute(f'SELECT {expr}, SUM(bytes), SUM(packets), COUNT(*), MIN(t), MAX(t) FROM flows '
                              f'WHERE {where} GROUP BY {expr} ORDER BY SUM(bytes) DESC LIMIT ?',
                              args + [int(limit)]).fetchall()
            tot = db.execute(f'SELECT COALESCE(SUM(bytes),0), COALESCE(SUM(packets),0), COUNT(*) FROM flows '
                             f'WHERE {where}', args).fetchone()
        n = len(keys)
        out = [dict(zip(keys, r[:n]), bytes=r[n], packets=r[n + 1], flows=r[n + 2], first=r[n + 3], last=r[n + 4])
               for r in rows]
        return {'group': group, 'since': since, 'until': until, 'rows': out,
                'total': {'bytes': tot[0], 'packets': tot[1], 'flows': tot[2]}}

    def export_csv(self, since, until=None, ip=None):
        """期間のフローを CSV(時刻は ISO 形式も付ける)"""
        until = time.time() if until is None else until
        where, args = 't >= ? AND t <= ?', [since, until]
        if ip:
            where += ' AND (src = ? OR dst = ?)'
            args += [ip, ip]
        self.flush()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['time'] + COLUMNS)
        with self._db() as db:
            for r in db.execute(f'SELECT {", ".join(COLUMNS)} FROM flows WHERE {where} ORDER BY t', args):
                w.writerow([time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[0]))] + list(r))
        return buf.getvalue()
