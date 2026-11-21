"""
試験の様子(/cyber-view の「試」プレートと TEST RUN パネル用)

Claude が裏で回している試験(シナリオ回帰・pytest・機種×プロトコルの総当たり など)が、
どの装置に何を当てて、通ったか落ちたかを記録する。試験側は POST /api/testrun/event で送る。

出来事の形(どれも省略可。kind と name だけは必ず入れる):
  kind     probe(装置にコマンドを当てた)/ pass / fail / skip / start(試験の開始)/ end(終了)
  suite    scenario / pytest / matrix / manual など
  run      1 回の試験の実行を見分ける ID(start で新しくなる。無ければ直近の run)
  name     チェックやテストの名前
  device   当てた装置の ID(あれば画面のその装置の上に出す)
  command  当てたコマンド(probe)
  detail   失敗の理由など(長いものは切る)

送った試験が装置を壊すことは無い(記録して画面に出すだけ)。メモリに最新 MAX_EVENTS 件だけ持つ。
"""
import time
import threading

MAX_EVENTS = 500
KINDS = ('probe', 'pass', 'fail', 'skip', 'start', 'end')
_TEXT_MAX = {'suite': 32, 'run': 64, 'name': 200, 'device': 128, 'command': 200, 'detail': 300}


class TestActivity:
    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.seq = 0
        self.runs = {}          # run → {'suite','started','ended','pass','fail','skip','probe','last'}
        self.current = None

    def reset(self):
        with self._lock:
            self.events.clear()
            self.runs.clear()
            self.seq = 0
            self.current = None

    def record(self, ev: dict, now: float = None):
        """1 件を記録して、記録した形を返す。kind / name が無い・おかしいときは ValueError"""
        if not isinstance(ev, dict):
            raise ValueError('event must be an object')
        kind = str(ev.get('kind') or '')
        if kind not in KINDS:
            raise ValueError(f'kind は {"/".join(KINDS)} のどれか')
        name = str(ev.get('name') or '').strip()
        if not name and kind not in ('start', 'end'):
            raise ValueError('name が必要')
        now = time.time() if now is None else float(now)
        out = {'kind': kind, 't': now}
        for k, lim in _TEXT_MAX.items():
            v = ev.get(k)
            if v is not None and v != '':
                out[k] = str(v)[:lim]
        out.setdefault('suite', 'manual')
        if name:
            out['name'] = name[:_TEXT_MAX['name']]
        with self._lock:
            if kind == 'start' or (out.get('run') and out['run'] not in self.runs) or self.current is None:
                run = out.get('run') or f"{out['suite']}-{int(now)}"
                self.runs[run] = {'run': run, 'suite': out['suite'], 'started': now, 'ended': None,
                                  'pass': 0, 'fail': 0, 'skip': 0, 'probe': 0, 'last': None}
                self.current = run
                if len(self.runs) > 20:                       # 古い実行の集計は捨てる
                    for old in sorted(self.runs, key=lambda r: self.runs[r]['started'])[:-20]:
                        self.runs.pop(old, None)
            run = out.get('run') or self.current
            out['run'] = run
            r = self.runs.get(run)
            if r is not None:
                if kind in ('pass', 'fail', 'skip', 'probe'):
                    r[kind] += 1
                if kind == 'end':
                    r['ended'] = now
                r['last'] = now
            self.seq += 1
            out['seq'] = self.seq
            self.events.append(out)
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]
        return out

    def since(self, seq: int = 0, limit: int = 200):
        with self._lock:
            evs = [e for e in self.events if e['seq'] > seq]
        return evs[-limit:]

    def summary(self):
        with self._lock:
            r = self.runs.get(self.current) if self.current else None
            return {'seq': self.seq, 'current': dict(r) if r else None,
                    'runs': sorted((dict(x) for x in self.runs.values()), key=lambda x: -x['started'])[:5]}


activity = TestActivity()
