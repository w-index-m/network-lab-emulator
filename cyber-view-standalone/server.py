"""
Cyber View(スタンドアロン版)— 実機の監視データで NIRVANA 改風の画面を出す

  python3 server.py -c config.yaml [--host 0.0.0.0] [--port 8090]
  → http://<host>:8090/

画面(web/cyber_view.html)はラボ版と同じファイル。画面が呼ぶ /api/traffic/* を、
Prometheus / Loki / Zabbix と設定ファイル(装置・つながり)から返す。
"""
import argparse
import asyncio
import collections
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cvs import config as C  # noqa: E402
from cvs.collector import Collector  # noqa: E402
from cvs.link_evidence import traps  # noqa: E402

WEB = Path(__file__).resolve().parent / 'web'


def create_app(cfg, start_collector=True):
    col = Collector(cfg)

    @asynccontextmanager
    async def lifespan(_app):
        if start_collector:
            col.start()
        yield
        col.stop()

    app = FastAPI(title='Cyber View (standalone)', lifespan=lifespan)
    app.state.collector = col
    token = (cfg.get('auth') or {}).get('token') or ''

    def check(x_session_token):
        if token and x_session_token != token:
            raise HTTPException(status_code=401, detail='token が違います')

    @app.get('/', response_class=HTMLResponse)
    @app.get('/cyber-view', response_class=HTMLResponse)
    def page():
        html = (WEB / 'cyber_view.html').read_text(encoding='utf-8')
        title = cfg.get('title')
        if title:
            html = html.replace('<title>', '<title>' + title + ' — ', 1)
        return HTMLResponse(html)

    @app.get('/api/health')
    def ok():
        return {'ok': True, 'errors': col.errors, 'notes': col.notes}

    @app.get('/api/traffic/hosts')
    def hosts(x_session_token: str = Header(None)):
        check(x_session_token)
        return {'hosts': col.hosts(), 'now': time.time()}

    @app.get('/api/traffic/links')
    def links(window: int = 60, x_session_token: str = Header(None)):
        check(x_session_token)
        s = col.snapshot(window)
        prom = (cfg.get('sources') or {}).get('prometheus') or {}
        return {'links': s['links'], 'window': s['window'], 'now': s['now'],
                'source': '+'.join(s['sources_used']) or 'none',
                'prometheus_url': prom.get('url'), 'prometheus_error': col.errors.get('prometheus'),
                'zabbix_error': col.errors.get('zabbix'), 'sample_interval': cfg['poll_interval'],
                'trap_listen': traps.listen, 'unreachable': s['unreachable']}

    @app.get('/api/traffic/flowmap')
    def flowmap(since: int = 0, alert_since: int = 0, x_session_token: str = Header(None)):
        """ラボ版の ping 経路(events)は実機監視には無いので空。アラートだけ返す"""
        check(x_session_token)
        s = col.snapshot(60)
        nodes = {d['id']: {'id': d['id'], 'hostname': d['hostname'], 'type': d['type']} for d in cfg['devices']}
        links = [{'a': x['a'], 'b': x['b'], 'iface_a': x['iface_a'], 'iface_b': x['iface_b'], 'down': x['down'],
                  'ab_bytes': 0, 'ba_bytes': 0} for x in s['links']]
        al = col.analytics.alerts_since(alert_since)
        evs = [e for e in list(col.sec.flow_events) if e['seq'] > since][-300:]     # NetFlow / sFlow があれば通信の粒
        return {'nodes': list(nodes.values()), 'links': links, 'events': evs, 'alerts': al,
                'seq': col.sec.flow_seq, 'alert_seq': col.analytics.alert_seq, 'now': time.time()}

    # ── フロー集計(sources.flow.store に保存したフローを、送信元・宛先・通信量でまとめる) ──
    @app.get('/flows', response_class=HTMLResponse)
    def flows_page():
        return HTMLResponse((WEB / 'flows.html').read_text(encoding='utf-8'))

    def _flow_store():
        st = col.sec.store
        if st is None:
            raise HTTPException(status_code=404, detail='フローを保存していません(sources.flow.store を設定してください)')
        return st

    @app.get('/api/flows/summary')
    def flows_summary(window: int = 3600, group: str = 'pair', ip: str = None, limit: int = 50,
                      x_session_token: str = Header(None)):
        check(x_session_token)
        now = time.time()
        try:
            return _flow_store().summary(now - max(60, window), now, group=group, limit=min(500, limit), ip=ip or None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    @app.get('/api/flows/export.csv')
    def flows_export(window: int = 3600, ip: str = None, x_session_token: str = Header(None)):
        check(x_session_token)
        now = time.time()
        body = _flow_store().export_csv(now - max(60, window), now, ip=ip or None)
        return Response(content=body.encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="flows.csv"'})

    @app.get('/api/attack')
    def attack(since: int = 0, window: int = 86400, x_session_token: str = Header(None)):
        """ATT&CK の表と進行度(ネクサスの中央なら拠点ごとの輪も)"""
        check(x_session_token)
        sec = col.sec
        sec.tick()
        d = sec.tracker.snapshot(since, max(60, min(int(window), 7 * 86400)),
                                 extra=[e for e in sec.hub.attack_events() if e.get('org') != sec.site])
        for t in d['targets']:
            t['host'] = (col.devs.get(t['target']) or {}).get('hostname', t['target'])
        for e in d['events']:
            e['target_host'] = (col.devs.get(e['target']) or {}).get('hostname', e['target'])
        d['site'] = sec.site
        d['nexus'] = {**sec.hub.snapshot(), 'site': sec.site,
                      'reporter': ({'url': sec.reporter.url, **sec.reporter.status} if sec.reporter else None)}
        return d

    @app.post('/api/nexus/report')
    def nexus_report(body: dict = Body(...), x_nexus_token: str = Header(None)):
        """ほかの拠点からの報告(この画面を中央にする場合。sources.nexus.token と一致すること)"""
        if not col.sec.hub_token or x_nexus_token != col.sec.hub_token:
            raise HTTPException(403, 'X-Nexus-Token が違います')
        try:
            return {'ok': True, 'findings': col.sec.hub.ingest(body.get('site'), body)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.get('/api/darknet')
    def darknet(since: int = 0, limit: int = 400, x_session_token: str = Header(None)):
        """ダークネット観測(sources.darknet の帯に届いた NetFlow / sFlow のフロー)"""
        check(x_session_token)
        if not col.sec.darknet.prefixes:
            raise HTTPException(404, 'sources.darknet が未設定')
        from cvs.darknet import CAT_NAME
        f = col.sec.flows.stats if col.sec.flows else {}
        return {**col.sec.darknet.snapshot(since, max(1, min(int(limit), 2000))), 'cat_names': CAT_NAME,
                'sensor': {'running': bool(col.sec.flows), 'passive': True,
                           'label': f"NetFlow / sFlow から(フロー {f.get('flows', 0)} 件)" if col.sec.flows else 'フローの受け口なし'}}

    @app.get('/api/wan')
    def wan(x_session_token: str = Header(None)):
        """回線チェック(sources.wan_probe の宛先への本物の ping)"""
        check(x_session_token)
        if not col.sec.probes:
            raise HTTPException(404, 'sources.wan_probe が未設定')
        from cvs.wan_watch import RTT_HIGH, LOSS_HIGH
        return {'cpes': [], 'bas': [], 'paths': col.sec.wan.snapshot(), 'thresholds': {'rtt_ms': RTT_HIGH, 'loss': LOSS_HIGH},
                'interval': col.sec.probe_interval, 'simulated': False}

    @app.get('/api/traffic/health')
    def health(window: int = 300, x_session_token: str = Header(None)):
        check(x_session_token)
        return col.health(max(30, min(int(window), 86400)))

    @app.get('/api/traffic/logs')
    def logs(since: float = 0, limit: int = 30, x_session_token: str = Header(None)):
        check(x_session_token)
        return {'logs': col.logs(since, limit), 'source': 'loki' if col.loki else 'none',
                'error': col.errors.get('loki'), 'now': time.time()}

    @app.get('/api/traffic/alerts')
    def alerts(since: int = 0, x_session_token: str = Header(None)):
        check(x_session_token)
        return {'alerts': col.analytics.alerts_since(since), 'alert_seq': col.analytics.alert_seq}

    @app.get('/api/traffic/routes')
    def routes(since: int = 0, limit: int = 100, x_session_token: str = Header(None)):
        """経路の変化と CPU の変化(情)。sources.snmp があれば実機から SNMP で定期取得した経路表の差分"""
        check(x_session_token)
        from cvs.route_watch import describe
        w = col.snmp.watch
        evs = w.since(since, max(1, min(int(limit), 500)))
        return {'events': [{**e, 'text': describe(e)} for e in evs],
                'seq': w.seq, 'interval': col.snmp.interval if col.snmp.enabled else None}

    # ── ヴァルハラ(Prometheus)連携: ② 再生 ③ 予測 ④ OSPF ──
    from cvs import valhalla_views as VV
    from cvs.collector import classify
    prom_cfg = (cfg.get('sources') or {}).get('prometheus') or {}

    def need_prom():
        if not col.prom:
            raise HTTPException(status_code=404, detail='sources.prometheus が未設定です(ヴァルハラに繋がっていません)')
        return col.prom.url, prom_cfg.get('job', 'snmp')

    @app.get('/api/valhalla/status')
    def valhalla_status(x_session_token: str = Header(None)):
        check(x_session_token)
        if not col.prom:
            return {'configured': False}
        try:
            n = VV.V.query(col.prom.url, f'count(up{{job="{prom_cfg.get("job", "snmp")}"}})')
            return {'configured': True, 'reachable': True, 'url': col.prom.url, 'recording': bool(n and n[0][1] > 0)}
        except Exception as e:  # noqa: BLE001
            return {'configured': True, 'reachable': False, 'url': col.prom.url, 'error': str(e)[:200]}

    @app.get('/api/valhalla/replay')
    def valhalla_replay(start: float = None, end: float = None, step: int = 15, x_session_token: str = Header(None)):
        check(x_session_token)
        url, job = need_prom()
        end = end or time.time()
        start = start or end - 3600
        step = max(5, int(step), int((end - start) / 600))
        try:
            d = VV.replay(url, job, cfg, start, end, step, classify)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f'ヴァルハラ不達: {str(e)[:200]}')
        return {**d, 'start': start, 'end': end, 'step': step, 'source': 'valhalla'}

    @app.get('/api/valhalla/forecast')
    def valhalla_forecast(fit_window: str = '1h', base_window: str = '1d', horizon_days: float = 7,
                          x_session_token: str = Header(None)):
        check(x_session_token)
        import re
        if not re.fullmatch(r'\d+[smhd]', fit_window) or not re.fullmatch(r'\d+[smhd]', base_window):
            raise HTTPException(status_code=400, detail='fit_window / base_window は 30m・1h・1d の形で指定')
        url, job = need_prom()
        try:
            rows = VV.forecast(url, job, cfg, fit_window, base_window, horizon_days)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f'ヴァルハラ不達: {str(e)[:200]}')
        return {'links': rows, 'alert_util': VV.V.UTIL_ALERT, 'fit_window': fit_window, 'base_window': base_window,
                'horizon_days': horizon_days, 'now': time.time()}

    @app.get('/api/traffic/ospf')
    def ospf(dest: str = None, x_session_token: str = Header(None)):
        """④ 実機の OSPF 隣接(OSPF-MIB を snmp_exporter → ヴァルハラ経由で)"""
        check(x_session_token)
        if any(col.snmp.ospf_now.values()):       # SNMP で直接読めていればそれを使う(ヴァルハラが無い構成)
            from cvs import snmp_ospf
            return {**snmp_ospf.snapshot(cfg, col.snmp.ospf_now, col.dev_of), 'now': time.time()}
        url, job = need_prom()
        try:
            d = VV.ospf(url, job, cfg, col.dev_of)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f'ヴァルハラ不達: {str(e)[:200]}')
        return {**d, 'now': time.time()}

    # ── ボード(Grafana が持つ機能: BOARD / EXPLORE / ALERTS)。ラボ版と同じ cvs/nirvana_board*.py ──
    import threading
    from cvs import nirvana_board as NB
    from cvs.nirvana_board_api import make_router
    from cvs import valhalla as V
    from fastapi import Request
    loki_cfg = (cfg.get('sources') or {}).get('loki') or {}
    tsdb = NB.InternalTSDB()
    store = NB.BoardStore(os.environ.get('NIRVANA_BOARD_PATH') or (cfg.get('board') or {}).get('path')
                          or str(Path(__file__).resolve().parent / 'nirvana_board.json'))
    lv = {0: 'emerg', 1: 'alert', 2: 'crit', 3: 'error', 4: 'warning', 5: 'notice', 6: 'info', 7: 'debug'}

    def board_logs(start, end):
        return [{'t': x['t'], 'host': x.get('host', ''), 'msg': x['msg'], 'level': lv.get(x.get('sev'), '')}
                for x in col.analytics.logs_between(start, end)]

    def sources():
        return NB.Sources(col.prom.url if col.prom else '', loki_cfg.get('url', ''), tsdb, board_logs)

    def on_change(rule, inst, prev, new):
        if new == 'firing':
            lab = inst.get('labels') or {}
            src = col.dev_of(lab.get('device'), lab.get('host_a'), lab.get('instance'), lab.get('a'))
            dst = col.dev_of(lab.get('neighbor'), lab.get('host_b'), lab.get('b'))
            sev = {'critical': 'high', 'warning': 'warning', 'info': 'low'}.get(rule.get('severity', 'warning'), 'medium')
            col.analytics.fire('nirvana_rule', f"{rule['uid']}:{inst['key']}", src, dst,
                               f"{rule['title']}: 値 {inst.get('value'):.4g} ({inst['key']})"[:200], severity=sev, name=f"ルール: {rule['title']}")
        if new == 'firing' or (new == 'normal' and prev == 'firing'):
            for cid in rule.get('contacts') or []:
                c = store.get('contact_points', cid)
                if c:
                    try:
                        NB.send_contact(c, NB.webhook_payload(rule, inst, new))
                    except Exception as e:  # noqa: BLE001
                        col.errors['webhook'] = str(e)[:200]

    evaluator = NB.AlertEvaluator(store, None, on_change=on_change)

    def board_check(request: Request):
        if token and request.headers.get('X-Session-Token') != token:
            return JSONResponse({'error': 'token が違います'}, status_code=401)
        return None

    def board_annotations(start, end):
        return [{'time': a['t'], 'text': f"{a['name']}: {a.get('detail', '')}"[:200], 'tags': ['alert', a.get('source', '')], 'kind': 'alert'}
                for a in col.analytics.alerts_between(start, end)]

    app.include_router(make_router(sources, store, evaluator, system_annotations=board_annotations, check=board_check))
    app.state.board = {'store': store, 'tsdb': tsdb, 'evaluator': evaluator}

    def board_loop(stop):
        store.ensure_default('internal')
        n = 0
        while not stop.wait(5 if n else 0):
            try:
                s = col.snapshot(60)
                tsdb.ingest_text(V.render_metrics(s['links'], col.health(300), None,
                                                  dict(collections.Counter((a['rule'], a['severity'], a['source']) for a in col.analytics.alerts_between(0, time.time())))))
            except Exception as e:  # noqa: BLE001
                col.errors['board'] = str(e)[:200]
            if n % 3 == 0:
                evaluator.sources = sources()
                evaluator.evaluate()
            n += 1

    board_stop = threading.Event()
    if start_collector:
        threading.Thread(target=board_loop, args=(board_stop,), daemon=True).start()

    @app.get('/nirvana-board.js')
    def board_js():
        from fastapi.responses import Response
        return Response((WEB / 'nirvana_board.js').read_text(encoding='utf-8'), media_type='application/javascript; charset=utf-8')

    # ── WORLD(地球儀): クラウド宛ての流量・Radar・IHR・都市の粒度(RIPE Atlas・攻撃元の都市)。ラボ版と同じ cvs/world_* ──
    from cvs.world_sources import world
    wcfg = cfg.get('world') or {}
    site = cfg.get('site') or {}
    world_cache = str(Path(wcfg.get('cache_dir') or (Path(__file__).parent / 'data' / 'world')))
    if wcfg.get('pairs'):
        world.set_pairs(wcfg['pairs'])
    col.sec.world = world                      # フローを地球儀へ(security.on_flow)

    def world_loop(stop):
        world.ensure_geo(world_cache)
        while not stop.wait(5):
            try:
                if wcfg.get('enabled', True) and world.enabled() and world.due():
                    world.refresh(world_cache)
            except Exception as e:  # noqa: BLE001
                col.errors['world'] = str(e)[:200]
            stop.wait(55)

    if start_collector and wcfg.get('enabled', True):
        threading.Thread(target=world_loop, args=(board_stop,), daemon=True).start()

    @app.get('/static/world/world.json')
    def world_map():
        from fastapi.responses import FileResponse
        return FileResponse(WEB / 'world' / 'world.json', media_type='application/json')

    @app.get('/api/world')
    def world_state(x_session_token: str = Header(None)):
        check(x_session_token)
        d = world.snapshot(col.sec.darknet)
        d['site'] = {'name': site.get('name') or cfg.get('title') or 'SITE', 'lat': float(site.get('lat', 35.68)),
                     'lon': float(site.get('lon', 139.69))}
        d['mawi'] = {'running': False, 'available': False}
        return d

    @app.post('/api/world/refresh')
    def world_refresh(x_session_token: str = Header(None)):
        check(x_session_token)
        world.refresh(world_cache)
        return world.snapshot(col.sec.darknet)

    @app.post('/api/world/paths')
    async def world_paths(request: Request, x_session_token: str = Header(None)):
        check(x_session_token)
        body = await request.json()
        pairs = world.set_pairs(body.get('pairs') or [])
        if not pairs:
            return JSONResponse({'error': '都市の組(from / to)を1つ以上指定してください'}, status_code=400)
        await asyncio.to_thread(world.refresh_city)
        return {'pairs': pairs, 'atlas': world.atlas}

    @app.get('/api/world/anchors')
    def world_anchors(q: str = '', x_session_token: str = Header(None)):
        check(x_session_token)
        lst, n = world.anchor_list(q)
        return {'anchors': lst, 'count': n}

    @app.get('/api/world/geo')
    def world_geo(ip: str, x_session_token: str = Header(None)):
        check(x_session_token)
        return {'ip': ip, 'geo': world.geo.lookup(ip), 'db': world.geo.status}

    @app.post('/api/mawi/replay')
    def mawi_replay():
        return JSONResponse({'error': 'MAWI の再生はラボ版だけです(単体版は自分の NetFlow / sFlow を地球儀に出します)'}, status_code=404)

    # ── AI ログ解析(cvs/log_ai.py はラボ版の engine/log_ai.py と同じ)。単体版は装置にログインしないので実行はしない ──
    from cvs.log_ai import analyst, provider_config

    def ai_devices():
        return [{'id': d['id'], 'hostname': d['hostname'], 'type': d.get('type', '?')} for d in cfg['devices']]

    @app.get('/api/ai/status')
    def ai_status(x_session_token: str = Header(None)):
        check(x_session_token)
        c = provider_config()
        return {'provider': c['provider'], 'model': c['model'], 'cloud': c['cloud'], 'apply': False,
                'redact_ips': os.environ.get('NIRVANA_AI_REDACT_IPS', '1') != '0'}

    @app.post('/api/ai/logs/analyze')
    async def ai_analyze(request: Request, x_session_token: str = Header(None)):
        check(x_session_token)
        body = await request.json()
        minutes = max(1, min(1440, int(body.get('minutes', 30))))
        now = time.time()
        rows = [dict(r) for r in col.analytics.logs_between(now - minutes * 60, now)]
        for a in col.analytics.alerts_between(now - minutes * 60, now):
            if a.get('rule') == 'ai_finding':
                continue
            dev = a.get('src') if a.get('src') in col.devs else a.get('dst') if a.get('dst') in col.devs else None
            rows.append({'t': a['t'], 'dev': dev or a.get('src'), 'host': col.devs[dev]['hostname'] if dev else a.get('src'),
                         'sev': 4 if a.get('severity') == 'high' else 5,
                         'msg': f"%NIRVANA-4-ALERT: {a.get('name', a.get('rule'))}: {a.get('detail', '')}"})
        if body.get('device'):
            want = str(body['device']).lower()
            rows = [r for r in rows if str(r.get('dev', '')).lower() == want or str(r.get('host', '')).lower() == want]
        rec = await asyncio.to_thread(analyst.analyze, rows, ai_devices(), bool(body.get('use_ai', True)),
                                      max(20, min(1000, int(body.get('max_lines', 200)))))
        for a in rec['actions']:
            a['executable'] = False                 # 単体版は表示とコピーだけ
        return {**rec, 'minutes': minutes}

    @app.get('/api/ai/logs/history')
    def ai_history(x_session_token: str = Header(None)):
        check(x_session_token)
        return {'history': [{k: h[k] for k in ('id', 't', 'provider', 'model', 'error', 'summary', 'stats')} for h in reversed(analyst.history)]}

    @app.post('/api/ai/logs/apply')
    def ai_apply():
        return JSONResponse({'error': '単体版は装置に入れません(コマンドをコピーして使ってください)'}, status_code=403)

    @app.get('/api/demo/status')
    def demo_status():
        """デモはラボ専用。画面はこれを見て DEMO ボタンを隠す"""
        return JSONResponse({'available': False, 'running': False}, status_code=404)

    return app


def main():
    ap = argparse.ArgumentParser(description='Cyber View standalone')
    ap.add_argument('-c', '--config', default=os.environ.get('CYBER_VIEW_CONFIG', 'config.yaml'))
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=int(os.environ.get('CYBER_VIEW_PORT', '8090')))
    a = ap.parse_args()
    import uvicorn
    uvicorn.run(create_app(C.load(a.config)), host=a.host, port=a.port, log_level='info')


if __name__ == '__main__':
    main()
