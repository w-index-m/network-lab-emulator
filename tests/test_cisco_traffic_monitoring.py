"""
Cisco ルータのトラフィック監視 (iac/cisco-traffic-monitoring) の回帰テスト

- IF-MIB ifXTable (ifName/ifHCIn/OutOctets/ifHighSpeed/ifAlias) と Counter64
- Counter32 の 2^32 折り返し
- IOS の `ping <dest> repeat <n> size <bytes>` がカウンタに反映されること
  (往路+復路、経路上のリンクだけ)
- CPU(CISCO-PROCESS-MIB)の乱歩が「ポーリング回数」ではなく時間で進むこと
- IaC 定義同士の整合(ダッシュボード/アラートが参照するメトリクスが
  snmp_exporter の定義にあるか、コミュニティ/監視対象IPがラボ構築スクリプトと一致するか)

docker 自体を起動する検証は手動のライブ検証(docs/cisco-traffic-monitoring-iac.md)。
"""

import json
import os
import re
import sys
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
import app as app_module  # noqa: E402
from engine.protocols import dp_engine, snmp_agent  # noqa: E402
from engine.snmp_udp_agent import _encode_value, TAG_COUNTER32, TAG_COUNTER64  # noqa: E402

IAC = os.path.join(ROOT, 'iac', 'cisco-traffic-monitoring')
_RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope='module')
def client():
    with TestClient(app_module.app) as c:
        yield c


def _cli(c, dev, *cmds):
    out = ''
    for cmd in cmds:
        out = c.post('/api/cli', json={'device_id': dev, 'command': cmd}).json()['output']
    return out


@pytest.fixture(scope='module')
def lab(client):
    """r1 Gi0/0/0 ─ r2 Gi0/0/0 (10.241.12.0/30)、r1 Gi0/0/1 ─ pc (10.241.1.0/24)"""
    r1, r2, pc = f'ctm-r1-{_RUN}', f'ctm-r2-{_RUN}', f'ctm-pc-{_RUN}'
    client.post('/api/device', json={'id': r1, 'type': 'cisco', 'hostname': 'CTM-R1'})
    client.post('/api/device', json={'id': r2, 'type': 'cisco', 'hostname': 'CTM-R2'})
    client.post('/api/device', json={'id': pc, 'type': 'pc', 'hostname': 'CTM-PC'})
    client.post('/api/link', json={'a': r1, 'b': r2, 'iface_a': 'GigabitEthernet0/0/0',
                                   'iface_b': 'GigabitEthernet0/0/0'})
    client.post('/api/link', json={'a': r1, 'b': pc, 'iface_a': 'GigabitEthernet0/0/1',
                                   'iface_b': 'eth0'})
    _cli(client, r1, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'description WAN to R2',
         'ip address 10.241.12.1 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.241.1.1 255.255.255.0', 'no shutdown', 'exit',
         'ip route 10.241.2.0 255.255.255.0 10.241.12.2', 'end')
    _cli(client, r2, 'configure terminal',
         'interface GigabitEthernet0/0/0', 'ip address 10.241.12.2 255.255.255.252', 'no shutdown', 'exit',
         'interface GigabitEthernet0/0/1', 'ip address 10.241.2.1 255.255.255.0', 'no shutdown', 'exit',
         'ip route 10.241.1.0 255.255.255.0 10.241.12.1', 'end')
    return client, r1, r2


def _mib(dev):
    return {oid: (t, v) for oid, t, v in snmp_agent._build_mib(dev)}


def _if_index(mib, name):
    for oid, (_t, v) in mib.items():
        if oid.startswith('1.3.6.1.2.1.2.2.1.2.') and v == name:
            return oid.rsplit('.', 1)[1]
    raise AssertionError(f'{name} not in ifTable')


class TestIfXTable:
    def test_ifxtable_columns(self, lab):
        _c, r1, _r2 = lab
        mib = _mib(r1)
        i = _if_index(mib, 'GigabitEthernet0/0/0')
        xt = '1.3.6.1.2.1.31.1.1.1'
        assert mib[f'{xt}.1.{i}'] == ('STRING', 'Gi0/0/0')           # ifName は短縮名
        assert mib[f'{xt}.6.{i}'][0] == 'Counter64'                  # ifHCInOctets
        assert mib[f'{xt}.10.{i}'][0] == 'Counter64'                 # ifHCOutOctets
        assert mib[f'{xt}.15.{i}'] == ('Gauge32', '1000')            # ifHighSpeed(Mbps)
        assert mib[f'{xt}.18.{i}'] == ('STRING', 'WAN to R2')        # ifAlias = description

    def test_counter_encoding(self):
        c32 = _encode_value('Counter32', (1 << 32) + 5)
        assert c32[0] == TAG_COUNTER32 and c32[-1] == 5 and len(c32) == 3   # 2^32で折り返す
        c64 = _encode_value('Counter64', (1 << 40) + 1)
        assert c64[0] == TAG_COUNTER64 and int.from_bytes(c64[2:], 'big') == (1 << 40) + 1

    def test_counter32_wraps_in_mib_but_hc_does_not(self, lab):
        _c, r1, _r2 = lab
        dp_engine.bump(r1, 'GigabitEthernet0/0/1', 'in', pkts=1, size=(1 << 32) + 7)
        mib = _mib(r1)
        i = _if_index(mib, 'GigabitEthernet0/0/1')
        hc = int(mib[f'1.3.6.1.2.1.31.1.1.1.6.{i}'][1])
        assert hc >= (1 << 32) + 7
        assert int(mib[f'1.3.6.1.2.1.2.2.1.10.{i}'][1]) == hc % (1 << 32)


class TestPingDrivesCounters:
    def _octets(self, dev, ifn):
        c = dp_engine.get_counter(dev, ifn)
        return c['in_bytes'], c['out_bytes']

    def test_repeat_and_size_are_honored(self, lab):
        c, r1, r2 = lab
        before_r1 = self._octets(r1, 'GigabitEthernet0/0/0')
        before_r2 = self._octets(r2, 'GigabitEthernet0/0/0')
        out = _cli(c, r1, 'ping 10.241.2.1 repeat 100 size 1500')
        assert 'Sending 100, 1500-byte ICMP Echos' in out
        assert 'Success rate is 100 percent (100/100)' in out
        assert out.count('!') == 100 and '!' * 71 not in out          # IOSは70個で折り返す
        after_r1 = self._octets(r1, 'GigabitEthernet0/0/0')
        after_r2 = self._octets(r2, 'GigabitEthernet0/0/0')
        # 往路(r1 out / r2 in)と復路(r2 out / r1 in)の両方
        assert after_r1[1] - before_r1[1] == 150000
        assert after_r1[0] - before_r1[0] == 150000
        assert after_r2[0] - before_r2[0] == 150000
        assert after_r2[1] - before_r2[1] == 150000

    def test_counters_only_on_path_links(self, lab):
        c, r1, _r2 = lab
        lan_before = self._octets(r1, 'GigabitEthernet0/0/1')
        _cli(c, r1, 'ping 10.241.2.1 repeat 50 size 1000')
        # r2 宛てのpingで、無関係なLAN側(pc向け)のカウンタは動かない
        assert self._octets(r1, 'GigabitEthernet0/0/1') == lan_before

    def test_default_ping_unchanged(self, lab):
        c, r1, _r2 = lab
        out = _cli(c, r1, 'ping 10.241.2.1')
        assert 'Sending 5, 100-byte ICMP Echos' in out and '(5/5)' in out


def test_cpu_walk_is_time_based_not_poll_based():
    dev = f'ctm-cpu-{_RUN}'
    snmp_agent.register(dev, 'cisco', 'CPU')
    try:
        vals = {snmp_agent._cpu_percent(dev, 'cisco') for _ in range(500)}
        assert len(vals) == 1     # 5秒以内に何百回ポーリングされても値は動かない
    finally:
        snmp_agent.devices.pop(dev, None)
        snmp_agent._cpu_state.pop(dev, None)
        snmp_agent._cpu_last.pop(dev, None)


class TestIacDefinitions:
    """iac/cisco-traffic-monitoring の定義ファイル同士の整合"""

    @pytest.fixture(scope='class')
    def snmp_cfg(self):
        return yaml.safe_load(open(os.path.join(IAC, 'snmp_exporter', 'snmp.yml')))

    def _exprs(self):
        dash = json.load(open(os.path.join(IAC, 'grafana', 'dashboards', 'cisco-traffic.json')))
        exprs = [t['expr'] for p in dash['panels'] for t in p.get('targets', [])]
        exprs += [v['query']['query'] for v in dash['templating']['list']]
        rules = yaml.safe_load(open(os.path.join(IAC, 'prometheus', 'alert_rules.yml')))
        exprs += [r['expr'] for g in rules['groups'] for r in g['rules']]
        return exprs

    def test_queried_metrics_are_exported(self, snmp_cfg):
        defined = {m['name'] for m in snmp_cfg['modules']['cisco_if']['metrics']} | {'up'}
        used = set()
        for e in self._exprs():
            used |= set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\{', e))
        assert used and used <= defined, used - defined

    def test_walked_oids_cover_metric_oids(self, snmp_cfg):
        mod = snmp_cfg['modules']['cisco_if']
        roots = mod['walk'] + [o.rsplit('.', 1)[0] for o in mod['get']]
        for m in mod['metrics']:
            assert any(m['oid'] == r or m['oid'].startswith(r + '.') for r in roots), m['name']
            for lk in m.get('lookups', []):
                assert lk['oid'] in mod['walk'], lk

    def test_community_and_targets_match_lab_script(self, snmp_cfg):
        script = open(os.path.join(IAC, 'lab', 'provision_cisco_lab.sh')).read()
        community = snmp_cfg['auths']['netlab_v2']['community']
        assert f'snmp-server community {community} RO' in script
        targets = yaml.safe_load(open(os.path.join(IAC, 'prometheus', 'targets', 'cisco_routers.yml')))
        for grp in targets:
            for ip in grp['targets']:
                assert f'ip address {ip} ' in script, ip
        prom = yaml.safe_load(open(os.path.join(IAC, 'prometheus', 'prometheus.yml')))
        job = next(j for j in prom['scrape_configs'] if j['job_name'] == 'cisco-snmp')
        assert job['params'] == {'module': ['cisco_if'], 'auth': ['netlab_v2']}

    def test_grafana_datasource_uid_matches_dashboard(self):
        ds = yaml.safe_load(open(os.path.join(IAC, 'grafana', 'provisioning', 'datasources',
                                              'prometheus.yml')))
        uid = ds['datasources'][0]['uid']
        dash = json.load(open(os.path.join(IAC, 'grafana', 'dashboards', 'cisco-traffic.json')))
        assert {p['datasource']['uid'] for p in dash['panels']} == {uid}
