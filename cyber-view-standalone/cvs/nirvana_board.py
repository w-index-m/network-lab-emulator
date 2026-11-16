"""
ニルヴァーナのボード機能(Grafana が持つ機能を /cyber-view に持たせるための部品)

  - データソース: prometheus(ヴァルハラ)/ loki / internal(このプロセスが自分で貯める時系列 + 内部ログ)
      internal は Prometheus が無い環境(Render 等)でもボードを動かすためのもの。/metrics と同じ値を
      10秒ごとに貯め、PromQL の基本部分(セレクタ・rate/irate/increase/delta/*_over_time・集約 by/without・
      四則演算・比較フィルタ)を自前で評価する。
  - ダッシュボード: Grafana と似た JSON(panels / variables / time / refresh)を保存・読み込み・取り込み・書き出し
  - アラートルール: 問い合わせ結果を条件で判定し、pending → firing → resolved を管理。連絡先(Webhook)へ通知
  - 注釈(annotation): 利用者が書いたものを保存(システムの出来事は呼び出し側で合わせる)

標準ライブラリだけで書き、スタンドアロン版(cyber-view-standalone/cvs/nirvana_board.py)にそのままコピーして使う。
"""
import collections
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

from . import valhalla as V

# ── 時間・単位 ──────────────────────────────────────
_DUR = re.compile(r'(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)')
_UNIT = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'y': 31536000}


def parse_duration(s):
    """'5m' / '1h30m' / '90' → 秒"""
    s = str(s).strip()
    if re.fullmatch(r'\d+(\.\d+)?', s):
        return float(s)
    total, pos = 0.0, 0
    for m in _DUR.finditer(s):
        if m.start() != pos:
            raise ValueError(f'bad duration: {s}')
        total += float(m.group(1)) * _UNIT[m.group(2)]
        pos = m.end()
    if pos != len(s) or not s:
        raise ValueError(f'bad duration: {s}')
    return total


def parse_time(s, now=None):
    """'now' / 'now-1h' / UNIX 秒 → UNIX 秒"""
    now = time.time() if now is None else now
    s = str(s).strip()
    if s == 'now':
        return now
    m = re.fullmatch(r'now-(.+)', s)
    if m:
        return now - parse_duration(m.group(1))
    return float(s)


def auto_step(start, end, points=240):
    return max(5, int(math.ceil((end - start) / points)))


# ── 内部の時系列(Prometheus テキスト形式を貯める) ─────────────────
_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)')
_LBL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def _unesc(v):
    return v.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')


def parse_exposition(text):
    """Prometheus のテキスト形式 → [(name, {labels}, value)]"""
    out = []
    for ln in text.splitlines():
        if not ln or ln.startswith('#'):
            continue
        m = _LINE.match(ln)
        if not m:
            continue
        try:
            v = float(m.group(4))
        except ValueError:
            continue
        labels = {k: _unesc(x) for k, x in _LBL.findall(m.group(3) or '')}
        out.append((m.group(1), labels, v))
    return out


class InternalTSDB:
    """名前+ラベルごとに (時刻, 値) を保持する小さな時系列 DB。retention 秒より古い点は捨てる"""

    def __init__(self, retention=6 * 3600, max_series=5000):
        self.retention = retention
        self.max_series = max_series
        self._s = {}                     # (name, tuple(sorted labels)) -> deque[(t, v)]
        self._lock = threading.Lock()

    def add(self, name, labels, value, t):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            dq = self._s.get(key)
            if dq is None:
                if len(self._s) >= self.max_series:
                    return
                dq = self._s[key] = collections.deque()
            dq.append((t, float(value)))
            cut = t - self.retention
            while dq and dq[0][0] < cut:
                dq.popleft()

    def ingest_text(self, text, t=None):
        t = time.time() if t is None else t
        n = 0
        for name, labels, v in parse_exposition(text):
            self.add(name, labels, v, t)
            n += 1
        return n

    def names(self):
        with self._lock:
            return sorted({k[0] for k in self._s})

    def label_values(self, label, name=None):
        with self._lock:
            vals = {dict(k[1]).get(label) for k in self._s if (name is None or k[0] == name)}
        return sorted(v for v in vals if v is not None)

    def label_names(self, name=None):
        with self._lock:
            ks = {lk for k in self._s if (name is None or k[0] == name) for lk, _ in k[1]}
        return sorted(ks)

    def select(self, name, matchers):
        """[(labels(dict, __name__ 込み), [(t,v)...])]"""
        out = []
        with self._lock:
            items = [(k, list(dq)) for k, dq in self._s.items() if k[0] == name]
        for (nm, lt), pts in items:
            labels = dict(lt)
            if all(_match(labels.get(l, ''), op, v) for l, op, v in matchers):
                out.append(({'__name__': nm, **labels}, pts))
        return out


def _match(val, op, want):
    if op == '=':
        return val == want
    if op == '!=':
        return val != want
    if op == '=~':
        return re.fullmatch(want, val) is not None
    if op == '!~':
        return re.fullmatch(want, val) is None
    return False


# ── 小さな PromQL 評価器(internal 用) ─────────────────────────
class QueryError(ValueError):
    pass


_TOK = re.compile(r'\s*(?:(\d+(?:\.\d+)?(?:e[+-]?\d+)?)(?![a-zA-Z_:])|([a-zA-Z_:][a-zA-Z0-9_:]*)|("(?:[^"\\]|\\.)*")'
                  r'|(\[[^\]]+\])|(==|!=|>=|<=|=~|!~|[(){},=<>+\-*/]))', re.I)
_RANGE_FN = {'rate', 'irate', 'increase', 'delta', 'avg_over_time', 'max_over_time', 'min_over_time',
             'sum_over_time', 'count_over_time', 'last_over_time'}
_AGG = {'sum', 'avg', 'max', 'min', 'count', 'topk', 'bottomk'}
_FN1 = {'abs', 'ceil', 'floor', 'round'}
LOOKBACK = 300


def _tokenize(s):
    pos, out = 0, []
    s = s.strip()
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            raise QueryError(f'読めない式です: {s[pos:pos + 20]!r}')
        num, ident, string, rng, op = m.groups()
        if num is not None:
            out.append(('num', float(num)))
        elif ident is not None:
            out.append(('id', ident))
        elif string is not None:
            out.append(('str', json.loads(string)))
        elif rng is not None:
            out.append(('range', parse_duration(rng[1:-1])))
        else:
            out.append(('op', op))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tk = self.peek()
        if (kind and tk[0] != kind) or (val is not None and tk[1] != val):
            raise QueryError(f'{val or kind} が必要です(位置 {self.i})')
        self.i += 1
        return tk

    def parse(self):
        node = self.cmp()
        if self.i != len(self.t):
            raise QueryError(f'余分な文字があります: {self.t[self.i][1]!r}')
        return node

    def cmp(self):
        left = self.add()
        while self.peek()[0] == 'op' and self.peek()[1] in ('>', '<', '>=', '<=', '==', '!='):
            op = self.take()[1]
            left = ('bin', op, left, self.add())
        return left

    def add(self):
        left = self.mul()
        while self.peek() in (('op', '+'), ('op', '-')):
            op = self.take()[1]
            left = ('bin', op, left, self.mul())
        return left

    def mul(self):
        left = self.unary()
        while self.peek() in (('op', '*'), ('op', '/')):
            op = self.take()[1]
            left = ('bin', op, left, self.unary())
        return left

    def unary(self):
        if self.peek() == ('op', '-'):
            self.take()
            return ('bin', '*', ('num', -1.0), self.unary())
        return self.atom()

    def atom(self):
        kind, val = self.peek()
        if kind == 'num':
            self.take()
            return ('num', val)
        if kind == 'op' and val == '(':
            self.take()
            n = self.cmp()
            self.take('op', ')')
            return n
        if kind == 'op' and val == '{':
            return self.selector(None)
        if kind != 'id':
            raise QueryError(f'式の途中で {val!r} は使えません')
        name = val
        if name in _AGG:
            self.take()
            mod = self.grouping()
            self.take('op', '(')
            param = None
            if name in ('topk', 'bottomk'):
                param = self.take('num')[1]
                self.take('op', ',')
            inner = self.cmp()
            self.take('op', ')')
            if mod is None:
                mod = self.grouping()
            return ('agg', name, mod, inner, param)
        if name in _RANGE_FN:
            self.take()
            self.take('op', '(')
            sel = self.selector(self.take('id')[1]) if self.peek()[0] == 'id' else self.selector(None)
            if sel[0] != 'msel':
                raise QueryError(f'{name}() には [5m] のような範囲が必要です')
            self.take('op', ')')
            return ('rfn', name, sel)
        if name in _FN1:
            self.take()
            self.take('op', '(')
            inner = self.cmp()
            self.take('op', ')')
            return ('fn', name, inner)
        self.take()
        return self.selector(name)

    def grouping(self):
        if self.peek()[0] == 'id' and self.peek()[1] in ('by', 'without'):
            kind = self.take()[1]
            self.take('op', '(')
            labels = []
            while self.peek() != ('op', ')'):
                labels.append(self.take('id')[1])
                if self.peek() == ('op', ','):
                    self.take()
            self.take('op', ')')
            return (kind, labels)
        return None

    def selector(self, name):
        matchers = []
        if self.peek() == ('op', '{'):
            self.take()
            while self.peek() != ('op', '}'):
                lab = self.take('id')[1]
                op = self.take('op')[1]
                if op not in ('=', '!=', '=~', '!~'):
                    raise QueryError(f'ラベルの比較 {op!r} は使えません')
                v = self.take('str')[1]
                if lab == '__name__' and op == '=':
                    name = v
                else:
                    matchers.append((lab, op, v))
                if self.peek() == ('op', ','):
                    self.take()
            self.take('op', '}')
        if not name:
            raise QueryError('メトリクス名が必要です')
        if self.peek()[0] == 'range':
            return ('msel', name, matchers, self.take()[1])
        return ('vsel', name, matchers)


def parse_promql(expr):
    return _Parser(_tokenize(expr)).parse()


def _sig(labels, drop_name=True):
    return tuple(sorted((k, v) for k, v in labels.items() if not (drop_name and k == '__name__')))


def _eval(node, db, t):
    """時刻 t での評価 → スカラー(float)か、[(labels, value)] のベクトル"""
    kind = node[0]
    if kind == 'num':
        return node[1]
    if kind == 'vsel':
        out = []
        for lab, pts in db.select(node[1], node[2]):
            v = None
            for pt, pv in reversed(pts):
                if pt <= t:
                    if t - pt <= LOOKBACK:
                        v = pv
                    break
            if v is not None:
                out.append((lab, v))
        return out
    if kind == 'msel':
        raise QueryError('範囲ベクトル([5m])は rate() などの関数の中で使ってください')
    if kind == 'rfn':
        fn, (_, name, matchers, rng) = node[1], node[2]
        out = []
        for lab, pts in db.select(name, matchers):
            w = [(pt, pv) for pt, pv in pts if t - rng < pt <= t]
            v = _range_fn(fn, w, rng)
            if v is not None:
                lab = {k: x for k, x in lab.items() if k != '__name__'}
                out.append((lab, v))
        return out
    if kind == 'fn':
        r = _eval(node[2], db, t)
        f = {'abs': abs, 'ceil': math.ceil, 'floor': math.floor, 'round': round}[node[1]]
        if isinstance(r, float):
            return float(f(r))
        return [({k: x for k, x in lab.items() if k != '__name__'}, float(f(v))) for lab, v in r]
    if kind == 'agg':
        _, op, mod, inner, param = node
        vec = _eval(inner, db, t)
        if isinstance(vec, float):
            raise QueryError(f'{op}() にはベクトルが必要です')
        groups = collections.OrderedDict()
        for lab, v in vec:
            if mod is None:
                key = ()
            elif mod[0] == 'by':
                key = tuple((k, lab.get(k, '')) for k in mod[1])
            else:
                key = tuple(sorted((k, x) for k, x in lab.items() if k not in mod[1] and k != '__name__'))
            groups.setdefault(key, []).append((lab, v))
        out = []
        for key, items in groups.items():
            vals = [v for _, v in items]
            if op in ('topk', 'bottomk'):
                items = sorted(items, key=lambda x: x[1], reverse=(op == 'topk'))[:int(param or 1)]
                out.extend(({k: x for k, x in lab.items() if k != '__name__'}, v) for lab, v in items)
                continue
            agg = {'sum': sum(vals), 'avg': sum(vals) / len(vals), 'max': max(vals), 'min': min(vals),
                   'count': float(len(vals))}[op]
            out.append((dict(key), agg))
        return out
    if kind == 'bin':
        _, op, a, b = node
        ra, rb = _eval(a, db, t), _eval(b, db, t)
        return _binop(op, ra, rb)
    raise QueryError(f'評価できない式です: {kind}')


def _range_fn(fn, w, rng):
    if not w:
        return None
    vals = [v for _, v in w]
    if fn in ('rate', 'irate', 'increase', 'delta'):
        if len(w) < 2:
            return None
        if fn == 'irate':
            (t0, v0), (t1, v1) = w[-2], w[-1]
            d = v1 - v0 if v1 >= v0 else v1
            return d / (t1 - t0) if t1 > t0 else None
        if fn == 'delta':
            return w[-1][1] - w[0][1]
        inc = 0.0
        for (_, p), (_, c) in zip(w, w[1:]):
            inc += c - p if c >= p else c          # カウンタのリセット
        span = w[-1][0] - w[0][0]
        if span <= 0:
            return None
        return inc / span if fn == 'rate' else inc * (rng / span)
    return {'avg_over_time': sum(vals) / len(vals), 'max_over_time': max(vals), 'min_over_time': min(vals),
            'sum_over_time': sum(vals), 'count_over_time': float(len(vals)), 'last_over_time': vals[-1]}[fn]


_CMP = {'>': lambda a, b: a > b, '<': lambda a, b: a < b, '>=': lambda a, b: a >= b,
        '<=': lambda a, b: a <= b, '==': lambda a, b: a == b, '!=': lambda a, b: a != b}


def _arith(op, a, b):
    if op == '+':
        return a + b
    if op == '-':
        return a - b
    if op == '*':
        return a * b
    if op == '/':
        return a / b if b != 0 else (math.inf if a > 0 else -math.inf if a < 0 else math.nan)
    return None


def _binop(op, ra, rb):
    sa, sb = isinstance(ra, float), isinstance(rb, float)
    if sa and sb:
        if op in _CMP:
            return 1.0 if _CMP[op](ra, rb) else 0.0
        return _arith(op, ra, rb)
    if op in _CMP:          # ベクトルの比較はフィルタ(Prometheus と同じ。値は左側を残す)
        if sb:
            return [(l, v) for l, v in ra if _CMP[op](v, rb)]
        if sa:
            return [(l, v) for l, v in rb if _CMP[op](ra, v)]
        idx = {_sig(l): v for l, v in rb}
        return [(l, v) for l, v in ra if _sig(l) in idx and _CMP[op](v, idx[_sig(l)])]
    if sb:
        return [({k: x for k, x in l.items() if k != '__name__'}, _arith(op, v, rb)) for l, v in ra]
    if sa:
        return [({k: x for k, x in l.items() if k != '__name__'}, _arith(op, ra, v)) for l, v in rb]
    idx = {_sig(l): v for l, v in rb}
    return [({k: x for k, x in l.items() if k != '__name__'}, _arith(op, v, idx[_sig(l)]))
            for l, v in ra if _sig(l) in idx]


def internal_query_range(db, expr, start, end, step):
    node = parse_promql(expr)
    series = collections.OrderedDict()
    t = start
    n = 0
    while t <= end + 1e-6 and n < 11000:
        r = _eval(node, db, t)
        if isinstance(r, float):
            series.setdefault((), ({}, []))[1].append((t, r))
        else:
            for lab, v in r:
                series.setdefault(_sig(lab, drop_name=False), (lab, []))[1].append((t, v))
        t += step
        n += 1
    return [{'labels': lab, 'points': pts} for lab, pts in series.values()]


# ── データソース ─────────────────────────────────────
class Sources:
    """prometheus / loki / internal の問い合わせ口。URL が無いものは使えない(available=False)"""

    def __init__(self, prometheus_url='', loki_url='', tsdb=None, internal_logs=None, opener=None):
        self.prometheus_url = (prometheus_url or '').strip()
        self.loki_url = (loki_url or '').strip()
        self.tsdb = tsdb or InternalTSDB()
        self.internal_logs = internal_logs          # callable(start, end) -> [{'t','host','msg',...}]
        self.opener = opener

    def list(self):
        out = [{'uid': 'internal', 'type': 'internal', 'name': 'NIRVANA 内部', 'available': True,
                'note': 'このプロセスが10秒ごとに貯める時系列(/metrics と同じ値)と内部ログ'}]
        out.append({'uid': 'prometheus', 'type': 'prometheus', 'name': 'VALHALLA (Prometheus)',
                    'available': bool(self.prometheus_url), 'url': self.prometheus_url or None})
        out.append({'uid': 'loki', 'type': 'loki', 'name': 'Loki', 'available': bool(self.loki_url),
                    'url': self.loki_url or None})
        return out

    def default_metrics(self):
        return 'prometheus' if self.prometheus_url else 'internal'

    def _need(self, ds):
        if ds == 'prometheus' and not self.prometheus_url:
            raise QueryError('ヴァルハラ(PROMETHEUS_URL)が設定されていません')
        if ds == 'loki' and not self.loki_url:
            raise QueryError('Loki(LOKI_URL)が設定されていません')
        if ds not in ('prometheus', 'loki', 'internal'):
            raise QueryError(f'データソース {ds!r} はありません')

    # メトリクス(時系列)
    def query_range(self, ds, expr, start, end, step):
        self._need(ds)
        if ds == 'internal':
            return internal_query_range(self.tsdb, expr, start, end, step)
        if ds == 'prometheus':
            return [{'labels': lab, 'points': pts} for lab, pts in V.query_range(self.prometheus_url, expr, start, end, step)]
        # loki のメトリクス式(count_over_time 等)
        d = self._loki('/loki/api/v1/query_range', {'query': expr, 'start': str(int(start * 1e9)),
                                                    'end': str(int(end * 1e9)), 'step': f'{int(step)}s'})
        if d.get('resultType') != 'matrix':
            raise QueryError('Loki の時系列には count_over_time(...) などのメトリクス式を使ってください')
        return [{'labels': r.get('metric', {}), 'points': [(float(t), float(v)) for t, v in r.get('values', [])]}
                for r in d.get('result', [])]

    def query(self, ds, expr, at=None):
        """その時点の値 → [{'labels','value'}]"""
        at = time.time() if at is None else at
        self._need(ds)
        if ds == 'prometheus':
            return [{'labels': lab, 'value': v} for lab, v in V.query(self.prometheus_url, expr, at)]
        if ds == 'internal':
            r = _eval(parse_promql(expr), self.tsdb, at)
            if isinstance(r, float):
                return [{'labels': {}, 'value': r}]
            return [{'labels': lab, 'value': v} for lab, v in r]
        rows = self.query_range(ds, expr, at - 60, at, 60)
        return [{'labels': r['labels'], 'value': r['points'][-1][1]} for r in rows if r['points']]

    # ログ
    def logs(self, ds, expr, start, end, limit=500):
        if ds == 'internal':
            rows = list(self.internal_logs(start, end)) if self.internal_logs else []
            pat = (expr or '').strip()
            if pat:
                try:
                    rx = re.compile(pat, re.I)
                except re.error as e:
                    raise QueryError(f'正規表現が読めません: {e}') from e
                rows = [r for r in rows if rx.search(r.get('msg', '')) or rx.search(r.get('host', ''))]
            rows = sorted(rows, key=lambda r: r['t'], reverse=True)[:limit]
            return [{'t': r['t'], 'labels': {'host': r.get('host', ''), 'level': r.get('level', '')}, 'line': r.get('msg', '')}
                    for r in rows]
        self._need(ds)
        if ds != 'loki':
            raise QueryError('ログは loki か internal で問い合わせてください')
        d = self._loki('/loki/api/v1/query_range', {'query': expr or '{job=~".+"}', 'start': str(int(start * 1e9)),
                                                    'end': str(int(end * 1e9)), 'limit': str(limit), 'direction': 'backward'})
        out = []
        for st in d.get('result', []):
            for ts, line in st.get('values', []):
                out.append({'t': int(ts) / 1e9, 'labels': st.get('stream', {}), 'line': line})
        out.sort(key=lambda r: r['t'], reverse=True)
        return out[:limit]

    # ラベル(変数の候補)
    def label_values(self, ds, label, metric=None):
        self._need(ds)
        if ds == 'internal':
            return self.tsdb.label_values(label, metric)
        if ds == 'prometheus':
            p = {'match[]': metric} if metric else {}
            return list(V._get(self.prometheus_url, f'/api/v1/label/{urllib.parse.quote(label)}/values', p))
        return list(self._loki(f'/loki/api/v1/label/{urllib.parse.quote(label)}/values', {}) or [])

    def metric_names(self, ds):
        self._need(ds)
        if ds == 'internal':
            return self.tsdb.names()
        if ds == 'prometheus':
            return list(V._get(self.prometheus_url, '/api/v1/label/__name__/values', {}))
        return []

    def _loki(self, path, params):
        q = urllib.parse.urlencode(params)
        op = self.opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(self.loki_url.rstrip('/') + path + ('?' + q if q else ''), timeout=10) as r:
            d = json.loads(r.read().decode())
        if d.get('status') != 'success':
            raise QueryError(d.get('error') or 'loki error')
        return d.get('data')


# ── 保存(ダッシュボード・アラートルール・連絡先・注釈) ─────────────────
def _uid():
    return uuid.uuid4().hex[:10]


def default_dashboard(ds='internal'):
    """最初から入っている「ネットワーク概要」"""
    return {
        'uid': 'nirvana-overview', 'title': 'ネットワーク概要', 'tags': ['nirvana', 'network'],
        'time': {'from': 'now-1h', 'to': 'now'}, 'refresh': '30s',
        'variables': [
            {'name': 'link', 'label': 'リンク', 'type': 'query', 'ds': ds, 'query': 'label_values(netlab_link_state, host_a)',
             'multi': True, 'includeAll': True, 'current': ['$__all']},
        ],
        'panels': [
            {'id': 1, 'type': 'stat', 'title': '総合状態', 'ds': ds, 'gridPos': {'x': 0, 'y': 0, 'w': 4, 'h': 4},
             'targets': [{'expr': 'netlab_health_state{category="overall"}', 'legend': '総合'}],
             'options': {'mappings': {'0': '正常', '1': '注意', '2': '異常'},
                         'thresholds': [{'value': None, 'color': '#5dffa8'}, {'value': 1, 'color': '#ffab2e'}, {'value': 2, 'color': '#ff3b4e'}]}},
            {'id': 2, 'type': 'stat', 'title': 'リンク断', 'ds': ds, 'gridPos': {'x': 4, 'y': 0, 'w': 4, 'h': 4},
             'targets': [{'expr': 'count(netlab_link_state == 3)', 'legend': '断'}],
             'options': {'unit': 'none', 'zeroIfEmpty': True,
                         'thresholds': [{'value': None, 'color': '#5dffa8'}, {'value': 1, 'color': '#ff3b4e'}]}},
            {'id': 3, 'type': 'gauge', 'title': '最大使用率', 'ds': ds, 'gridPos': {'x': 8, 'y': 0, 'w': 4, 'h': 4},
             'targets': [{'expr': 'max(netlab_link_util)', 'legend': '最大'}],
             'options': {'unit': 'percentunit', 'min': 0, 'max': 1,
                         'thresholds': [{'value': None, 'color': '#5dffa8'}, {'value': 0.5, 'color': '#ffab2e'}, {'value': 0.8, 'color': '#ff3b4e'}]}},
            {'id': 4, 'type': 'timeseries', 'title': 'リンク流量 ($link)', 'ds': ds, 'gridPos': {'x': 0, 'y': 4, 'w': 8, 'h': 6},
             'targets': [{'expr': 'sum by (host_a, host_b) (netlab_link_bps{host_a=~"$link"})', 'legend': '{{host_a}} ⇄ {{host_b}}'}],
             'options': {'unit': 'bps', 'fill': True}},
            {'id': 5, 'type': 'bar', 'title': 'リンク使用率', 'ds': ds, 'gridPos': {'x': 8, 'y': 4, 'w': 4, 'h': 6},
             'targets': [{'expr': 'netlab_link_util', 'legend': '{{host_a}}⇄{{host_b}}'}],
             'options': {'unit': 'percentunit', 'min': 0, 'max': 1,
                         'thresholds': [{'value': None, 'color': '#33d6ff'}, {'value': 0.5, 'color': '#ffab2e'}, {'value': 0.8, 'color': '#ff3b4e'}]}},
            {'id': 6, 'type': 'heatmap', 'title': '状態ボードの推移', 'ds': ds, 'gridPos': {'x': 0, 'y': 10, 'w': 6, 'h': 5},
             'targets': [{'expr': 'netlab_health_state{category!="overall"}', 'legend': '{{category}}'}],
             'options': {'min': 0, 'max': 2}},
            {'id': 7, 'type': 'table', 'title': 'OSPF 隣接', 'ds': ds, 'gridPos': {'x': 6, 'y': 10, 'w': 6, 'h': 5},
             'targets': [{'expr': 'netlab_ospf_adjacency_state', 'legend': ''}],
             'options': {'mappings': {'8': 'Full', '1': 'Down', '3': 'Init', '4': '2Way'}}},
            {'id': 8, 'type': 'logs', 'title': 'ログ', 'ds': 'internal', 'gridPos': {'x': 0, 'y': 15, 'w': 12, 'h': 5},
             'targets': [{'expr': '', 'legend': ''}], 'options': {}},
        ],
    }


class BoardStore:
    """JSON ファイル1つに保存する。path が None ならメモリだけ"""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self.data = {'dashboards': {}, 'alert_rules': {}, 'contact_points': {}, 'annotations': {}}
        self.load()

    def load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    d = json.load(f)
                for k in self.data:
                    self.data[k] = dict(d.get(k) or {})
            except (OSError, ValueError):
                pass

    def save(self):
        if not self.path:
            return
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def ensure_default(self, ds):
        with self._lock:
            if not self.data['dashboards']:
                d = default_dashboard(ds)
                d['version'] = 1
                d['updated'] = time.time()
                self.data['dashboards'][d['uid']] = d
                self.save()

    # 汎用 CRUD
    def all(self, kind):
        with self._lock:
            return [dict(v) for v in self.data[kind].values()]

    def get(self, kind, uid):
        with self._lock:
            v = self.data[kind].get(uid)
            return dict(v) if v else None

    def put(self, kind, obj, overwrite=True):
        obj = dict(obj)
        obj['uid'] = str(obj.get('uid') or _uid())
        with self._lock:
            old = self.data[kind].get(obj['uid'])
            if old and not overwrite:
                raise KeyError(obj['uid'])
            obj['version'] = (old or {}).get('version', 0) + 1
            obj['updated'] = time.time()
            self.data[kind][obj['uid']] = obj
            self.save()
        return obj

    def delete(self, kind, uid):
        with self._lock:
            ok = self.data[kind].pop(uid, None) is not None
            if ok:
                self.save()
        return ok


# ── 検証 ────────────────────────────────────────
PANEL_TYPES = ('timeseries', 'stat', 'gauge', 'bar', 'table', 'logs', 'heatmap', 'text')


def validate_dashboard(d):
    if not isinstance(d, dict) or not str(d.get('title') or '').strip():
        raise ValueError('title が必要です')
    panels = d.get('panels') or []
    if not isinstance(panels, list):
        raise ValueError('panels は配列です')
    seen = set()
    for p in panels:
        if p.get('type') not in PANEL_TYPES:
            raise ValueError(f'パネルの種類 {p.get("type")!r} は使えません({"/".join(PANEL_TYPES)})')
        if p.get('id') in seen:
            raise ValueError(f'パネル id {p.get("id")} が重複しています')
        seen.add(p.get('id'))
        g = p.get('gridPos') or {}
        for k in ('x', 'y', 'w', 'h'):
            if not isinstance(g.get(k), (int, float)):
                raise ValueError(f'パネル {p.get("id")} の gridPos.{k} が必要です')
    for v in d.get('variables') or []:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(v.get('name', ''))):
            raise ValueError(f'変数名 {v.get("name")!r} が不正です')
    t = d.get('time') or {}
    parse_time(t.get('from', 'now-1h'))
    parse_time(t.get('to', 'now'))
    return d


OPS = ('>', '<', '>=', '<=', '==', '!=')
REDUCERS = ('last', 'avg', 'max', 'min', 'sum', 'count')


def validate_rule(r):
    if not str(r.get('title') or '').strip():
        raise ValueError('title が必要です')
    if not str(r.get('expr') or '').strip():
        raise ValueError('expr(問い合わせ)が必要です')
    if r.get('op', '>') not in OPS:
        raise ValueError(f'比較 {r.get("op")!r} は使えません')
    if r.get('reducer', 'last') not in REDUCERS:
        raise ValueError(f'集計 {r.get("reducer")!r} は使えません')
    float(r.get('threshold', 0))
    parse_duration(r.get('for', '0s'))
    parse_duration(r.get('window', '5m'))
    if r.get('ds', 'internal') == 'internal':
        parse_promql(r['expr'])
    return r


def validate_contact(c):
    if c.get('type', 'webhook') != 'webhook':
        raise ValueError('連絡先の種類は webhook のみです')
    u = urllib.parse.urlparse(str(c.get('url') or ''))
    if u.scheme not in ('http', 'https') or not u.netloc:
        raise ValueError('url は http(s)://... で指定してください')
    if not str(c.get('name') or '').strip():
        raise ValueError('name が必要です')
    return c


# ── アラートの評価 ─────────────────────────────────────
def _reduce(points, how):
    vals = [v for _, v in points if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    return {'last': vals[-1], 'avg': sum(vals) / len(vals), 'max': max(vals), 'min': min(vals),
            'sum': sum(vals), 'count': float(len(vals))}[how]


def series_key(labels):
    return ','.join(f'{k}={v}' for k, v in sorted(labels.items()) if k != '__name__') or '{}'


class AlertEvaluator:
    """ルールごと・系列ごとに Normal → Pending → Firing を管理する(Grafana の Alerting と同じ考え方)。
    on_change(rule, inst, prev_state, new_state) が状態変化のたびに呼ばれる"""

    def __init__(self, store, sources, on_change=None, history=500):
        self.store, self.sources = store, sources
        self.on_change = on_change
        self.instances = {}                       # (rule_uid, key) -> {...}
        self.history = collections.deque(maxlen=history)
        self.errors = {}                          # rule_uid -> 最後のエラー
        self._lock = threading.Lock()

    def evaluate(self, now=None):
        now = time.time() if now is None else now
        changes = []
        live = set()
        for r in self.store.all('alert_rules'):
            if not r.get('enabled', True):
                continue
            uid = r['uid']
            try:
                win = parse_duration(r.get('window', '5m'))
                step = max(5, int(win / 30) or 5)
                rows = self.sources.query_range(r.get('ds', 'internal'), r['expr'], now - win, now, step)
                self.errors.pop(uid, None)
            except Exception as e:        # データソース不達・式の誤りはルールのエラーとして見せる
                self.errors[uid] = str(e)[:300]
                continue
            op, thr = r.get('op', '>'), float(r.get('threshold', 0))
            hold = parse_duration(r.get('for', '0s'))
            for row in rows:
                v = _reduce(row['points'], r.get('reducer', 'last'))
                if v is None:
                    continue
                key = series_key(row['labels'])
                live.add((uid, key))
                cond = _CMP[op](v, thr)
                with self._lock:
                    inst = self.instances.setdefault((uid, key), {'rule': uid, 'key': key, 'labels': row['labels'],
                                                                  'state': 'normal', 'since': now, 'active_at': None})
                    inst['value'] = v
                    inst['last_eval'] = now
                    prev = inst['state']
                    if cond:
                        if prev == 'normal':
                            inst['active_at'] = now
                            new = 'firing' if hold <= 0 else 'pending'
                        elif prev == 'pending' and now - inst['active_at'] >= hold:
                            new = 'firing'
                        else:
                            new = prev
                    else:
                        new = 'normal'
                        inst['active_at'] = None
                    if new != prev:
                        inst['state'], inst['since'] = new, now
                        changes.append((r, dict(inst), prev, new))
        # 系列が消えたら解消扱い
        with self._lock:
            for k in [k for k in self.instances if k not in live and k[0] not in self.errors]:
                inst = self.instances.pop(k)
                if inst['state'] != 'normal':
                    r = self.store.get('alert_rules', k[0])
                    if r:
                        changes.append((r, dict(inst, state='normal'), inst['state'], 'normal'))
        for r, inst, prev, new in changes:
            self.history.append({'t': now, 'rule': r['uid'], 'title': r['title'], 'key': inst['key'],
                                 'labels': inst['labels'], 'from': prev, 'to': new, 'value': inst.get('value')})
            if self.on_change:
                try:
                    self.on_change(r, inst, prev, new)
                except Exception:                 # 通知の失敗で評価を止めない
                    pass
        return changes

    def states(self):
        with self._lock:
            return [dict(v) for v in self.instances.values()]

    def forget(self, rule_uid):
        with self._lock:
            for k in [k for k in self.instances if k[0] == rule_uid]:
                self.instances.pop(k)
        self.errors.pop(rule_uid, None)


def webhook_payload(rule, inst, new_state, external_url=''):
    """Grafana / Alertmanager の webhook に近い形"""
    status = 'firing' if new_state == 'firing' else 'resolved'
    labels = {'alertname': rule['title'], 'severity': rule.get('severity', 'warning'),
              **{k: v for k, v in (rule.get('labels') or {}).items()},
              **{k: str(v) for k, v in inst.get('labels', {}).items() if k != '__name__'}}
    return {
        'receiver': 'nirvana', 'status': status, 'externalURL': external_url,
        'title': f"[{status.upper()}] {rule['title']}",
        'message': (rule.get('summary') or '') + f" (値 {inst.get('value')})",
        'alerts': [{'status': status, 'labels': labels,
                    'annotations': {'summary': rule.get('summary') or rule['title']},
                    'startsAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(inst.get('since') or time.time())),
                    'values': {'A': inst.get('value')}, 'fingerprint': f"{rule['uid']}:{inst['key']}"}],
    }


def send_webhook(url, payload, timeout=5):
    """連絡先(webhook)へ POST。戻り値は HTTP ステータス(失敗時は例外)"""
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    host = urllib.parse.urlparse(url).hostname or ''
    direct = host in ('localhost',) or re.match(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)', host)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        return r.status
