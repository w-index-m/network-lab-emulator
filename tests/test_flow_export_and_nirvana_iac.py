"""
装置からの sFlow / NetFlow の送出と、ニルヴァーナ監視の IaC(iac/nirvana-monitoring)

- NX-OS / Arista の sflow、IOS の ip flow-export と ip flow ingress が running-config と show に出る
- ラボの ping が装置を通ると、本物の UDP で sFlow v5 / NetFlow v5 が届き、ニルヴァーナの受け口で読める
- IaC の各ファイル(compose・ヴァルハラの設定・監視対象・ニルヴァーナの設定)が互いに食い違っていない
"""
import os
import socket
import sys
import uuid

os.environ['NETLAB_AUTH_DISABLE'] = '1'

import pytest  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
import app as app_module  # noqa: E402
from engine import flow_export  # noqa: E402
from engine.flow_collector import parse_netflow_v5, parse_sflow_v5  # noqa: E402

IAC = os.path.join(ROOT, 'iac', 'nirvana-monitoring')
_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    return TestClient(app_module.app)


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json().get('output', '')
    return out


def _udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    s.settimeout(3)
    return s


def test_encoders_round_trip():
    sf = flow_export.build_sflow_v5('10.9.0.1', 1, 1000, [{'src': '10.1.1.1', 'dst': '10.2.2.2', 'size': 1500, 'rate': 64, 'pool': 500}])
    f = parse_sflow_v5(sf)[0]
    assert (f['src'], f['dst'], f['proto'], f['packets'], f['exporter']) == ('10.1.1.1', '10.2.2.2', 'icmp', 64, '10.9.0.1')
    nf = parse_netflow_v5(flow_export.build_netflow_v5(1, 1000, [{'src': '10.1.1.1', 'dst': '10.2.2.2', 'packets': 5, 'bytes': 640, 'proto': 1}]))[0]
    assert (nf['packets'], nf['bytes'], nf['proto']) == (5, 640, 'icmp')


def test_nexus_sflow_and_ios_netflow_really_send(client):
    r, n = f'fx-r-{_RUN}', f'fx-n-{_RUN}'
    client.post('/api/device', json={'id': r, 'type': 'cisco', 'hostname': 'FX-R'})
    client.post('/api/device', json={'id': n, 'type': 'nexus', 'hostname': 'FX-N'})
    client.post('/api/link', json={'a': r, 'b': n, 'iface_a': 'GigabitEthernet0/0/2', 'iface_b': 'Ethernet1/1'})
    _cli(client, r, 'configure terminal', 'interface GigabitEthernet0/0/2', 'ip address 10.245.1.1 255.255.255.252', 'no shutdown', 'end')
    _cli(client, n, 'configure terminal', 'interface Ethernet1/1', 'no switchport', 'ip address 10.245.1.2/30', 'no shutdown', 'end')
    sf_rx, nf_rx = _udp(), _udp()
    try:
        _cli(client, n, 'configure terminal', 'feature sflow', 'sflow sampling-rate 32', 'sflow agent-ip 10.245.1.2',
             'sflow collector-ip 127.0.0.1 vrf default', f'sflow collector-port {sf_rx.getsockname()[1]}',
             'sflow data-source interface Ethernet1/1', 'end')
        _cli(client, r, 'configure terminal', f'ip flow-export destination 127.0.0.1 {nf_rx.getsockname()[1]}',
             'ip flow-export version 5', 'interface GigabitEthernet0/0/2', 'ip flow ingress', 'end')
        assert 'NetFlow v5 だけ' in _cli(client, r, 'configure terminal', 'ip flow-export version 9')
        _cli(client, r, 'end')
        rc_n = _cli(client, n, 'show running-config')
        assert 'feature sflow' in rc_n and 'sflow sampling-rate 32' in rc_n and 'sflow collector-ip 127.0.0.1 vrf default' in rc_n
        rc_r = _cli(client, r, 'show running-config')
        assert f'ip flow-export destination 127.0.0.1 {nf_rx.getsockname()[1]}' in rc_r and 'ip flow-export version 5' in rc_r
        assert '!!!!!' in _cli(client, r, 'ping 10.245.1.2 repeat 100 size 1000')
        f = parse_sflow_v5(sf_rx.recvfrom(65535)[0])[0]
        assert f['exporter'] == '10.245.1.2' and f['dst'] == '10.245.1.2' and f['sampling_rate'] == 32
        g = parse_netflow_v5(nf_rx.recvfrom(65535)[0])[0]
        assert g['dst'] == '10.245.1.2' and g['packets'] == 100 and g['bytes'] == 100 * 1028
        assert 'Datagrams sent       : 1' in _cli(client, n, 'show sflow')
        assert '1 flows exported' in _cli(client, r, 'show ip flow export')
        # 止める
        _cli(client, n, 'configure terminal', 'no feature sflow', 'end')
        _cli(client, r, 'configure terminal', 'interface GigabitEthernet0/0/2', 'no ip flow ingress', 'end')
        sf_rx.settimeout(0.5)
        nf_rx.settimeout(0.5)
        _cli(client, r, 'ping 10.245.1.2')
        for s in (sf_rx, nf_rx):
            with pytest.raises(socket.timeout):
                s.recvfrom(65535)
    finally:
        sf_rx.close()
        nf_rx.close()


def test_arista_sflow_and_logging_transport_port(client):
    a = f'fx-a-{_RUN}'
    client.post('/api/device', json={'id': a, 'type': 'arista', 'hostname': 'FX-A'})
    _cli(client, a, 'configure terminal', 'sflow sample 128', 'sflow destination 127.0.0.1 16999', 'sflow run',
         'logging host 127.0.0.1 transport udp port 5515', 'end')
    rc = _cli(client, a, 'show running-config')
    assert 'sflow sample 128' in rc and 'sflow destination 127.0.0.1 16999' in rc and 'sflow run' in rc
    assert 'logging host 127.0.0.1 5515' in rc
    st = app_module.device_sessions[a]
    assert st.flow_export['sflow']['enabled'] and st.syslog_servers[0]['port'] == 5515


# ── IaC の整合 ─────────────────────────────────────
def _y(*p):
    return yaml.safe_load(open(os.path.join(IAC, *p), encoding='utf-8'))


def test_iac_files_agree():
    compose = _y('docker-compose.yml')
    svc = compose['services']
    assert set(svc) == {'snmp-exporter', 'valhalla', 'loki', 'syslog-bridge', 'nirvana'}
    assert all(s.get('network_mode') == 'host' for s in svc.values())
    for s in svc.values():                                 # 参照しているファイルが実在する
        for v in s.get('volumes', []):
            src = v.split(':')[0]
            if src.startswith('.'):
                assert os.path.exists(os.path.normpath(os.path.join(IAC, src))), src
    assert os.path.isfile(os.path.join(IAC, svc['nirvana']['build'], 'Dockerfile'))
    prom = _y('prometheus', 'prometheus.yml')
    jobs = {j['job_name']: j for j in prom['scrape_configs']}
    exporter_port = next(a for a in svc['snmp-exporter']['command'] if 'listen-address' in a).rsplit(':', 1)[1]
    assert jobs['cisco-snmp']['relabel_configs'][-1]['replacement'] == f'127.0.0.1:{exporter_port}'
    from cvs import config as sa_config
    cfg = sa_config.validate(_y('nirvana', 'config.yaml'))
    src = cfg['sources']
    assert src['prometheus']['job'] == 'cisco-snmp' and src['prometheus']['url'].endswith(':9091')
    assert '--web.listen-address=:9091' in svc['valhalla']['command']
    assert src['loki']['url'].endswith(':3101') and 'http_listen_port: 3101' in open(os.path.join(IAC, 'loki', 'loki.yml')).read()
    assert '--syslog-port 5515' in svc['syslog-bridge']['command']
    targets = {t for g in _y('prometheus', 'targets', 'devices.yml') for t in g['targets']}
    assert targets == {d['mgmt_ip'] for d in cfg['devices']}           # ヴァルハラとニルヴァーナが同じ装置を見る
    lab = open(os.path.join(IAC, 'lab', 'provision_nirvana_lab.sh'), encoding='utf-8').read()
    assert f"127.0.0.1 {src['flow']['netflow']}" in lab and 'sflow collector-ip 127.0.0.1' in lab
    assert 'port 5515' in lab and 'logging server 127.0.0.1 5515' in lab
    for d in cfg['devices']:
        assert d['mgmt_ip'] in lab or d['id'] in ('cr1', 'cr2')     # CR1/CR2 は Grafana 版の構築スクリプトで作る
