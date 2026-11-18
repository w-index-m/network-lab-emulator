"""
昔ながらの監視(MRTG・Cacti)から流量を読む

Prometheus が普及する前(〜2015年ごろ)は、SNMP で数分おきにカウンタを取ってグラフにする MRTG(1995〜)と
Cacti(2001〜)が定番だった。今も動いている現場は多いので、そのデータをそのまま Cyber View に載せる。

- MRTG
  - 既定の .log 形式: 1行目 `<時刻> <入力カウンタ> <出力カウンタ>`、2行目以降 `<時刻> <平均入力> <平均出力> <最大入力> <最大出力>`
    (バイト/秒、新しい順)。最新の区間の平均 ×8 を bps とする
  - `LogFormat: rrdtool` の場合は <target>.rrd(ds0 = 入力、ds1 = 出力、バイト/秒)を `rrdtool fetch` で読む
  - mrtg.cfg(cfgmaker が作るもの)から Target 名 → 装置・インタフェースを自動で対応づける:
    `Target[name]: <ifIndex>:<community>@<host>:` と `SetEnv[name]: MRTG_INT_IP="..." MRTG_INT_DESCR="..."`
- Cacti
  - データソースの RRD(「Interface - Traffic」テンプレートは traffic_in / traffic_out、COUNTER、バイト/秒)を
    `rrdtool fetch` で読む。ファイルと装置・インタフェースの対応は設定に書く(Cacti の DB は読まない)

どちらも「最後に更新された区間」の値を返す。更新が `stale` 秒より古ければ値を返さない(止まった MRTG を
「流量0」と誤表示しないため)。
"""
import os
import re
import shutil
import subprocess
import time

from .link_evidence import norm_if

# mrtg.cfg / Cacti の RRD を探す場所の候補(インストーラーと、設定のパスが見つからないときのエラーで使う)。
# Debian 系は /etc/mrtg.cfg、RHEL 系は /etc/mrtg/mrtg.cfg、FreeBSD は /usr/local/etc/mrtg/mrtg.cfg、
# MRTG のソースを自分で入れた場合は MRTG の手順書にある /usr/local/mrtg-2/conf/mrtg.cfg が多い
MRTG_CFG_CANDIDATES = ['/etc/mrtg.cfg', '/etc/mrtg/*.cfg', '/usr/local/etc/mrtg/*.cfg', '/usr/local/mrtg-2/conf/*.cfg']
CACTI_RRA_CANDIDATES = ['/var/lib/cacti/rra', '/usr/share/cacti/rra', '/var/www/html/cacti/rra', '/usr/share/cacti/site/rra']

_TARGET = re.compile(r'^Target\[([^\]]+)\]:\s*(.+)$', re.I)
_SETENV = re.compile(r'^SetEnv\[([^\]]+)\]:\s*(.+)$', re.I)
_WORKDIR = re.compile(r'^(?:WorkDir|LogDir):\s*(\S+)', re.I)
_LOGFMT = re.compile(r'^LogFormat:\s*(\S+)', re.I)
_TITLE = re.compile(r'^Title\[([^\]]+)\]:\s*(.+)$', re.I)


def parse_mrtg_cfg(text):
    """mrtg.cfg → {'workdir', 'logformat', 'targets': {name: {'host', 'ifindex', 'ip', 'descr', 'title'}}}"""
    out = {'workdir': None, 'logformat': 'mrtg', 'targets': {}}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = _WORKDIR.match(line)
        if m:
            out['workdir'] = m.group(1)
            continue
        m = _LOGFMT.match(line)
        if m:
            out['logformat'] = m.group(1).lower()
            continue
        m = _TARGET.match(line)
        if m:
            name, spec = m.group(1), m.group(2).strip()
            t = out['targets'].setdefault(name, {})
            # 2:public@10.0.0.1:  /  #Gi0/0/0:public@host  /  /10.0.0.1:public@host(IP 指定)
            mm = re.match(r'^([^:@\s]+):([^@\s]*)@([^:\s]+)', spec)
            if mm:
                sel, host = mm.group(1), mm.group(3)
                t['host'] = host
                if sel.isdigit():
                    t['ifindex'] = int(sel)
                elif sel.startswith('#'):
                    t['descr'] = sel[1:]
                elif sel.startswith('/'):
                    t['ip'] = sel[1:]
            continue
        m = _SETENV.match(line)
        if m:
            t = out['targets'].setdefault(m.group(1), {})
            for k, v in re.findall(r'(\w+)="([^"]*)"', m.group(2)):
                if k == 'MRTG_INT_IP' and v:
                    t['ip'] = v
                elif k == 'MRTG_INT_DESCR' and v:
                    t['descr'] = v
            continue
        m = _TITLE.match(line)
        if m:
            out['targets'].setdefault(m.group(1), {})['title'] = m.group(2)
    return out


def read_mrtg_log(path):
    """MRTG の .log → (時刻, 入力 bps, 出力 bps)。最新の区間の平均"""
    with open(path, encoding='ascii', errors='replace') as f:
        f.readline()                                # 1行目はカウンタの生値
        for line in f:
            p = line.split()
            if len(p) >= 3:
                return int(p[0]), float(p[1]) * 8, float(p[2]) * 8
    return None


def rrd_last(path, ds_in, ds_out, rrdtool='rrdtool', seconds=1800):
    """rrdtool fetch で、値の入っている最新の行 → (時刻, 入力 bps, 出力 bps)。値はバイト/秒の想定"""
    r = subprocess.run([rrdtool, 'fetch', path, 'AVERAGE', '-s', f'-{seconds}'], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200] or 'rrdtool fetch failed')
    lines = r.stdout.splitlines()
    names = lines[0].split() if lines else []
    if ds_in not in names or ds_out not in names:
        raise RuntimeError(f'{os.path.basename(path)} に {ds_in}/{ds_out} がありません({" ".join(names)})')
    i_in, i_out = names.index(ds_in), names.index(ds_out)
    for line in reversed(lines[1:]):
        if ':' not in line:
            continue
        t, vals = line.split(':', 1)
        v = vals.split()
        try:
            a, b = float(v[i_in]), float(v[i_out])
        except (ValueError, IndexError):
            continue
        if a != a or b != b:                       # nan
            continue
        return int(t), a * 8, b * 8
    return None


class LegacyRates:
    """MRTG・Cacti の設定から {(装置, インタフェース): (in_bps, out_bps, 取得元)} を作る"""

    def __init__(self, cfg, devices, lookup):
        src = cfg.get('sources') or {}
        self.mrtg = src.get('mrtg') or None
        self.cacti = src.get('cacti') or None
        self.devices = devices              # id -> device
        self.lookup = lookup                # (host/ip/name) -> id
        self.rrdtool = shutil.which('rrdtool') or 'rrdtool'
        self.errors = {}
        self.mapping = {}                   # 'mrtg' / 'cacti' -> [(dev, ifname, 読み方)]

    def enabled(self):
        return bool(self.mrtg or self.cacti)

    def _dev(self, key):
        return self.lookup(str(key or ''))

    def _ifname(self, dev, target):
        """MRTG の対象(descr / ip / ifIndex)→ 設定のインタフェース名"""
        ifs = self.devices[dev]['interfaces']
        if target.get('descr'):
            for i in ifs:
                if norm_if(i['name']) == norm_if(target['descr']):
                    return i['name']
        if target.get('ip'):
            for i in ifs:
                if i.get('ip') == target['ip']:
                    return i['name']
        if target.get('ifindex') and target['ifindex'] <= len(ifs):
            return ifs[target['ifindex'] - 1]['name']      # 設定の並び順 = ifIndex の順、とみなす(最後の手段)
        return None

    def map_mrtg(self):
        m = self.mrtg
        items = []
        parsed = {'workdir': m.get('workdir'), 'logformat': m.get('logformat', 'mrtg'), 'targets': {}}
        if m.get('cfg') and not os.path.exists(m['cfg']):
            # 黙って「対象なし」にしない(パスの書き間違いに気づけるよう、候補も添える)
            raise FileNotFoundError(f"mrtg.cfg が見つかりません: {m['cfg']}(候補: {', '.join(MRTG_CFG_CANDIDATES)})")
        if m.get('cfg'):
            p = parse_mrtg_cfg(open(m['cfg'], encoding='utf-8', errors='replace').read())
            parsed['targets'] = p['targets']
            parsed['workdir'] = parsed['workdir'] or p['workdir']
            if 'logformat' not in m:
                parsed['logformat'] = p['logformat']
        explicit = {t['target']: t for t in m.get('targets') or []}
        for name, t in parsed['targets'].items():
            if name in explicit:
                continue
            dev = self._dev(t.get('host'))
            ifn = self._ifname(dev, t) if dev else None
            if dev and ifn:
                items.append((dev, ifn, name))
        for name, t in explicit.items():
            items.append((t['device'], t['if'], name))
        self.mapping['mrtg'] = items
        return parsed, items

    def rates(self, now=None):
        now = now or time.time()
        out = {}
        if self.mrtg:
            try:
                parsed, items = self.map_mrtg()
                wd = parsed['workdir'] or '.'
                stale = float(self.mrtg.get('stale', 900))
                for dev, ifn, name in items:
                    base = os.path.join(wd, name.lower() if self.mrtg.get('lowercase', True) else name)
                    if parsed['logformat'] == 'rrdtool':
                        got = rrd_last(base + '.rrd', 'ds0', 'ds1', self.rrdtool)
                    else:
                        got = read_mrtg_log(base + '.log') if os.path.exists(base + '.log') else None
                    if got and now - got[0] <= stale:
                        out[(dev, norm_if(ifn))] = (got[1], got[2], 'mrtg', got[0])
                self.errors.pop('mrtg', None)
            except Exception as e:  # noqa: BLE001
                self.errors['mrtg'] = str(e)[:200]
        if self.cacti:
            try:
                rra = self.cacti.get('rra_dir', '/var/lib/cacti/rra')
                if not os.path.isdir(rra):
                    raise FileNotFoundError(f"Cacti の rra_dir が見つかりません: {rra}(候補: {', '.join(CACTI_RRA_CANDIDATES)})")
                stale = float(self.cacti.get('stale', 900))
                items, bad = [], []
                for t in self.cacti.get('targets') or []:
                    path = t['file'] if os.path.isabs(t['file']) else os.path.join(rra, t['file'])
                    items.append((t['device'], t['if'], path))
                    try:
                        got = rrd_last(path, t.get('ds_in', 'traffic_in'), t.get('ds_out', 'traffic_out'), self.rrdtool)
                    except Exception as e:  # noqa: BLE001 - 1つ読めなくても他は出す
                        bad.append(f'{os.path.basename(path)}: {e}'[:120])
                        continue
                    if got and now - got[0] <= stale:
                        out[(t['device'], norm_if(t['if']))] = (got[1], got[2], 'cacti', got[0])
                self.mapping['cacti'] = items
                if bad:
                    self.errors['cacti'] = '; '.join(bad)[:200]
                else:
                    self.errors.pop('cacti', None)
            except Exception as e:  # noqa: BLE001
                self.errors['cacti'] = str(e)[:200]
        return out
