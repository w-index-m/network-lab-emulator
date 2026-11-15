"""
/cyber-view の状態ボード (/api/traffic/health) と syslog 記録の回帰テスト

4観点 (ネットワーク / トラフィック / セキュリティ / syslog) が、問題が無ければ ok(正常)、
リンク断・到達不可・アラート・error/warning のログでそれぞれ warn/crit になること。
"""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.traffic_analytics import recorder  # noqa: E402
from engine.link_stats import sampler  # noqa: E402

_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def clean(client):
    """他テストのフロー・アラート・ログ・リンクの影響を消して、このテストの装置だけにする"""
    saved = dict(app_module.device_sessions)
    links = {k: set(v) for k, v in app_module.vnet.links.items()}
    app_module.device_sessions.clear()
    app_module.vnet.links.clear()
    recorder.clear()
    sampler.clear()
    yield client
    app_module.device_sessions.clear()
    app_module.device_sessions.update(saved)
    app_module.vnet.links.clear()
    app_module.vnet.links.update(links)
    recorder.clear()


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


def _lab(c):
    r1, r2 = f'hl-r1-{_RUN}', f'hl-r2-{_RUN}'
    c.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'HL-R1'})
    c.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'HL-R2'})
    c.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0', 'iface_b': 'GigabitEthernet0/0/0'})
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.245.12.1 255.255.255.252', 'no shutdown', 'end')
    _cli(c, r2, 'configure terminal', 'interface GigabitEthernet0/0/0',
         'ip address 10.245.12.2 255.255.255.252', 'no shutdown', 'end')
    recorder.clear()          # 構築時のログは数えない
    return r1, r2


def _cat(c):
    d = c.get('/api/traffic/health').json()
    return d, {x['key']: x for x in d['categories']}


def test_all_ok_shows_normal(clean):
    c = clean
    r1, _r2 = _lab(c)
    _cli(c, r1, 'ping 10.245.12.2')
    d, cat = _cat(c)
    assert set(cat) == {'network', 'traffic', 'security', 'syslog'}
    assert d['overall'] == 'ok' and all(x['state'] == 'ok' for x in cat.values()), d
    assert '到達率 100%' in cat['traffic']['summary']


def test_link_down_is_network_crit_and_syslog_warn(clean):
    c = clean
    r1, _r2 = _lab(c)
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end')
    d, cat = _cat(c)
    assert cat['network']['state'] == 'crit' and 'HL-R1' in cat['network']['details'][0]
    # %LINK-3-UPDOWN は severity 3 (error) として syslog 観点に入る
    assert cat['syslog']['state'] == 'crit'
    assert any('%LINK-3-UPDOWN' in x for x in cat['syslog']['details'])
    assert d['overall'] == 'crit'


def test_unreachable_ping_is_traffic_warn(clean):
    c = clean
    r1, _r2 = _lab(c)
    _cli(c, r1, 'ping 10.245.12.2')
    _cli(c, r1, 'ping 10.245.99.9')
    _d, cat = _cat(c)
    assert cat['traffic']['state'] in ('warn', 'crit')
    assert any('10.245.99.9' in x for x in cat['traffic']['details'])


def test_alerts_are_security(clean):
    c = clean
    r1, _r2 = _lab(c)
    _cli(c, r1, 'ping 10.245.12.2 size 9000')            # MTU超のICMP (low)
    _d, cat = _cat(c)
    assert cat['security']['state'] == 'warn' and 'MTU' in cat['security']['details'][0]


def test_protocol_log_buffer_is_recorded_with_severity():
    dev = f'hl-log-{_RUN}'
    recorder.clear()
    app_module.proto_log_buffer.setdefault(dev, []).append(
        {'message': '%OSPF-5-ADJCHG: Process 1, Nbr 1.1.1.1 on Gi0/0/0 from FULL to DOWN'})
    app_module.proto_log_buffer[dev] = []             # フロントの取得でクリアされても残る
    app_module.proto_log_buffer[dev].append('IKE negotiation failed')
    logs = [x for x in recorder.logs if x['dev'] == dev]
    assert [x['sev'] for x in logs] == [5, 3]
    assert app_module._guess_severity('x', level='WARN') == 4
    assert app_module._guess_severity('normal info') == 6


def test_recovery_messages_are_not_problems(clean):
    c = clean
    r1, _r2 = _lab(c)
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'no shutdown', 'end')
    recorder.clear()
    # 実機同様 up も %LINK-3 だが、復旧なので syslog 観点では問題にしない
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'shutdown', 'end')
    recorder.clear()
    _cli(c, r1, 'configure terminal', 'interface GigabitEthernet0/0/0', 'no shutdown', 'end')
    _d, cat = _cat(c)
    ups = [x for x in recorder.logs if '%LINK-3-UPDOWN' in x['msg'] and 'state to up' in x['msg']]
    assert ups and all(x['sev'] == 3 for x in ups)
    assert len({x['msg'] for x in ups}) == len(ups)       # 時刻付き/無しの二重記録をしない
    assert cat['syslog']['state'] == 'ok'


class _FakeLoki:
    """Loki の /loki/api/v1/query_range を返すだけの簡易サーバ"""
    def __init__(self, values):
        import http.server
        import json
        import threading
        body = json.dumps({'status': 'success', 'data': {'resultType': 'streams', 'result': [
            {'stream': {'job': 'netlab-device-log', 'device_id': 'lk-r1', 'hostname': 'LK-R1'},
             'values': values}]}}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.srv.server_port}'


def test_syslog_from_loki_when_configured(clean, monkeypatch):
    import time as _t
    now_ns = int(_t.time() * 1e9)
    fake = _FakeLoki([[str(now_ns - 5_000_000_000), '%OSPF-5-ADJCHG: Nbr 1.1.1.1 from FULL to DOWN'],
                      [str(now_ns - 3_000_000_000), '%SYS-3-CPUHOG: Task ran for 2004 msec'],
                      [str(now_ns - 1_000_000_000), '%LINK-3-UPDOWN: Interface Gi0/0/0, changed state to up']])
    try:
        monkeypatch.setenv('LOKI_URL', fake.url)
        d, cat = _cat(clean)
        assert cat['syslog']['source'] == 'loki' and d['sources']['logs'] == 'loki'
        assert cat['syslog']['state'] == 'crit'                 # CPUHOG(3)。up は復旧なので数えない
        assert any('CPUHOG' in x for x in cat['syslog']['details'])
        logs = clean.get('/api/traffic/logs?since=0').json()
        assert logs['source'] == 'loki' and [x['recovery'] for x in logs['logs']] == [False, False, True]
        assert [x['sev'] for x in logs['logs']] == [5, 3, 3]
    finally:
        fake.srv.shutdown()


def test_unreachable_loki_falls_back_to_internal(clean, monkeypatch):
    monkeypatch.setenv('LOKI_URL', 'http://127.0.0.1:9')
    _d, cat = _cat(clean)
    assert cat['syslog']['source'] == 'internal' and cat['syslog']['source_error']


def test_alert_sources_for_hex_petals(clean):
    """六角形の花びらは検知元(IDS/FW/SYSLOG/NET)単位。FWのshunとerrorログもアラートになる"""
    c = clean
    r1, _r2 = _lab(c)
    from engine.threat_detection import threat_detection_engine
    st = app_module.device_sessions[r1]
    cfg = {'enabled': True, 'shun': True, 'rate_interval': 10, 'average_rate': 1, 'burst_rate': 1,
           'shun_duration': 60, 'shun_except': []}
    for _ in range(30):
        threat_detection_engine.record_connection_attempt(id(st), '10.245.77.7', cfg)
    recorder.record_log(r1, 'HL-R1', 3, '%SYS-3-CPUHOG: Task ran for 2004 msec')
    al = {a['rule']: a for a in recorder.alerts}
    assert al['fw_shun']['source'] == 'FW' and al['fw_shun']['src'] == r1 and al['fw_shun']['dst'] == '10.245.77.7'
    assert al['syslog_error']['source'] == 'SYSLOG'
    _d, cat = _cat(c)
    # セキュリティ観点は IDS/FW だけ(FWの遮断は high)。syslog の error は syslog 観点で数える
    assert cat['security']['state'] == 'crit'
    assert all('error' not in x for x in cat['security']['details'])
    threat_detection_engine.clear_shun(id(st))
