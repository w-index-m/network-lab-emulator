"""Cacti の DB から「RRD → 装置・IF」を自動で対応づける(cvs/cacti_db.py)

- DB 接続情報(PHP の `$database_xxx`)の読み取り: Debian の debian.php、RHEL の db.php、引用符・数字
- mysql クライアントの呼び出し: パスワードをコマンドラインに載せず MYSQL_PWD で渡す。失敗は理由つきのエラー
- 対応づけ: ホスト(IP / 説明)と ifName / ifDescr / ifIP を、設定の装置・IF と照合。できないものは理由つきで知らせる
- LegacyRates: 本物の rrdtool で作った RRD から流量を読む。targets の手書きが優先、DB の結果は使い回し、
  DB が落ちても前回の対応で流量を出し続ける
- インストーラー: DB 接続情報を探し、サブディレクトリの RRD も数える
- 使う SQL が読み取り(SELECT)だけで、Cacti 1.2.26 のテーブル・列を使っていること

本物の MariaDB + Cacti 1.2.26 の cacti.sql での確認は docs/nirvana-data-ingestion.md に記録。
"""
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'cyber-view-standalone'))
from cvs import cacti_db, installer  # noqa: E402
from cvs.legacy_sources import LegacyRates  # noqa: E402

needs_rrdtool = pytest.mark.skipif(not shutil.which('rrdtool'), reason='rrdtool が無い')

DEBIAN_PHP = """<?php
$database_type     = 'mysql';
$database_default  = 'cacti';
$database_hostname = 'db.example.local';
$database_username = 'cactiuser';
$database_password = 'p@ss;word';
$database_port     = '3307';
$database_retries  = 5;
"""
RHEL_DB_PHP = """<?php
$database_type = "mysql";
$database_default = "cacti_prod";
$database_hostname = "localhost";
$database_username = "cacti";
$database_password = "x y";
$database_port = 3306;
"""


def test_read_db_config_debian_and_rhel_styles(tmp_path):
    a = tmp_path / 'debian.php'
    a.write_text(DEBIAN_PHP)
    assert cacti_db.read_db_config(str(a)) == {'host': 'db.example.local', 'user': 'cactiuser', 'password': 'p@ss;word',
                                               'database': 'cacti', 'port': 3307}
    b = tmp_path / 'db.php'
    b.write_text(RHEL_DB_PHP)
    c = cacti_db.read_db_config(str(b))
    assert (c['database'], c['host'], c['port'], c['password']) == ('cacti_prod', 'localhost', 3306, 'x y')
    bad = tmp_path / 'other.php'
    bad.write_text('<?php $x = 1;')
    with pytest.raises(ValueError, match='database_username'):
        cacti_db.read_db_config(str(bad))


def test_find_db_config_takes_first_readable(tmp_path):
    bad, good = tmp_path / 'bad.php', tmp_path / 'good.php'
    bad.write_text('<?php // 空')
    good.write_text(DEBIAN_PHP)
    p, c = cacti_db.find_db_config([str(tmp_path / 'none.php'), str(bad), str(good)])
    assert p == str(good) and c['user'] == 'cactiuser'
    assert cacti_db.find_db_config([str(tmp_path / 'none.php')]) == (None, None)


def _fake_mysql(tmp_path, body):
    exe = tmp_path / 'bin' / 'mysql'
    exe.parent.mkdir()
    exe.write_text('#!/bin/sh\n' + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe.parent)


def test_run_query_passes_password_by_env_not_argv(tmp_path, monkeypatch):
    out = tmp_path / 'seen.txt'
    d = _fake_mysql(tmp_path, f'echo "$@" > {out}\necho "PWD=$MYSQL_PWD" >> {out}\nprintf "h1\\t3\\tx\\n\\nh2\\t4\\ty\\n"\n')
    monkeypatch.setenv('PATH', d + os.pathsep + os.environ['PATH'])
    rows = cacti_db.run_query({'host': 'db', 'port': 3307, 'user': 'u', 'password': 'secret-pw', 'database': 'cacti'}, 'SELECT 1')
    assert rows == [['h1', '3', 'x'], ['h2', '4', 'y']]
    seen = out.read_text()
    assert '-h db' in seen and '-P 3307' in seen and '-u u' in seen and 'cacti' in seen and '--batch' in seen
    assert 'secret-pw' not in seen.splitlines()[0] and 'PWD=secret-pw' in seen


def test_run_query_errors_are_explained(tmp_path, monkeypatch):
    d = _fake_mysql(tmp_path, 'echo "ERROR 1045 (28000): Access denied for user" >&2\nexit 1\n')
    monkeypatch.setenv('PATH', d + os.pathsep + os.environ['PATH'])
    with pytest.raises(RuntimeError, match='Cacti の DB に繋がりません: ERROR 1045'):
        cacti_db.run_query({'user': 'u', 'database': 'cacti'}, 'SELECT 1')
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    with pytest.raises(RuntimeError, match='mysql クライアントが見つかりません'):
        cacti_db.run_query({'user': 'u', 'database': 'cacti'}, 'SELECT 1')


def test_sql_is_read_only_and_uses_cacti_1_2_tables():
    low = cacti_db.SQL.lower()
    assert low.strip().startswith('select') and not any(w in low for w in ('insert ', 'update ', 'delete ', 'drop ', 'alter '))
    for t in ('host', 'data_local', 'data_template_data', 'data_template_rrd', 'host_snmp_cache'):
        assert t in low
    for col in ('data_source_path', 'snmp_index', 'snmp_query_id', 'field_name', 'field_value', 'data_source_name'):
        assert col in low
    assert 'traffic_in' in cacti_db.SQL and 'traffic_out' in cacti_db.SQL


DEVS = {'cr1': {'id': 'cr1', 'hostname': 'CR1', 'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.240.12.1'},
                                                                {'name': 'GigabitEthernet0/0/1', 'ip': '10.240.1.1'}]},
        'cr2': {'id': 'cr2', 'hostname': 'CR2', 'interfaces': [{'name': 'GigabitEthernet0/0/0', 'ip': '10.240.12.2'}]}}
KEYS = {'10.240.12.1': 'cr1', 'CR1': 'cr1', 'cr1': 'cr1', '10.240.12.2': 'cr2', 'CR2': 'cr2', 'cr2': 'cr2'}


def _r(host, desc, idx, path, name='', descr='', ip=''):
    return {'host': host, 'description': desc, 'ifindex': idx, 'path': path, 'if_name': name, 'if_descr': descr, 'if_ip': ip}


def test_map_rrds_matches_by_host_and_interface_names():
    rrds = [_r('10.240.12.1', 'CR1', 1, '/r/1/11.rrd', name='Gi0/0/0'),                      # ifName の略記
            _r('router-x', 'CR1', 2, '/r/1/12.rrd', descr='GigabitEthernet0/0/1'),          # 説明で装置、ifDescr で IF
            _r('10.240.12.2', 'x', 1, '/r/2/21.rrd', ip='10.240.12.2'),                       # ifIP だけ
            _r('10.9.9.9', 'OLD-SW', 1, '/r/3/31.rrd', name='Fa0/1'),                         # 装置が設定に無い
            _r('10.240.12.1', 'CR1', 9, '/r/1/19.rrd', name='Gi0/0/9')]                       # IF が設定に無い
    items, bad = cacti_db.map_rrds(rrds, DEVS, KEYS.get)
    assert items == [('cr1', 'GigabitEthernet0/0/0', '/r/1/11.rrd'), ('cr1', 'GigabitEthernet0/0/1', '/r/1/12.rrd'),
                     ('cr2', 'GigabitEthernet0/0/0', '/r/2/21.rrd')]
    assert len(bad) == 2 and '装置が設定に無い: 10.9.9.9' in bad[0] and 'cr1 にそのインタフェースが無い' in bad[1]


def test_load_rrds_replaces_path_rra_and_reads_settings_when_no_rra_dir(tmp_path):
    p = tmp_path / 'db.php'
    p.write_text(DEBIAN_PHP)
    calls = []

    def q(conf, sql):
        calls.append(sql)
        if 'path_rra' in sql:
            return [['/var/lib/cacti/rra']]
        return [['10.240.12.1', 'CR1', '1', '<path_rra>/1/11.rrd', 'Gi0/0/0', 'GigabitEthernet0/0/0', '10.240.12.1']]
    r = cacti_db.load_rrds({'config': str(p)}, None, query=q)
    assert r[0]['path'] == '/var/lib/cacti/rra/1/11.rrd' and r[0]['ifindex'] == 1 and len(calls) == 2
    calls.clear()
    r = cacti_db.load_rrds({'config': str(p)}, '/mnt/rra/', query=q)             # rra_dir を書けばそちらを使う
    assert r[0]['path'] == '/mnt/rra/1/11.rrd' and len(calls) == 1


def test_resolve_conf_direct_values_win_and_missing_file_is_explained(tmp_path):
    p = tmp_path / 'db.php'
    p.write_text(DEBIAN_PHP)
    c = cacti_db.resolve_conf({'config': str(p), 'host': '127.0.0.1', 'password': 'other'})
    assert c['host'] == '127.0.0.1' and c['password'] == 'other' and c['user'] == 'cactiuser'
    with pytest.raises(FileNotFoundError, match='/etc/cacti/db.php'):
        cacti_db.resolve_conf({'config': str(tmp_path / 'nope.php')})
    assert cacti_db.resolve_conf({'user': 'u'})['user'] == 'u'


def _rrd(path, in_bps, out_bps):
    now = int(time.time())
    start = now - 1200
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(['rrdtool', 'create', path, '--start', str(start), '--step', '60', 'DS:traffic_in:COUNTER:600:0:U',
                    'DS:traffic_out:COUNTER:600:0:U', 'RRA:AVERAGE:0.5:1:100'], check=True)
    ci = co = 0
    for t in range(start + 60, now - 30, 60):
        ci += int(in_bps / 8 * 60)
        co += int(out_bps / 8 * 60)
        subprocess.run(['rrdtool', 'update', path, f'{t}:{ci}:{co}'], check=True)


@needs_rrdtool
def test_legacy_rates_reads_real_rrd_via_db_mapping_and_handles_outage(tmp_path, monkeypatch):
    _rrd(str(tmp_path / '1' / '11.rrd'), 2_000_000, 1_920_000)
    _rrd(str(tmp_path / '2' / '21.rrd'), 1_000_000, 1_000_000)
    p = tmp_path / 'db.php'
    p.write_text(DEBIAN_PHP)
    state = {'down': False, 'queries': 0}

    def fake_query(conf, sql):
        state['queries'] += 1
        if state['down']:
            raise RuntimeError('Cacti の DB に繋がりません: test')
        if 'path_rra' in sql:
            return [[str(tmp_path)]]
        return [['10.240.12.1', 'CR1', '1', '<path_rra>/1/11.rrd', 'Gi0/0/0', '', ''],
                ['10.240.12.2', 'CR2', '1', '<path_rra>/2/21.rrd', 'Gi0/0/0', '', ''],
                ['10.9.9.9', 'OLD-SW', '1', '<path_rra>/3/31.rrd', 'Fa0/1', '', '']]
    monkeypatch.setattr(cacti_db, 'run_query', fake_query)
    lr = LegacyRates({'sources': {'cacti': {'db': {'config': str(p)}, 'stale': 3600, 'db_ttl': 300}}}, DEVS, KEYS.get)
    r = lr.rates(time.time())
    assert r[('cr1', 'gi0/0/0')][0] == pytest.approx(2_000_000, rel=0.02) and r[('cr1', 'gi0/0/0')][1] == pytest.approx(1_920_000, rel=0.02)
    assert r[('cr2', 'gi0/0/0')][0] == pytest.approx(1_000_000, rel=0.02) and r[('cr2', 'gi0/0/0')][2] == 'cacti'
    assert lr.errors == {} and '対応づけ 2 本' in lr.notes['cacti'] and 'OLD-SW' in lr.notes['cacti']
    n = state['queries']
    lr.rates(time.time())
    assert state['queries'] == n                                  # db_ttl の間は DB を引き直さない
    lr.cacti['db_ttl'] = 0
    state['down'] = True                                          # DB が落ちても、前回の対応で流量は出し続ける
    r2 = lr.rates(time.time())
    assert ('cr1', 'gi0/0/0') in r2 and 'cacti_db' in lr.errors
    state['down'] = False
    lr.rates(time.time())
    assert 'cacti_db' not in lr.errors


@needs_rrdtool
def test_explicit_targets_win_over_db(tmp_path, monkeypatch):
    _rrd(str(tmp_path / '1' / '11.rrd'), 2_000_000, 2_000_000)
    _rrd(str(tmp_path / 'manual.rrd'), 5_000_000, 5_000_000)
    monkeypatch.setattr(cacti_db, 'run_query', lambda conf, sql: [[str(tmp_path)]] if 'path_rra' in sql else
                        [['10.240.12.1', 'CR1', '1', '<path_rra>/1/11.rrd', 'Gi0/0/0', '', '']])
    lr = LegacyRates({'sources': {'cacti': {'rra_dir': str(tmp_path), 'stale': 3600, 'db': {'user': 'u'},
                                            'targets': [{'device': 'cr1', 'if': 'GigabitEthernet0/0/0', 'file': 'manual.rrd'}]}}},
                     DEVS, KEYS.get)
    assert lr.rates(time.time())[('cr1', 'gi0/0/0')][0] == pytest.approx(5_000_000, rel=0.02)


def test_db_empty_dict_means_auto_find_config_and_absent_means_off(tmp_path, monkeypatch):
    monkeypatch.setattr(cacti_db, 'run_query', lambda c, s: [])
    monkeypatch.setattr(cacti_db, 'find_db_config', lambda c=None: ('/x', {'user': 'u', 'database': 'cacti'}))
    on = LegacyRates({'sources': {'cacti': {'rra_dir': str(tmp_path), 'db': {}}}}, DEVS, KEYS.get)
    on.rates(time.time())
    assert 'cacti' in on.notes                                   # DB を引いた(0 本でも結果が notes に出る)
    off = LegacyRates({'sources': {'cacti': {'rra_dir': str(tmp_path)}}}, DEVS, KEYS.get)
    off.rates(time.time())
    assert off.notes == {}


def test_installer_finds_db_config_and_counts_nested_rrds(tmp_path):
    (tmp_path / 'rra' / '1').mkdir(parents=True)
    (tmp_path / 'rra' / '1' / '11.rrd').write_text('x')
    (tmp_path / 'rra' / '1' / '12.rrd').write_text('x')
    cfgp = tmp_path / 'debian.php'
    cfgp.write_text(DEBIAN_PHP)
    f = installer.detect(cacti_dirs=[str(tmp_path / 'rra')], cacti_db_configs=[str(tmp_path / 'none.php'), str(cfgp)],
                         ollama='http://127.0.0.1:9', env={}, get=lambda u: None)
    assert f['cacti'][0]['count'] == 2 and f['cacti'][0]['files'] == ['1/11.rrd', '1/12.rrd']
    assert f['cacti_db']['config'] == str(cfgp) and f['cacti_db']['user'] == 'cactiuser'
    text = installer.summary(f)
    assert 'Cacti      : ' in text and '(2 RRD)' in text and f'Cacti DB   : {cfgp}' in text
    assert f'Cacti DB 候補 {tmp_path / "none.php"}: 無い' in text
    cfg = installer.build_config(f, 't')
    assert cfg['sources']['cacti']['db'] == {'config': str(cfgp)} and 'targets' not in cfg['sources']['cacti']
    assert 'password' not in str(cfg['sources']['cacti'])                       # パスワードは設定に書かない
    assert 'Cacti の DB から自動で読む' in installer.render_yaml(cfg, f)


def test_installer_without_db_config_keeps_manual_targets_hint(tmp_path):
    (tmp_path / 'rra').mkdir()
    (tmp_path / 'rra' / 'cr1_traffic_in_12.rrd').write_text('x')
    f = installer.detect(cacti_dirs=[str(tmp_path / 'rra')], cacti_db_configs=[], ollama='http://127.0.0.1:9', env={}, get=lambda u: None)
    cfg = installer.build_config(f, 't')
    assert cfg['sources']['cacti'] == {'rra_dir': str(tmp_path / 'rra'), 'targets': []}
    assert 'sources.cacti.targets を手で書く' in installer.render_yaml(cfg, f)
