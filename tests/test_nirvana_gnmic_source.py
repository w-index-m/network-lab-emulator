"""
ニルヴァーナ単体版: gNMI の値を gnmic(prometheus 出力)→ Prometheus 経由で読む

- flavor=gnmic: interfaces_interface_state_counters_in/out_octets(ラベル source・interface_name)から流量・カウンタ、
  oper_status ラベル(strings-as-labels)から POLL の up/down
- flavor=both: 同じ装置・IF は snmp_exporter を優先し、無いところを gnmic で埋める
- gnmic だけのときは「応答なし」の判定をしない(装置ごとの up が無い)
- installer が gnmic の系列を見つけて flavor / gnmic_job を書く

PromQL そのもの(topk + timestamp で新しい oper_status を選ぶ)は、本物の Prometheus 2.54 + gnmic +
ラボの gNMI で確かめた(docs/nirvana-gnmi-via-gnmic.md)。ここでは問い合わせの組み立てと結果の読み方を固定する。
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
from cvs import installer  # noqa: E402
from cvs import sources as S  # noqa: E402


def _row(metric, value):
    return {'metric': metric, 'value': [0, str(value)]}


def _g(src, ifn, **extra):
    return {'source': src, 'interface_name': ifn, 'job': 'gnmic', **extra}


def _s(inst, ifn):
    return {'instance': inst, 'ifName': ifn, 'job': 'cisco-snmp'}


class FakeProm(S.Prometheus):
    """問い合わせ文字列の中身で返す結果を選ぶ"""

    def __init__(self, table, **kw):
        super().__init__('http://prom', **kw)
        self.table, self.asked = table, []

    def query(self, q):
        self.asked.append(q)
        for key, rows in self.table.items():
            if key(q):
                return rows
        return []


def _gnmic_table():
    return {
        lambda q: q.startswith('rate(max by (source, interface_name) (interfaces_interface_state_counters_in_octets{job="gnmic"})'):
            [_row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1'), 8000)],
        lambda q: q.startswith('rate(max by (source, interface_name) (interfaces_interface_state_counters_out_octets{job="gnmic"})'):
            [_row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1'), 16000)],
        lambda q: q.startswith('max by (source, interface_name) (interfaces_interface_state_counters_in_octets{job="gnmic"})'):
            [_row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1'), 46608)],
        lambda q: q.startswith('max by (source, interface_name) (interfaces_interface_state_counters_out_octets{job="gnmic"})'):
            [_row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1'), 29318)],
        lambda q: 'interfaces_interface_state_oper_status' in q: [
            _row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1', oper_status='UP'), 1),
            _row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/2', oper_status='DOWN'), 1),
            _row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/3', oper_status='LOWER_LAYER_DOWN'), 1),
            _row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/4', oper_status='TESTING'), 1)],
    }


def test_gnmic_flavor_reads_rates_counters_and_oper_status():
    p = FakeProm(_gnmic_table(), flavor='gnmic')
    k = ('192.168.1.1', 'gi1/0/1')                      # ポートを外した IP と短縮した IF 名
    assert p.rates(60) == {k: (8000.0, 16000.0)}
    assert p.counters() == {k: (46608.0, 29318.0)}
    assert p.oper_status() == {k: 'up', ('192.168.1.1', 'gi1/0/2'): 'down', ('192.168.1.1', 'gi1/0/3'): 'down'}
    assert p.targets_up() == {}                         # gnmic は装置ごとの up を出さない
    assert not any('ifHCInOctets' in q or 'up{' in q for q in p.asked)
    # oper_status は系列ごとの最新時刻で選ぶ(状態が変わると古い系列も expiration まで残るため)
    oq = next(q for q in p.asked if 'oper_status' in q)
    assert oq.startswith('topk by (source, interface_name) (1, timestamp(') and '== 1' in oq


def test_both_prefers_snmp_and_fills_the_rest_from_gnmic():
    t = _gnmic_table()
    t.update({
        lambda q: q.startswith('rate(ifHCInOctets'): [_row(_s('192.168.1.1:161', 'Gi1/0/1'), 1)],
        lambda q: q.startswith('rate(ifHCOutOctets'): [_row(_s('192.168.1.1:161', 'Gi1/0/1'), 2),
                                                       _row(_s('10.0.0.9', 'Gi0/0'), 3)],
        lambda q: q.startswith('ifOperStatus'): [_row(_s('192.168.1.1', 'Gi1/0/2'), 1)],
        lambda q: q.startswith('up{'): [_row({'instance': '192.168.1.1:161'}, 1)],
    })
    rin = 'rate(max by (source, interface_name) (interfaces_interface_state_counters_in_octets{job="gnmic"})[60s:15s]) * 8'
    t = {k: v for k, v in t.items() if not k(rin)}          # gnmic の受信だけ差し替える
    t[lambda q: q.startswith('rate(max by (source, interface_name) (interfaces_interface_state_counters_in_octets')] = [
        _row(_g('192.168.1.1:50052', 'GigabitEthernet1/0/1'), 8000),
        _row(_g('192.168.1.2:50052', 'GigabitEthernet1/0/1'), 99)]
    p = FakeProm(t, flavor='both', job='cisco-snmp')
    r = p.rates(60)
    assert r[('192.168.1.1', 'gi1/0/1')] == (1.0, 2.0)         # snmp の組をそのまま使う
    assert r[('192.168.1.2', 'gi1/0/1')] == (99.0, None)       # snmp に無いところは gnmic
    assert r[('10.0.0.9', 'gi0/0')] == (None, 3.0)
    assert p.oper_status()[('192.168.1.1', 'gi1/0/2')] == 'up'  # 同じ IF は snmp が勝つ
    assert p.targets_up() == {'192.168.1.1': True}


def test_snmp_flavor_is_unchanged_and_bad_flavor_is_rejected():
    p = FakeProm({lambda q: q.startswith('rate(ifHCInOctets'): [_row(_s('10.1.1.1', 'Gi0/1'), 5)]}, job='cisco-snmp')
    assert p.rates(60) == {('10.1.1.1', 'gi0/1'): (5.0, None)}
    assert not any('interfaces_interface' in q for q in p.asked)
    with pytest.raises(ValueError):
        S.Prometheus('http://prom', flavor='netflow')


def test_gnmic_metric_prefix():
    p = FakeProm({}, flavor='gnmic', gnmic_prefix='cat')
    p.rates(60)
    assert any(q.startswith('rate(max by (source, interface_name) (cat_interfaces_interface_state_counters_in_octets') for q in p.asked)


def test_installer_detects_gnmic_series():
    def get(url):
        if url.endswith('/api/v1/status/buildinfo'):
            return {'data': {'version': '2.54.1'}}
        if '/api/v1/targets' in url:
            return {'data': {'activeTargets': [{'labels': {'job': 'gnmic'}}]}}
        if 'count+by+%28job%29' in url:
            return {'status': 'success', 'data': {'result': [{'metric': {'job': 'gnmic'}}]}}
        if 'count+by+%28source' in url:
            return {'status': 'success', 'data': {'result': [
                {'metric': {'source': '192.168.1.1:50052', 'interface_name': 'GigabitEthernet1/0/1'}}]}}
        raise OSError('unreachable')
    f = installer.detect(prometheus='http://prom:9090', loki='http://x:1', zabbix='http://x:1', ollama='http://x:1',
                         mrtg_cfgs=[], cacti_dirs=[], cacti_db_configs=[], get=get, env={})
    assert f['prometheus']['gnmic_job'] == 'gnmic'
    cfg = installer.build_config(f)
    assert cfg['sources']['prometheus'] == {'url': 'http://prom:9090', 'job': 'snmp', 'flavor': 'gnmic',
                                            'gnmic_job': 'gnmic'}
    d = next(d for d in cfg['devices'] if d['mgmt_ip'] == '192.168.1.1')
    assert [i['name'] for i in d['interfaces']] == ['GigabitEthernet1/0/1']
    assert 'gnmic=gnmic' in installer.summary(f)


def test_example_files_document_the_gnmic_route():
    with open(os.path.join(ROOT, 'cyber-view-standalone', 'config.example.yaml'), encoding='utf-8') as fh:
        cfg = fh.read()
    assert 'flavor:' in cfg and 'gnmic_job' in cfg
    with open(os.path.join(ROOT, 'cyber-view-standalone', 'gnmic.example.yaml'), encoding='utf-8') as fh:
        g = fh.read()
    for needle in ('type: prometheus', 'strings-as-labels: true', 'expiration: 25s', '# export-timestamps: true',
                   '"/interfaces/interface[name=GigabitEthernet1/0/1]/state"', 'has missing keys',
                   'MDT_SUBSCRIPTION-4-TERMINATED', 'up=1'):
        assert needle in g, needle


def test_quickstart_script_writes_16x_safe_configs(tmp_path):
    """gnmi-quickstart.sh config: ポートごとの /state 購読・export-timestamps なし・パスワードは本人だけ・
    ニルヴァーナの設定が読める(ネットワークには出ない)"""
    import subprocess
    import yaml
    sh = os.path.join(ROOT, 'cyber-view-standalone', 'gnmi-quickstart.sh')
    r = subprocess.run(['bash', sh, 'config', '--target', '192.168.1.1,10.0.0.2:57400', '--ports', 'Gi1/0/1,Te1/1/1',
                        '--user', 'admin', '--password', 'p"w', '--dir', str(tmp_path)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    g = yaml.safe_load((tmp_path / 'gnmic.yaml').read_text())
    assert g['password'] == 'p"w' and set(g['targets']) == {'192.168.1.1:50052', '10.0.0.2:57400'}
    assert g['subscriptions']['if-state']['paths'] == [
        '/interfaces/interface[name=GigabitEthernet1/0/1]/state',
        '/interfaces/interface[name=TenGigabitEthernet1/1/1]/state']
    out = g['outputs']['prom']
    assert out['strings-as-labels'] is True and 'export-timestamps' not in out and out['expiration'] == '25s'
    assert oct((tmp_path / 'gnmic.yaml').stat().st_mode & 0o777) == '0o600'
    p = yaml.safe_load((tmp_path / 'prometheus.yml').read_text())
    assert p['scrape_configs'][0]['job_name'] == 'gnmic'
    sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
    from cvs import config as C
    cfg = C.load(str(tmp_path / 'nirvana.yaml'))
    assert [d['mgmt_ip'] for d in cfg['devices']] == ['192.168.1.1', '10.0.0.2']
    assert cfg['sources']['prometheus']['flavor'] == 'gnmic'
