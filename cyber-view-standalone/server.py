"""
Cyber View(スタンドアロン版)— 実機の監視データで NIRVANA 改風の画面を出す

  python3 server.py -c config.yaml [--host 0.0.0.0] [--port 8090]
  → http://<host>:8090/

画面(web/cyber_view.html)はラボ版と同じファイル。画面が呼ぶ /api/traffic/* を、
Prometheus / Loki / Zabbix と設定ファイル(装置・つながり)から返す。
"""
import argparse
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cvs import config as C  # noqa: E402
from cvs.collector import Collector  # noqa: E402

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
        return {'ok': True, 'errors': col.errors}

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
                'trap_listen': None, 'unreachable': s['unreachable']}

    @app.get('/api/traffic/flowmap')
    def flowmap(since: int = 0, alert_since: int = 0, x_session_token: str = Header(None)):
        """ラボ版の ping 経路(events)は実機監視には無いので空。アラートだけ返す"""
        check(x_session_token)
        s = col.snapshot(60)
        nodes = {d['id']: {'id': d['id'], 'hostname': d['hostname'], 'type': d['type']} for d in cfg['devices']}
        links = [{'a': x['a'], 'b': x['b'], 'iface_a': x['iface_a'], 'iface_b': x['iface_b'], 'down': x['down'],
                  'ab_bytes': 0, 'ba_bytes': 0} for x in s['links']]
        al = col.analytics.alerts_since(alert_since)
        return {'nodes': list(nodes.values()), 'links': links, 'events': [], 'alerts': al,
                'seq': 0, 'alert_seq': col.analytics.alert_seq, 'now': time.time()}

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
        """経路の変化(情)はラボ専用。実機版では未対応(空を返す)"""
        check(x_session_token)
        return {'events': [], 'seq': 0, 'interval': cfg['poll_interval']}

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
