"""
Cacti の DB から「どの RRD がどの装置のどのインタフェースか」を自動で読む

Cacti は、RRD のファイルと装置・インタフェースの対応を自分の DB(MySQL / MariaDB)にだけ持っている。
RRD のファイル名からは分からない(Cacti 1.x は `<path_rra>/<host_id>/<local_data_id>.rrd` のような数字だけの名前)。
そこで DB を読み取り専用で引き、設定に1本ずつ書かなくても対応づけられるようにする。

読むテーブル(Cacti 1.2.26 の cacti.sql で確認):

  host                 id, hostname(IP か名前), description, disabled
  data_local           id, host_id, snmp_query_id, snmp_index(= ifIndex)
  data_template_data   local_data_id, data_source_path(`<path_rra>/…rrd`)
  data_template_rrd    local_data_id, data_source_name(traffic_in / traffic_out)
  host_snmp_cache      host_id, snmp_query_id, snmp_index, field_name(ifName / ifDescr / ifIP), field_value

「traffic_in と traffic_out の両方を持つ RRD」(Interface - Traffic のデータソース)だけを対象にする。

DB へは `mysql`(または `mariadb`)クライアントを呼ぶ。標準ライブラリだけで動かす方針と、rrdtool を呼ぶ既存の作りに
合わせるため。パスワードはコマンドラインに載せず、環境変数 MYSQL_PWD で渡す(ps に出ない)。
"""
import os
import re
import shutil
import subprocess

from .link_evidence import norm_if

# Cacti の DB 接続情報(PHP)の場所の候補。Debian / Ubuntu は /etc/cacti/debian.php(include/config.php はその symlink)、
# RHEL(EPEL)は /etc/cacti/db.php、ソースから入れた場合は <cacti>/include/config.php
DB_CONFIG_CANDIDATES = ['/etc/cacti/db.php', '/etc/cacti/debian.php', '/usr/share/cacti/site/include/config.php',
                        '/var/www/html/cacti/include/config.php', '/usr/share/cacti/include/config.php']

_PHP_VAR = re.compile(r"""^\s*\$database_(\w+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([\w.]+))\s*;""", re.M)

SQL = """
SELECT h.hostname, h.description, dl.snmp_index, dtd.data_source_path,
       COALESCE(MAX(CASE WHEN c.field_name = 'ifName'  THEN c.field_value END), ''),
       COALESCE(MAX(CASE WHEN c.field_name = 'ifDescr' THEN c.field_value END), ''),
       COALESCE(MAX(CASE WHEN c.field_name = 'ifIP'    THEN c.field_value END), '')
FROM data_local dl
JOIN host h ON h.id = dl.host_id
JOIN data_template_data dtd ON dtd.local_data_id = dl.id
LEFT JOIN host_snmp_cache c ON c.host_id = dl.host_id AND c.snmp_query_id = dl.snmp_query_id
     AND c.snmp_index = dl.snmp_index AND c.field_name IN ('ifName', 'ifDescr', 'ifIP')
WHERE dtd.data_source_path <> '' AND dl.snmp_query_id > 0 AND COALESCE(h.disabled, '') <> 'on'
  AND EXISTS (SELECT 1 FROM data_template_rrd r WHERE r.local_data_id = dl.id AND r.data_source_name = 'traffic_in')
  AND EXISTS (SELECT 1 FROM data_template_rrd r WHERE r.local_data_id = dl.id AND r.data_source_name = 'traffic_out')
GROUP BY dl.id, h.hostname, h.description, dl.snmp_index, dtd.data_source_path
ORDER BY h.hostname, dl.snmp_index
"""
SQL_PATH_RRA = "SELECT value FROM settings WHERE name = 'path_rra'"


def read_db_config(path):
    """Cacti の DB 接続情報(PHP の `$database_xxx = '…';`)→ {'host', 'port', 'user', 'password', 'database'}"""
    with open(path, encoding='utf-8', errors='replace') as f:
        text = f.read()
    v = {}
    for m in _PHP_VAR.finditer(text):
        v[m.group(1)] = next((g for g in m.groups()[1:] if g is not None), '')
    out = {'host': v.get('hostname') or 'localhost', 'user': v.get('username') or '',
           'password': v.get('password') or '', 'database': v.get('default') or 'cacti'}
    try:
        out['port'] = int(v.get('port') or 3306)
    except ValueError:
        out['port'] = 3306
    if not out['user']:
        raise ValueError(f'{path} に $database_username がありません(Cacti の DB 接続情報のファイルではない?)')
    return out


def find_db_config(candidates=None):
    """候補のうち、DB 接続情報として読める最初のファイル → (パス, 設定)。無ければ (None, None)"""
    for p in candidates or DB_CONFIG_CANDIDATES:
        try:
            if os.path.exists(p):
                return p, read_db_config(p)
        except (OSError, ValueError):
            continue
    return None, None


def _client():
    return shutil.which('mysql') or shutil.which('mariadb')


def run_query(conf, sql, timeout=15):
    """mysql クライアントで SQL を流し、タブ区切りの行 [[列, …], …] を返す(読み取り専用の SELECT だけを渡すこと)"""
    exe = _client()
    if not exe:
        raise RuntimeError('mysql クライアントが見つかりません(mariadb-client か mysql-client を入れてください)')
    cmd = [exe, '--batch', '--raw', '--skip-column-names', f'--connect-timeout={min(int(timeout), 10)}',
           '-h', str(conf.get('host') or 'localhost'), '-P', str(conf.get('port') or 3306),
           '-u', conf['user'], conf.get('database') or 'cacti', '-e', sql]
    env = dict(os.environ)
    if conf.get('password'):
        env['MYSQL_PWD'] = conf['password']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError('Cacti の DB に繋がりません: ' + (r.stderr.strip().splitlines() or ['mysql が失敗'])[-1][:160])
    return [ln.split('\t') for ln in r.stdout.splitlines() if ln.strip()]


def resolve_conf(db):
    """sources.cacti.db → 接続情報。`config:`(PHP のパス)を読み、host/user/password/database を直接書いたものが優先"""
    db = dict(db or {})
    base = {}
    if db.get('config'):
        if not os.path.exists(db['config']):
            raise FileNotFoundError(f"Cacti の DB 接続情報が見つかりません: {db['config']}(候補: {', '.join(DB_CONFIG_CANDIDATES)})")
        base = read_db_config(db['config'])
    else:
        _p, base = find_db_config()
        base = base or {}
    for k in ('host', 'port', 'user', 'password', 'database'):
        if db.get(k) not in (None, ''):
            base[k] = db[k]
    if not base.get('user'):
        raise ValueError('Cacti の DB 接続情報がありません(sources.cacti.db に config: か user: を書いてください)')
    return base


def load_rrds(db, rra_dir=None, query=None):
    """DB から traffic の RRD 一覧 → [{'host', 'description', 'ifindex', 'path', 'if_name', 'if_descr', 'if_ip'}]。
    path は `<path_rra>` を実際のディレクトリに置き換えたもの(rra_dir が無ければ Cacti の設定 path_rra を使う)"""
    query = query or run_query                    # 呼び出し時に決める(差し替えられるように)
    conf = resolve_conf(db)
    if not rra_dir:
        rows = query(conf, SQL_PATH_RRA)
        rra_dir = rows[0][0] if rows and rows[0] else ''
    out = []
    for r in query(conf, SQL):
        if len(r) < 7:
            continue
        host, desc, idx, path, if_name, if_descr, if_ip = r[:7]
        path = path.replace('<path_rra>', rra_dir.rstrip('/')) if rra_dir else path
        out.append({'host': host, 'description': desc, 'ifindex': int(idx) if idx.isdigit() else None, 'path': path,
                    'if_name': if_name, 'if_descr': if_descr, 'if_ip': if_ip})
    return out


def map_rrds(rrds, devices, lookup):
    """RRD 一覧 → (対応づけできたもの [(装置 id, IF 名, パス)], できなかったもの [理由つきの文字列])。
    装置は Cacti のホスト名(IP か名前)と説明で、IF は ifName / ifDescr / ifIP で、設定の装置・IF と照合する"""
    items, bad = [], []
    for r in rrds:
        dev = lookup(r['host']) or lookup(r['description'])
        label = f"{r['description'] or r['host']} {r['if_name'] or r['if_descr'] or ('ifIndex ' + str(r['ifindex']))}"
        if not dev:
            bad.append(f'{label}(装置が設定に無い: {r["host"]})')
            continue
        ifn = None
        for i in devices[dev].get('interfaces') or []:
            names = {norm_if(x) for x in (r['if_name'], r['if_descr']) if x}
            if (names and norm_if(i['name']) in names) or (r['if_ip'] and i.get('ip') == r['if_ip']):
                ifn = i['name']
                break
        if not ifn:
            bad.append(f'{label}(設定の {dev} にそのインタフェースが無い)')
            continue
        items.append((dev, ifn, r['path']))
    return items, bad
