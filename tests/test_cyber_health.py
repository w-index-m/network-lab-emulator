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
