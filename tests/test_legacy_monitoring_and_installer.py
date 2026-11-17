"""
昔ながらの監視(MRTG・Cacti)の取り込み、インストーラー(監視の仕組みを探して設定を作る)、
SNMP エージェントの直した点(負の INTEGER・ipAddrTable)

外部ツールは使わない(rrdtool があれば Cacti / MRTG の rrd 形式も実ファイルで確かめる)。
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SA = os.path.join(ROOT, 'cyber-view-standalone')
sys.path.insert(0, SA)
sys.path.insert(0, ROOT)
from cvs import config as sa_config  # noqa: E402
from cvs import installer  # noqa: E402
from cvs.legacy_sources import LegacyRates, parse_mrtg_cfg, read_mrtg_log, rrd_last  # noqa: E402

# cfgmaker が実際に出した形(エミュレータの CR1 に対して実行した結果を縮めたもの)
MRTG_CFG = """### Global Config Options
WorkDir: {wd}
Options[_]: growright, bits
EnableIPv6: no
### Interface 1 >> Descr: 'GigabitEthernet0/0/0' | Name: 'Gi0/0/0' | Ip: '10.240.12.1' | Eth: 'No Ethernet Id' ###
Target[10.240.12.1_Gi0_0_0]: #Gi0/0/0:netlab-ro@10.240.12.1:
SetEnv[10.240.12.1_Gi0_0_0]: MRTG_INT_IP="10.240.12.1" MRTG_INT_DESCR="GigabitEthernet0/0/0"
MaxBytes[10.240.12.1_Gi0_0_0]: 125000000
Title[10.240.12.1_Gi0_0_0]: Traffic Analysis for Gi0/0/0 -- CR1
Target[10.240.12.1_Gi0_0_1]: #Gi0/0/1:netlab-ro@10.240.12.1:
SetEnv[10.240.12.1_Gi0_0_1]: MRTG_INT_IP="10.240.1.1" MRTG_INT_DESCR="GigabitEthernet0/0/1"
Title[10.240.12.1_Gi0_0_1]: Traffic Analysis for Gi0/0/1 -- CR1
Target[10.240.12.2_Gi0_0_0]: #Gi0/0/0:netlab-ro@10.240.12.2:
SetEnv[10.240.12.2_Gi0_0_0]: MRTG_INT_IP="10.240.12.2" MRTG_INT_DESCR="GigabitEthernet0/0/0"
Title[10.240.12.2_Gi0_0_0]: Traffic Analysis for Gi0/0/0 -- CR2
Target[legacy]: 3:public@10.9.9.9:
"""

CFG = {'devices': [
    {'id': 'cr1', 'hostname': 'CR1', 'type': 'cisco', 'mgmt_ip': '10.240.12.1',
     'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.240.12.1', 'prefix': 30}, {'name': 'GigabitEthernet0/0/1', 'ip': '10.240.1.1'}]},
    {'id': 'cr2', 'hostname': 'CR2', 'type': 'cisco', 'mgmt_ip': '10.240.12.2',
     'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.240.12.2', 'prefix': 30}]}],
       'links': [{'a': 'cr1', 'a_if': 'GigabitEthernet0/0/0', 'b': 'cr2', 'b_if': 'GigabitEthernet0/0/0'}]}


def _mrtg_files(tmp_path, now):
    wd = tmp_path / 'mrtg'
    wd.mkdir()
    (tmp_path / 'mrtg.cfg').write_text(MRTG_CFG.format(wd=wd))
    # MRTG の .log: 1行目は生カウンタ、2行目以降は 時刻 平均入力 平均出力 最大入力 最大出力(バイト/秒)
    (wd / '10.240.12.1_gi0_0_0.log').write_text(f'{now} 999 999\n{now} 1185184 501600 1185184 501600\n{now - 300} 1 1 1 1\n')
    (wd / '10.240.12.2_gi0_0_0.log').write_text(f'{now - 5000} 1 1\n{now - 5000} 100 100 100 100\n')   # 古い → 出さない
    return tmp_path / 'mrtg.cfg'


def test_parse_mrtg_cfg_and_log(tmp_path):
    now = int(time.time())
    cfgp = _mrtg_files(tmp_path, now)
    p = parse_mrtg_cfg(cfgp.read_text())
    t = p['targets']['10.240.12.1_Gi0_0_0']
    assert p['workdir'].endswith('mrtg') and p['logformat'] == 'mrtg'
    assert t == {'host': '10.240.12.1', 'descr': 'GigabitEthernet0/0/0', 'ip': '10.240.12.1', 'title': 'Traffic Analysis for Gi0/0/0 -- CR1'}
    assert p['targets']['legacy'] == {'host': '10.9.9.9', 'ifindex': 3}
    assert read_mrtg_log(str(tmp_path / 'mrtg' / '10.240.12.1_gi0_0_0.log')) == (now, 1185184 * 8, 501600 * 8)


def test_legacy_rates_mrtg_auto_mapping_and_stale(tmp_path):
    now = int(time.time())
    cfgp = _mrtg_files(tmp_path, now)
    cfg = dict(CFG, sources={'mrtg': {'cfg': str(cfgp)}})
    look = {'10.240.12.1': 'cr1', '10.240.12.2': 'cr2', 'cr1': 'cr1', 'cr2': 'cr2'}
    lr = LegacyRates(cfg, {d['id']: d for d in CFG['devices']}, lambda k: look.get(k.lower()))
    r = lr.rates(now)
    assert r[('cr1', 'gi0/0/0')][:3] == (1185184 * 8, 501600 * 8, 'mrtg')
    assert ('cr2', 'gi0/0/0') not in r                                  # 更新が止まった MRTG を「0」と出さない
    assert ('cr1', 'GigabitEthernet0/0/1', '10.240.12.1_Gi0_0_1') in lr.mapping['mrtg']
    assert not any(m[2] == 'legacy' for m in lr.mapping['mrtg'])        # 知らない装置は対応づけない


@pytest.mark.skipif(not shutil.which('rrdtool'), reason='rrdtool がありません')
def test_cacti_rrd_and_collector_uses_it(tmp_path):
    rra = tmp_path / 'rra'
    rra.mkdir()
    f = rra / 'cr2_traffic_in_7.rrd'
    start = int(time.time()) - 600
    subprocess.run(['rrdtool', 'create', str(f), '--start', str(start - 60), '--step', '60', 'DS:traffic_in:COUNTER:120:0:U',
                    'DS:traffic_out:COUNTER:120:0:U', 'RRA:AVERAGE:0.5:1:600'], check=True)
    for k in range(11):                                                  # Cacti の poller と同じく生カウンタを書く
        subprocess.run(['rrdtool', 'update', str(f), f'{start + k * 60}:{k * 600000}:{k * 120000}'], check=True)
    t, i, o = rrd_last(str(f), 'traffic_in', 'traffic_out', seconds=900)
    assert abs(i - 10000 * 8) < 1 and abs(o - 2000 * 8) < 1
    with pytest.raises(RuntimeError):
        rrd_last(str(f), 'ds0', 'ds1')
    cfg = sa_config.validate(dict(CFG, sources={'cacti': {'rra_dir': str(rra), 'targets': [
        {'file': 'cr2_traffic_in_7.rrd', 'device': 'cr2', 'if': 'Gi0/0/0'}, {'file': 'missing.rrd', 'device': 'cr1', 'if': 'Gi0/0/0'}]}}))
    from cvs.collector import Collector
    col = Collector(cfg)
    ln = col._build(60)['links'][0]
    assert ln['source'] == 'cacti' and abs(ln['ba_bps'] - 16000) < 1 and abs(ln['ab_bps'] - 80000) < 1   # 片側だけでも反対向きを埋める
    assert 'missing.rrd' in col.errors['cacti']


def test_installer_detects_and_writes_config(tmp_path):
    now = int(time.time())
    cfgp = _mrtg_files(tmp_path, now)
    rra = tmp_path / 'rra'
    rra.mkdir()
    (rra / 'cr2_traffic_in_7.rrd').write_text('x')

    def get(url, timeout=3):
        if '/api/v1/status/buildinfo' in url:
            return {'status': 'success', 'data': {'version': '2.54.1'}}
        if '/api/v1/targets' in url:
            return {'status': 'success', 'data': {'activeTargets': [{'labels': {'job': 'cisco-snmp'}}, {'labels': {'job': 'prometheus'}}]}}
        if '/api/v1/query' in url:
            return {'status': 'success', 'data': {'result': [
                {'metric': {'instance': '10.240.12.3', 'ifName': 'Gi0/0/0', 'sysName': 'CR3'}, 'value': [0, '1']}]}}
        if url.endswith('/ready'):
            return 'ready'
        raise OSError('connection refused')
    f = installer.detect(mrtg_cfgs=[str(cfgp)], cacti_dirs=[str(rra)], get=get, env={'ANTHROPIC_API_KEY': 'x'})
    assert f['prometheus']['job'] == 'cisco-snmp' and f['loki'] and f['ai'] == 'claude' and f['ollama'] is None
    assert {d['hostname'] for d in f['devices'].values()} == {'CR1', 'CR2', 'CR3', '10.9.9.9'}
    cfg = installer.build_config(f)
    by = {d['hostname']: d for d in cfg['devices']}
    assert by['CR1']['interfaces'][0] == {'name': 'GigabitEthernet0/0/0', 'ip': '10.240.12.1', 'prefix': 30}   # 対向がある → /30
    assert by['CR1']['interfaces'][1]['prefix'] == 24
    assert cfg['links'] == [{'a': 'cr1', 'a_if': 'GigabitEthernet0/0/0', 'b': 'cr2', 'b_if': 'GigabitEthernet0/0/0'}]
    assert set(cfg['sources']) == {'prometheus', 'loki', 'mrtg', 'cacti'}
    text = installer.render_yaml(cfg, f)
    assert 'cr2_traffic_in_7.rrd' in text and 'claude' in text
    out = tmp_path / 'config.yaml'
    out.write_text(text)
    assert sa_config.load(str(out))['sources']['prometheus']['job'] == 'cisco-snmp'
    assert installer.main(['--write', str(out), '--mrtg-cfg', str(cfgp), '--prometheus', 'http://127.0.0.1:9', '--loki',
                           'http://127.0.0.1:9', '--ollama', 'http://127.0.0.1:9']) == 3      # 既存は上書きしない
    empty = tmp_path / 'none.yaml'
    assert installer.main(['--write', str(empty), '--mrtg-cfg', str(tmp_path / 'nope.cfg'), '--prometheus', 'http://127.0.0.1:9',
                           '--loki', 'http://127.0.0.1:9', '--ollama', 'http://127.0.0.1:9', '--cacti-rra', str(tmp_path / 'x')]) == 2


def test_install_script_is_valid_bash():
    r = subprocess.run(['bash', '-n', os.path.join(SA, 'install.sh')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.access(os.path.join(SA, 'install.sh'), os.X_OK)


def test_snmp_negative_request_id_and_ipaddrtable():
    from engine.snmp_udp_agent import _decode_int, _encode_int
    for n in (-1488714757, -1, -128, -129, -2 ** 31, 0, 127, 128, 2 ** 31 - 1):
        b = _encode_int(n)
        assert _decode_int(b[2:]) == n
    assert _encode_int(-1488714757).hex() == '0204a74403fb'               # 以前は例外で応答しなかった(cfgmaker が「応答なし」)
    from engine.protocols import icmp_engine, snmp_agent
    icmp_engine.register_device('ipad-t', 'IPAD-T', {'GigabitEthernet0/0/0': {'ip': '10.252.1.1', 'prefix': 30},
                                                     'GigabitEthernet0/0/1': {'ip': '10.252.2.1', 'prefix': 24}})
    mib = {o: v for o, _t, v in snmp_agent._build_mib('ipad-t')}
    assert mib['1.3.6.1.2.1.4.20.1.2.10.252.2.1'] == '2' and mib['1.3.6.1.2.1.4.20.1.3.10.252.1.1'] == '255.255.255.252'
    assert mib['1.3.6.1.2.1.4.20.1.3.10.252.2.1'] == '255.255.255.0'
