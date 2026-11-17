"""
インストーラーの中身: いま動いている監視の仕組みを探して、Cyber View の設定ファイル(config.yaml)の下書きを作る

  python -m cvs.installer                 # 見つけたものを表示するだけ
  python -m cvs.installer --write config.yaml [--force]

探すもの(引数で場所を指定すれば、そこだけを見る):
  - Prometheus(既定 http://127.0.0.1:9090): snmp_exporter の scrape job と、その対象(装置)・インタフェース
  - Loki(既定 http://127.0.0.1:3100)
  - Zabbix(/etc/zabbix/web/zabbix.conf.php があるか、--zabbix で URL)。トークンは ${ZABBIX_TOKEN} のまま
  - MRTG(/etc/mrtg.cfg・/etc/mrtg/*.cfg): Target から装置・インタフェース・IP
  - Cacti(/var/lib/cacti/rra 等): RRD のファイル名から装置。どのファイルがどのインタフェースかは自動では分からない
    ので、targets は下書き(コメント)として出す
  - Ollama(既定 http://127.0.0.1:11434)と、AI の鍵(ANTHROPIC_API_KEY / GROQ_API_KEY / OPENAI_API_KEY)
つながり(links)は、同じ /30・/31 に IP がある2台を「推定」として入れる。必ず見直すこと。
既存の設定ファイルは --force が無ければ上書きしない。秘密(トークン・パスワード)はファイルに書かない。
"""
import argparse
import glob
import ipaddress
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from .legacy_sources import parse_mrtg_cfg
from .link_evidence import norm_if

MRTG_CFGS = ['/etc/mrtg.cfg', '/etc/mrtg/*.cfg', '/usr/local/etc/mrtg/*.cfg']
CACTI_DIRS = ['/var/lib/cacti/rra', '/usr/share/cacti/rra', '/var/www/html/cacti/rra', '/usr/share/cacti/site/rra']


def http_json(url, timeout=3):
    req = urllib.request.Request(url, headers={'User-Agent': 'cyber-view-installer'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 構内の監視基盤へは直接
    with opener.open(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    try:
        return json.loads(body)
    except ValueError:
        return body


def _prom(url, get, q):
    d = get(url.rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': q}))
    return d['data']['result'] if isinstance(d, dict) and d.get('status') == 'success' else []


def detect(prometheus=None, loki=None, zabbix=None, mrtg_cfgs=None, cacti_dirs=None, ollama=None, env=None, get=None):
    get = get or http_json
    env = env if env is not None else os.environ
    found = {'prometheus': None, 'loki': None, 'zabbix': None, 'mrtg': [], 'cacti': [], 'ollama': None, 'ai': None,
             'devices': {}, 'notes': []}

    def dev(key, hostname=None):
        d = found['devices'].setdefault(key, {'hostname': hostname or key, 'mgmt_ip': None, 'ifs': {}, 'from': set()})
        if hostname and d['hostname'] == key:
            d['hostname'] = hostname
        return d

    # Prometheus + snmp_exporter
    p_url = prometheus or 'http://127.0.0.1:9090'
    try:
        info = get(p_url.rstrip('/') + '/api/v1/status/buildinfo')
        ver = (info.get('data') or {}).get('version') if isinstance(info, dict) else None
        tg = get(p_url.rstrip('/') + '/api/v1/targets?state=active')
        jobs = sorted({t['labels'].get('job') for t in (tg.get('data') or {}).get('activeTargets', [])}) if isinstance(tg, dict) else []
        job = next((j for j in jobs if 'snmp' in (j or '').lower()), None)
        found['prometheus'] = {'url': p_url, 'version': ver, 'jobs': jobs, 'job': job}
        if job:
            for r in _prom(p_url, get, f'count by (instance, ifName, ifDescr, sysName) (ifHCInOctets{{job="{job}"}})'):
                m = r['metric']
                inst = m.get('instance', '')
                d = dev(inst.split(':')[0], m.get('sysName'))
                d['mgmt_ip'] = d['mgmt_ip'] or inst.split(':')[0]
                name = m.get('ifDescr') or m.get('ifName')
                if name:
                    d['ifs'].setdefault(norm_if(name), {'name': name})
                d['from'].add('prometheus')
        else:
            found['notes'].append('Prometheus に snmp を含む job が見つかりません(snmp_exporter の scrape を確認)')
    except Exception as e:  # noqa: BLE001
        if prometheus:
            found['notes'].append(f'Prometheus({p_url})に届きません: {e}')

    # Loki
    l_url = loki or 'http://127.0.0.1:3100'
    try:
        r = get(l_url.rstrip('/') + '/ready')
        if 'ready' in str(r).lower():
            found['loki'] = {'url': l_url}
    except Exception as e:  # noqa: BLE001
        if loki:
            found['notes'].append(f'Loki({l_url})に届きません: {e}')

    # Zabbix
    z_url = zabbix
    if not z_url and os.path.exists('/etc/zabbix/web/zabbix.conf.php'):
        z_url = 'http://127.0.0.1/zabbix/api_jsonrpc.php'
    if z_url:
        found['zabbix'] = {'url': z_url}

    # MRTG
    for pat in mrtg_cfgs if mrtg_cfgs is not None else MRTG_CFGS:
        for path in sorted(glob.glob(pat)):
            try:
                p = parse_mrtg_cfg(open(path, encoding='utf-8', errors='replace').read())
            except OSError:
                continue
            if not p['targets']:
                continue
            found['mrtg'].append({'cfg': path, 'workdir': p['workdir'], 'targets': len(p['targets'])})
            for t in p['targets'].values():
                if not t.get('host'):
                    continue
                d = dev(t['host'])
                d['mgmt_ip'] = d['mgmt_ip'] or t['host']
                if t.get('descr'):
                    i = d['ifs'].setdefault(norm_if(t['descr']), {'name': t['descr']})
                    if t.get('ip') and re.fullmatch(r'[\d.]+', t['ip']):
                        i['ip'] = t['ip']
                m = re.search(r'--\s*(\S+)\s*$', t.get('title', ''))
                if m and d['hostname'] == t['host']:
                    d['hostname'] = m.group(1)
                d['from'].add('mrtg')

    # Cacti
    for pat in cacti_dirs if cacti_dirs is not None else CACTI_DIRS:
        for path in sorted(glob.glob(pat)):
            files = sorted(f for f in os.listdir(path) if f.endswith('.rrd') and 'traffic' in f)
            if files:
                found['cacti'].append({'rra_dir': path, 'files': files[:200]})

    # AI
    try:
        o_url = ollama or 'http://127.0.0.1:11434'
        tags = get(o_url.rstrip('/') + '/api/tags')
        if isinstance(tags, dict):
            found['ollama'] = {'url': o_url, 'models': [m.get('name') for m in tags.get('models', [])][:10]}
    except Exception:  # noqa: BLE001
        pass
    if env.get('ANTHROPIC_API_KEY') or env.get('ANTHROPIC_AUTH_TOKEN'):
        found['ai'] = 'claude'
    elif env.get('GROQ_API_KEY') or env.get('OPENAI_API_KEY') or env.get('NIRVANA_AI_KEY'):
        found['ai'] = 'openai'
    elif found['ollama']:
        found['ai'] = 'ollama'
    return found


def infer_links(devices):
    """同じ /30・/31 の中に IP がある2台をつなぐ(推定)"""
    ends = []
    for did, d in devices.items():
        for i in d['interfaces']:
            if i.get('ip'):
                ends.append((did, i['name'], ipaddress.IPv4Address(i['ip']), i))
    links, used = [], set()
    for k, (a, ai, aip, aif) in enumerate(ends):
        for b, bi, bip, bif in ends[k + 1:]:
            if a == b or (a, ai) in used or (b, bi) in used:
                continue
            for plen in (30, 31):
                if ipaddress.ip_network(f'{aip}/{plen}', strict=False) == ipaddress.ip_network(f'{bip}/{plen}', strict=False):
                    links.append({'a': a, 'a_if': ai, 'b': b, 'b_if': bi})
                    used |= {(a, ai), (b, bi)}
                    aif['prefix'] = bif['prefix'] = plen     # 対向がある = 対向間のリンク(/30・/31)
                    break
    return links


def build_config(found, title='本番ネットワーク'):
    devices = {}
    for n, (key, d) in enumerate(sorted(found['devices'].items()), 1):
        did = re.sub(r'[^a-z0-9]+', '-', (d['hostname'] or key).lower()).strip('-') or f'dev{n}'
        while did in devices:
            did += f'-{n}'
        ifs = [{'name': i['name'], **({'ip': i['ip'], 'prefix': 24} if i.get('ip') else {})} for i in d['ifs'].values()]
        devices[did] = {'id': did, 'hostname': d['hostname'], 'type': 'cisco', 'mgmt_ip': d['mgmt_ip'], 'interfaces': ifs}
    cfg = {'title': title, 'poll_interval': 10, 'link_speed_bps': 1_000_000_000,
           'devices': list(devices.values()), 'links': infer_links(devices), 'sources': {}}
    src = cfg['sources']
    if found['prometheus'] and found['prometheus'].get('job'):
        src['prometheus'] = {'url': found['prometheus']['url'], 'job': found['prometheus']['job']}
    if found['loki']:
        src['loki'] = {'url': found['loki']['url'], 'query': '{job=~".+"}'}
    if found['zabbix']:
        src['zabbix'] = {'url': found['zabbix']['url'], 'token': '${ZABBIX_TOKEN}'}
    if found['mrtg']:
        src['mrtg'] = {'cfg': found['mrtg'][0]['cfg']}
    if found['cacti']:
        src['cacti'] = {'rra_dir': found['cacti'][0]['rra_dir'], 'targets': []}
    return cfg


def render_yaml(cfg, found):
    import yaml
    lines = ['# Cyber View の設定(インストーラーが作った下書き)。必ず見直してから使う',
             '# - type は装置の種類(cisco / catalyst / nexus / juniper / sir / yamaha …)に直す',
             '# - links は「同じ /30・/31 に IP がある2台」を推定したもの。足りないつながりは手で足す',
             '# - 秘密は ${ENV} のまま。起動時の環境変数で渡す']
    if found['cacti']:
        c = found['cacti'][0]
        lines.append('# - Cacti: どの RRD がどのインタフェースかは自動で分からないので、sources.cacti.targets を手で書く。例:')
        for f in c['files'][:5]:
            lines.append(f'#     - {{file: {f}, device: <装置 id>, if: <インタフェース名>}}')
    if found['ai']:
        lines.append(f"# - AI ログ解析: {found['ai']} を使う(鍵は環境変数。NIRVANA_AI_PROVIDER で切り替え)")
    else:
        lines.append('# - AI ログ解析: 鍵が無いのでルールで解析する(ANTHROPIC_API_KEY 等を設定すると AI を使う)')
    for n in found['notes']:
        lines.append(f'# 注: {n}')
    return '\n'.join(lines) + '\n' + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=200)


def summary(found):
    out = []
    p = found['prometheus']
    out.append(f"Prometheus : {p['url'] + ' v' + str(p['version']) + ' job=' + str(p['job']) if p else 'なし'}")
    out.append(f"Loki       : {found['loki']['url'] if found['loki'] else 'なし'}")
    out.append(f"Zabbix     : {found['zabbix']['url'] if found['zabbix'] else 'なし'}")
    out.append(f"MRTG       : {', '.join(m['cfg'] + ' (' + str(m['targets']) + ' 対象)' for m in found['mrtg']) or 'なし'}")
    out.append(f"Cacti      : {', '.join(c['rra_dir'] + ' (' + str(len(c['files'])) + ' RRD)' for c in found['cacti']) or 'なし'}")
    out.append(f"Ollama     : {found['ollama']['url'] if found['ollama'] else 'なし'}")
    out.append(f"AI         : {found['ai'] or 'なし(ルールで解析)'}")
    out.append(f"装置       : {len(found['devices'])} 台 " + ', '.join(f"{d['hostname']}({'/'.join(sorted(d['from']))})" for d in found['devices'].values()))
    out += [f'注: {n}' for n in found['notes']]
    return '\n'.join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Cyber View: 監視の仕組みを探して設定ファイルの下書きを作る')
    ap.add_argument('--write', metavar='PATH')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--title', default='本番ネットワーク')
    ap.add_argument('--prometheus')
    ap.add_argument('--loki')
    ap.add_argument('--zabbix')
    ap.add_argument('--mrtg-cfg', action='append')
    ap.add_argument('--cacti-rra', action='append')
    ap.add_argument('--ollama')
    a = ap.parse_args(argv)
    found = detect(a.prometheus, a.loki, a.zabbix, a.mrtg_cfg, a.cacti_rra, a.ollama)
    print(summary(found))
    if not a.write:
        return 0
    if not found['devices']:
        print('装置が1台も見つかりませんでした。config.example.yaml を元に手で書いてください', file=sys.stderr)
        return 2
    if os.path.exists(a.write) and not a.force:
        print(f'{a.write} は既にあります(上書きするなら --force)', file=sys.stderr)
        return 3
    cfg = build_config(found, a.title)
    with open(a.write, 'w', encoding='utf-8') as f:
        f.write(render_yaml(cfg, found))
    from . import config as C
    C.load(a.write)                                  # 作ったファイルが読めることを確かめる
    print(f'書きました: {a.write}(装置 {len(cfg["devices"])} 台・つながり {len(cfg["links"])} 本・取り込み口 {", ".join(cfg["sources"]) or "なし"})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
