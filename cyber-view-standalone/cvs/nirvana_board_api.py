"""
ニルヴァーナのボード機能の HTTP API(/api/nirvana/*)。ラボ版(app.py)とスタンドアロン版(server.py)の両方で使う。

  GET  /api/nirvana/datasources                     データソースの一覧
  GET  /api/nirvana/query?ds&expr&start&end&step    時系列(start/end は now-1h 形式も可)
  GET  /api/nirvana/query_instant?ds&expr           その時点の値
  GET  /api/nirvana/logs?ds&expr&start&end&limit    ログ
  GET  /api/nirvana/labels?ds&label&metric          ラベルの値(変数の候補)
  GET  /api/nirvana/metric-names?ds                 メトリクス名(問い合わせの補完)
  GET|POST /api/nirvana/dashboards                  一覧 / 作成
  GET|PUT|DELETE /api/nirvana/dashboards/{uid}      取得 / 保存 / 削除
  POST /api/nirvana/dashboards/import               JSON の取り込み(Grafana と同じく panels/variables/time)
  GET|POST /api/nirvana/alert-rules                 アラートルール
  PUT|DELETE /api/nirvana/alert-rules/{uid}
  GET  /api/nirvana/alert-state                     各ルール・系列の状態(normal/pending/firing)と履歴
  POST /api/nirvana/alert-rules/{uid}/test          今の値で一度評価してみる(状態は変えない)
  GET|POST /api/nirvana/contact-points              連絡先(webhook)
  DELETE /api/nirvana/contact-points/{uid}
  POST /api/nirvana/contact-points/{uid}/test       試しに送る
  GET|POST /api/nirvana/annotations                 注釈(利用者のもの+システムの出来事)
  DELETE /api/nirvana/annotations/{uid}
"""
import time

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .nirvana_board import (QueryError, auto_step, parse_time, send_contact, validate_contact,
                            validate_dashboard, validate_rule, webhook_payload, _reduce, _CMP)

MAX_POINTS = 11000
_GF_UNITS = {'bps': 'bps', 'binbps': 'bps', 'Bps': 'Bps', 'binBps': 'Bps', 'bytes': 'bytes', 'decbytes': 'bytes',
             'percent': 'percent', 'percentunit': 'percentunit', 's': 's', 'ms': 'ms', 'pps': 'pps', 'short': 'short',
             'none': 'none', 'dtdurations': 's'}
_GF_TYPES = {'graph': 'timeseries', 'timeseries': 'timeseries', 'stat': 'stat', 'singlestat': 'stat', 'gauge': 'gauge',
             'bargauge': 'bar', 'barchart': 'bar', 'table': 'table', 'heatmap': 'heatmap', 'logs': 'logs', 'text': 'text',
             'state-timeline': 'heatmap', 'status-history': 'heatmap'}
_GF_COLORS = {'green': '#5dffa8', 'red': '#ff3b4e', 'orange': '#ffab2e', 'yellow': '#ffe066', 'blue': '#33d6ff',
              'purple': '#c58bff', 'dark-red': '#c0102a', 'semi-dark-red': '#e02035', 'light-green': '#9dff5d',
              'semi-dark-orange': '#ff9f3a', 'dark-orange': '#e07a00', 'text': '#bfe9ff'}


def from_grafana(d):
    """Grafana の書き出し JSON をニルヴァーナの形へ(24列→12列、fieldConfig の単位・しきい値、datasource、templating)。
    ニルヴァーナ自身の JSON はそのまま通す"""
    panels = d.get('panels') or []
    is_gf = 'templating' in d or 'schemaVersion' in d or any('fieldConfig' in p or 'datasource' in p for p in panels)
    if not is_gf:
        return d
    out = []
    flat = []
    for p in panels:                       # 折りたたんだ行(row)の中のパネルも出す
        if p.get('type') == 'row':
            flat.extend(p.get('panels') or [])
        else:
            flat.append(p)
    for i, p in enumerate(flat, 1):
        t = _GF_TYPES.get(p.get('type'))
        if not t:
            continue
        g = p.get('gridPos') or {'x': 0, 'y': 0, 'w': 12, 'h': 8}
        gp = {'x': int(g.get('x', 0)) // 2, 'w': max(1, -(-int(g.get('w', 12)) // 2)),
              'y': int(round(g.get('y', 0) * 0.6)), 'h': max(2, int(round(g.get('h', 8) * 0.6)))}
        gp['w'] = min(gp['w'], 12 - gp['x'])
        ds = p.get('datasource') or {}
        dtype = ds.get('type') if isinstance(ds, dict) else str(ds or '')
        fd = ((p.get('fieldConfig') or {}).get('defaults') or {})
        steps = ((fd.get('thresholds') or {}).get('steps')) or []
        opts = {'unit': _GF_UNITS.get(fd.get('unit'), 'none'),
                'thresholds': [{'value': s.get('value'), 'color': _GF_COLORS.get(s.get('color'), s.get('color') or '#33d6ff')} for s in steps]}
        if fd.get('min') is not None:
            opts['min'] = fd['min']
        if fd.get('max') is not None:
            opts['max'] = fd['max']
        if fd.get('decimals') is not None:
            opts['decimals'] = fd['decimals']
        maps = {}
        for m in fd.get('mappings') or []:
            for k, v in ((m.get('options') or {}).items() if m.get('type') == 'value' else []):
                maps[str(k)] = v.get('text', '') if isinstance(v, dict) else str(v)
        if maps:
            opts['mappings'] = maps
        custom = fd.get('custom') or {}
        if custom.get('fillOpacity') == 0:
            opts['fill'] = False
        if (custom.get('stacking') or {}).get('mode') in ('normal', 'percent'):
            opts['stack'] = True
        if t == 'text':
            opts['content'] = (p.get('options') or {}).get('content', '')
        out.append({'id': p.get('id') or i, 'type': t, 'title': p.get('title', ''), 'gridPos': gp,
                    'ds': 'loki' if 'loki' in dtype else 'prometheus',
                    'targets': [{'expr': x.get('expr', ''), 'legend': x.get('legendFormat', '') if x.get('legendFormat') != '__auto' else ''}
                                for x in p.get('targets') or [] if not x.get('hide')],
                    'options': opts})
    seen = set()
    for p in out:                          # id の重複は振り直す
        while p['id'] in seen:
            p['id'] = max(seen) + 1
        seen.add(p['id'])
    vars_ = []
    for v in (d.get('templating') or {}).get('list', []):
        if v.get('type') not in ('query', 'custom'):
            continue
        q = v.get('query')
        q = q if isinstance(q, str) else (q or {}).get('query', '')
        cur = (v.get('current') or {}).get('value')
        dsv = v.get('datasource') or {}
        vars_.append({'name': v.get('name'), 'label': v.get('label') or v.get('name'), 'type': v.get('type'),
                      'query': q, 'multi': bool(v.get('multi')), 'includeAll': bool(v.get('includeAll')),
                      'ds': 'loki' if 'loki' in str(dsv) else 'prometheus',
                      'current': cur if isinstance(cur, list) else ([cur] if cur else None)})
    t = d.get('time') or {}
    return {'uid': d.get('uid'), 'title': d.get('title') or 'Grafana から取り込み', 'tags': list(d.get('tags') or []) + ['grafana'],
            'time': {'from': t.get('from', 'now-1h'), 'to': t.get('to', 'now')}, 'refresh': d.get('refresh') or '',
            'variables': vars_, 'panels': out}



def make_router(get_sources, store, evaluator, system_annotations=None, check=None):
    """get_sources() -> Sources、store=BoardStore、evaluator=AlertEvaluator、
    system_annotations(start, end) -> [{time, text, tags, kind}]、check(request) -> None|JSONResponse(認証)"""
    r = APIRouter(prefix='/api/nirvana')

    def guard(request):
        return check(request) if check else None

    def err(msg, code=400):
        return JSONResponse({'error': str(msg)}, status_code=code)

    def _range(start, end, step):
        now = time.time()
        s, e = parse_time(start or 'now-1h', now), parse_time(end or 'now', now)
        if e <= s:
            raise QueryError('終了は開始より後にしてください')
        st = int(step) if step else auto_step(s, e)
        if (e - s) / max(1, st) > MAX_POINTS:
            st = int((e - s) / MAX_POINTS) + 1
        return s, e, st

    @r.get('/datasources')
    def datasources(request: Request):
        if (g := guard(request)):
            return g
        src = get_sources()
        return {'datasources': src.list(), 'default': src.default_metrics()}

    @r.get('/query')
    def query(request: Request, ds: str = 'internal', expr: str = '', start: str = 'now-1h', end: str = 'now',
              step: int = 0):
        if (g := guard(request)):
            return g
        if not expr.strip():
            return err('expr が必要です')
        try:
            s, e, st = _range(start, end, step)
            series = get_sources().query_range(ds, expr, s, e, st)
        except (QueryError, ValueError) as x:
            return err(x)
        except Exception as x:          # データソース不達
            return err(f'{ds} に問い合わせできません: {str(x)[:200]}', 502)
        return {'series': [{'labels': x['labels'], 'points': [[t, v] for t, v in x['points']]} for x in series],
                'start': s, 'end': e, 'step': st}

    @r.get('/query_instant')
    def query_instant(request: Request, ds: str = 'internal', expr: str = ''):
        if (g := guard(request)):
            return g
        try:
            return {'result': get_sources().query(ds, expr)}
        except (QueryError, ValueError) as x:
            return err(x)
        except Exception as x:
            return err(f'{ds} に問い合わせできません: {str(x)[:200]}', 502)

    @r.get('/logs')
    def logs(request: Request, ds: str = 'internal', expr: str = '', start: str = 'now-1h', end: str = 'now',
             limit: int = 200):
        if (g := guard(request)):
            return g
        try:
            s, e, _ = _range(start, end, 60)
            return {'logs': get_sources().logs(ds, expr, s, e, max(1, min(limit, 5000)))}
        except (QueryError, ValueError) as x:
            return err(x)
        except Exception as x:
            return err(f'{ds} に問い合わせできません: {str(x)[:200]}', 502)

    @r.get('/labels')
    def labels(request: Request, ds: str = 'internal', label: str = '', metric: str = ''):
        if (g := guard(request)):
            return g
        try:
            return {'values': get_sources().label_values(ds, label, metric or None)}
        except (QueryError, ValueError) as x:
            return err(x)
        except Exception as x:
            return err(f'{ds} に問い合わせできません: {str(x)[:200]}', 502)

    @r.get('/metric-names')
    def metric_names(request: Request, ds: str = 'internal'):
        if (g := guard(request)):
            return g
        try:
            return {'names': get_sources().metric_names(ds)}
        except Exception as x:
            return err(str(x)[:200], 502)

    # ── ダッシュボード ──
    @r.get('/dashboards')
    def dashboards(request: Request):
        if (g := guard(request)):
            return g
        return {'dashboards': sorted(({'uid': d['uid'], 'title': d['title'], 'tags': d.get('tags', []),
                                       'version': d.get('version'), 'updated': d.get('updated'),
                                       'panels': len(d.get('panels', []))} for d in store.all('dashboards')),
                                     key=lambda x: x['title'])}

    def _save_dash(d, uid=None, overwrite=True):
        try:
            validate_dashboard(d)
        except ValueError as x:
            return err(x)
        if uid:
            d['uid'] = uid
        try:
            return store.put('dashboards', d, overwrite=overwrite)
        except KeyError:
            return err(f'uid {d.get("uid")} は既にあります', 409)

    @r.post('/dashboards')
    def create_dashboard(request: Request, d: dict = Body(...)):
        if (g := guard(request)):
            return g
        d = dict(d)
        return _save_dash(d, overwrite=False)

    @r.post('/dashboards/import')
    def import_dashboard(request: Request, d: dict = Body(...), overwrite: bool = False):
        if (g := guard(request)):
            return g
        d = dict(d.get('dashboard') or d)          # Grafana の書き出し形式({"dashboard": {...}})も受ける
        d.pop('id', None)
        d = from_grafana(d)
        return _save_dash(d, overwrite=overwrite)

    @r.get('/dashboards/{uid}')
    def get_dashboard(request: Request, uid: str):
        if (g := guard(request)):
            return g
        d = store.get('dashboards', uid)
        return d if d else err('ありません', 404)

    @r.put('/dashboards/{uid}')
    def put_dashboard(request: Request, uid: str, d: dict = Body(...)):
        if (g := guard(request)):
            return g
        cur = store.get('dashboards', uid)
        if cur and d.get('version') is not None and d['version'] != cur.get('version'):
            return err(f'他で更新されています(version {cur.get("version")})。読み込み直してください', 409)
        return _save_dash(dict(d), uid=uid)

    @r.delete('/dashboards/{uid}')
    def del_dashboard(request: Request, uid: str):
        if (g := guard(request)):
            return g
        return {'ok': store.delete('dashboards', uid)}

    # ── アラート ──
    @r.get('/alert-rules')
    def rules(request: Request):
        if (g := guard(request)):
            return g
        return {'rules': sorted(store.all('alert_rules'), key=lambda x: x['title']), 'errors': dict(evaluator.errors)}

    @r.post('/alert-rules')
    def create_rule(request: Request, body: dict = Body(...)):
        if (g := guard(request)):
            return g
        try:
            validate_rule(body)
        except ValueError as x:
            return err(x)
        return store.put('alert_rules', dict(body), overwrite=False) if not body.get('uid') else store.put('alert_rules', dict(body))

    @r.put('/alert-rules/{uid}')
    def put_rule(request: Request, uid: str, body: dict = Body(...)):
        if (g := guard(request)):
            return g
        try:
            validate_rule(body)
        except ValueError as x:
            return err(x)
        body = dict(body, uid=uid)
        evaluator.forget(uid)
        return store.put('alert_rules', body)

    @r.delete('/alert-rules/{uid}')
    def del_rule(request: Request, uid: str):
        if (g := guard(request)):
            return g
        evaluator.forget(uid)
        return {'ok': store.delete('alert_rules', uid)}

    @r.post('/alert-rules/{uid}/test')
    def test_rule(request: Request, uid: str):
        if (g := guard(request)):
            return g
        rule = store.get('alert_rules', uid)
        if not rule:
            return err('ありません', 404)
        return _try_rule(rule)

    @r.post('/alert-rules/preview')
    def preview_rule(request: Request, body: dict = Body(...)):
        if (g := guard(request)):
            return g
        try:
            validate_rule(body)
        except ValueError as x:
            return err(x)
        return _try_rule(body)

    def _try_rule(rule):
        from .nirvana_board import parse_duration
        now = time.time()
        try:
            win = parse_duration(rule.get('window', '5m'))
            rows = get_sources().query_range(rule.get('ds', 'internal'), rule['expr'], now - win, now, max(5, int(win / 30)))
        except Exception as x:
            return err(str(x)[:300])
        out = []
        for row in rows:
            v = _reduce(row['points'], rule.get('reducer', 'last'))
            if v is not None:
                out.append({'labels': row['labels'], 'value': v,
                            'firing': _CMP[rule.get('op', '>')](v, float(rule.get('threshold', 0)))})
        return {'results': out}

    @r.get('/alert-state')
    def alert_state(request: Request):
        if (g := guard(request)):
            return g
        return {'instances': evaluator.states(), 'history': list(evaluator.history)[-200:], 'errors': dict(evaluator.errors)}

    @r.get('/contact-points')
    def contacts(request: Request):
        if (g := guard(request)):
            return g
        return {'contact_points': store.all('contact_points')}

    @r.post('/contact-points')
    def create_contact(request: Request, body: dict = Body(...)):
        if (g := guard(request)):
            return g
        try:
            body = validate_contact(dict(body, type=body.get('type') or 'webhook'))
        except ValueError as x:
            return err(x)
        return store.put('contact_points', body)

    @r.delete('/contact-points/{uid}')
    def del_contact(request: Request, uid: str):
        if (g := guard(request)):
            return g
        return {'ok': store.delete('contact_points', uid)}

    @r.post('/contact-points/{uid}/test')
    def test_contact(request: Request, uid: str):
        if (g := guard(request)):
            return g
        c = store.get('contact_points', uid)
        if not c:
            return err('ありません', 404)
        rule = {'uid': 'test', 'title': 'ニルヴァーナからの試験通知', 'severity': 'info', 'summary': '連絡先の試験です'}
        try:
            code = send_contact(c, webhook_payload(rule, {'key': 'test', 'labels': {}, 'value': 1, 'since': time.time()}, 'firing'))
        except Exception as x:
            return err(f'送れませんでした: {str(x)[:200]}', 502)
        return {'ok': True, 'status': code}

    # ── 注釈 ──
    @r.get('/annotations')
    def annotations(request: Request, start: str = 'now-6h', end: str = 'now', system: bool = True):
        if (g := guard(request)):
            return g
        now = time.time()
        try:
            s, e = parse_time(start, now), parse_time(end, now)
        except ValueError as x:
            return err(x)
        user = [dict(a, kind='user') for a in store.all('annotations') if s <= a.get('time', 0) <= e]
        sys_ = list(system_annotations(s, e)) if (system and system_annotations) else []
        for h in evaluator.history:
            if s <= h['t'] <= e and (h['to'] == 'firing' or h['from'] == 'firing'):
                sys_.append({'time': h['t'], 'text': f"ルール「{h['title']}」 {h['from']} → {h['to']} ({h['key']})",
                             'tags': ['rule', h['to']], 'kind': 'rule'})
        return {'annotations': sorted(user + sys_, key=lambda a: a['time'])}

    @r.post('/annotations')
    def create_annotation(request: Request, body: dict = Body(...)):
        if (g := guard(request)):
            return g
        text = str(body.get('text') or '').strip()
        if not text:
            return err('text が必要です')
        a = {'time': float(body.get('time') or time.time()), 'timeEnd': body.get('timeEnd'), 'text': text[:500],
             'tags': [str(t)[:40] for t in (body.get('tags') or [])][:10], 'dashboard': body.get('dashboard')}
        return store.put('annotations', a)

    @r.delete('/annotations/{uid}')
    def del_annotation(request: Request, uid: str):
        if (g := guard(request)):
            return g
        return {'ok': store.delete('annotations', uid)}

    return r
