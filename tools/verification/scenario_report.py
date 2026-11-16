"""
1シナリオ(機種 × プロトコル)ごとに専用の環境を立ち上げて試験し、マークダウンにまとめる。

  python3 tools/verification/scenario_report.py catalyst ospf            # 3台(既定)
  python3 tools/verification/scenario_report.py sir bgp --devices 4      # 4台
  python3 tools/verification/scenario_report.py all                      # 全12シナリオを順番に(1つずつ別環境)

1シナリオごとに:
  1. 保存設定を消してエミュレータ(uvicorn)を新しく起動(他のシナリオの装置・ログが混ざらない)
  2. 台数からトポロジとアドレスを自動で決める
       ルーティング: R1 ─ R2 ─ … ─ RN の直列 + R1 と RN にそれぞれ LAN
       STP        : R1 → R2 → … → RN → R1 のリング(R1 の priority を下げてルートに)
       アドレス   : 10.<機種番号>.<プロトコル番号*40 + リンク番号>.0/24、LAN は +30/+31
  3. 正常 → 障害 → 復旧 → フラップ の順に動かし、段階ごとに
       投入コマンド / show 出力 / ログ(/cyber-view が見ているもの) / アラート / 状態ボード / リンクの検知経路
     を記録、ROUTER LINKS 画面を撮影
  4. docs/verification/<機種>-<プロトコル>.md と画像を書き出し、エミュレータを止める
"""
import argparse
import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
OUTDIR = os.path.join(ROOT, 'docs', 'verification')
IMGDIR = os.path.join(OUTDIR, 'img')
PORT = int(os.environ.get('SCENARIO_PORT', '8000'))
BASE = f'http://127.0.0.1:{PORT}'

VENDORS = {
    'catalyst': {'n': 61, 'label': 'Cisco Catalyst', 'ifs': ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/3']},
    'srs': {'n': 62, 'label': 'Fujitsu SR-S', 'ifs': ['GigabitEthernet1/0/1', 'GigabitEthernet1/0/2', 'GigabitEthernet1/0/3']},
    'sir': {'n': 63, 'label': 'Fujitsu Si-R', 'ifs': ['lan0', 'lan1', 'lan2']},
}
PROTOS = {'stp': 1, 'rip': 2, 'ospf': 3, 'bgp': 4}
CODE = {'rip': 'R', 'ospf': 'O', 'bgp': 'B'}
BLOCK_WORDS = ('BLK', 'Altn', 'Blocking', 'BLOCKING', 'Alternate')


# ── エミュレータの起動・停止 ─────────────────────────────
class Emulator:
    def __init__(self, logpath):
        self.logpath = logpath
        self.proc = None

    def __enter__(self):
        cfg = os.path.join(ROOT, 'saved_config.json')
        if os.path.exists(cfg):
            os.remove(cfg)
        env = dict(os.environ, NETLAB_AUTH_DISABLE='1')
        for k in ('PROMETHEUS_URL', 'LOKI_URL', 'SNMP_TRAP_LISTEN'):
            env.pop(k, None)
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(PORT)],
            cwd=ROOT, env=env, stdout=open(self.logpath, 'w'), stderr=subprocess.STDOUT, start_new_session=True)
        end = time.time() + 60
        while time.time() < end:
            try:
                urllib.request.urlopen(BASE + '/api/health', timeout=2)
                return self
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        raise RuntimeError('emulator did not start')

    def __exit__(self, *a):
        if self.proc:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        cfg = os.path.join(ROOT, 'saved_config.json')
        if os.path.exists(cfg):
            os.remove(cfg)


def req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(r, timeout=60) as f:
        return json.loads(f.read().decode() or '{}')


def wait(fn, timeout=90, step=2):
    end = time.time() + timeout
    v = fn()
    while not v and time.time() < end:
        time.sleep(step)
        v = fn()
    return v


# ── シナリオ本体 ──────────────────────────────────────
class Scenario:
    def __init__(self, vendor, proto, n):
        self.v, self.p, self.n = vendor, proto, n
        V = VENDORS[vendor]
        self.ifs = V['ifs']
        self.ids = [f'{vendor}-{proto}-r{i + 1}' for i in range(n)]
        self.tag = f'{vendor}-{proto}' + ('' if n == 3 else f'-{n}dev')     # 出力ファイル名(3台以外は台数を付ける)
        self.hosts = {d: d.upper() for d in self.ids}
        self.cmds = {d: [] for d in self.ids}          # 投入したコマンド(装置ごと)
        self.checks = []                               # (段階, 確認内容, ok, 補足)
        self.phases = []                               # 段階ごとの記録
        self.addr = {}                                 # (装置, IF) -> ip/24
        self.links = []                                # (a, ifa, b, ifb, net)
        self.lans = {}
        base = PROTOS[proto] * 40
        self.net = lambda k: f"10.{V['n']}.{base + k}"
        self.t0 = time.time()

    # CLI(投入内容を記録しながら)
    def cli(self, dev, *cmds, record=True):
        out = ''
        for c in cmds:
            if record:
                self.cmds[dev].append(c)
            out = req('/api/cli', {'device_id': dev, 'command': c}).get('output', '')
        return out

    def show(self, dev, cmd):
        return self.cli(dev, cmd, record=False)

    def check(self, phase, name, ok, info=''):
        self.checks.append((phase, name, bool(ok), str(info)[:400] if not ok else ''))
        print(f"  [{'OK' if ok else 'NG'}] {phase}: {name}", flush=True)

    # ── 構成 ──
    def build(self):
        v = self.v
        for d in self.ids:
            req('/api/device', {'id': d, 'type': v, 'hostname': self.hosts[d]})
            if v == 'sir':     # ether ポートと lan を VLAN で結ぶ(実機どおり。無いと ether use off で lan が落ちない)
                self.cli(d, 'configure', 'ether 1 1 vlan untag 1', 'ether 1 2 vlan untag 2', 'ether 1 3 vlan untag 3',
                         'lan 0 vlan 1', 'lan 1 vlan 2', 'lan 2 vlan 3', 'exit')
        if self.p == 'stp':
            self._build_stp()
        else:
            self._build_chain()

    def _ifaddr(self, d, iface, ip):
        self.addr[(d, iface)] = f'{ip}/24'
        if self.v == 'sir':
            self.cli(d, 'configure', f"lan {iface.replace('lan', '')} ip address {ip}/24 3", 'exit')
        else:
            self.cli(d, 'configure terminal', f'interface {iface}', 'no switchport',
                     f'ip address {ip} 255.255.255.0', 'no shutdown', 'end')

    def _build_chain(self):
        prev_if, next_if, lan_if = self.ifs
        for i in range(self.n - 1):
            a, b, net = self.ids[i], self.ids[i + 1], self.net(i + 1)
            req('/api/link', {'a': a, 'b': b, 'iface_a': next_if, 'iface_b': prev_if})
            self.links.append((a, next_if, b, prev_if, f'{net}.0/24'))
            self._ifaddr(a, next_if, f'{net}.1')
            self._ifaddr(b, prev_if, f'{net}.2')
        self.lans = {self.ids[0]: self.net(30), self.ids[-1]: self.net(31)}
        for d, net in self.lans.items():
            self._ifaddr(d, lan_if, f'{net}.1')

    def _build_stp(self):
        a_if, b_if = self.ifs[0], self.ifs[1]
        for d in self.ids:
            if self.v == 'sir':
                self.cli(d, 'configure', 'stp mode stp', 'exit')
            else:
                self.cli(d, 'configure terminal', 'spanning-tree mode rapid-pvst', 'end')
        r1 = self.ids[0]
        if self.v == 'sir':
            self.cli(r1, 'configure', 'stp priority 4096', 'exit')
        else:
            self.cli(r1, 'configure terminal', 'spanning-tree vlan 1 priority 4096', 'end')
        for i in range(self.n):
            a, b = self.ids[i], self.ids[(i + 1) % self.n]
            req('/api/link', {'a': a, 'b': b, 'iface_a': b_if, 'iface_b': a_if})
            self.links.append((a, b_if, b, a_if, 'L2'))

    def routing(self):
        v, p, ids = self.v, self.p, self.ids
        prev_if, next_if, lan_if = self.ifs

        def nets_of(i):
            ns = []
            if i > 0:
                ns.append(self.net(i))
            if i < self.n - 1:
                ns.append(self.net(i + 1))
            if ids[i] in self.lans:
                ns.append(self.lans[ids[i]])
            return ns

        for i, d in enumerate(ids):
            ns = nets_of(i)
            asn = 65001 + i
            nbrs = []
            if i > 0:
                nbrs.append((f'{self.net(i)}.1', 65000 + i))
            if i < self.n - 1:
                nbrs.append((f'{self.net(i + 1)}.2', 65002 + i))
            if v == 'sir':
                used = [f for f in (prev_if, next_if, lan_if)
                        if (d, f) in self.addr]
                if p == 'rip':
                    self.cli(d, 'configure', 'ip rip use on', *[f'ip rip network {x}.0/24' for x in ns], 'exit')
                elif p == 'ospf':
                    self.cli(d, 'configure', 'ospf use on', 'ospf area 0.0.0.0',
                             *[f"lan {f.replace('lan', '')} ip ospf use on" for f in used], 'exit')
                else:
                    self.cli(d, 'configure', 'bgp use on', f'bgp as {asn}',
                             *[f'bgp neighbor {k} address {ip} remote-as {ra}' for k, (ip, ra) in enumerate(nbrs)],
                             *([f'bgp network {self.lans[d]}.0/24'] if d in self.lans else []), 'exit')
            else:
                if p == 'rip':
                    self.cli(d, 'configure terminal', 'ip routing', 'router rip', 'version 2',
                             'network 10.0.0.0', 'no auto-summary', 'end')
                elif p == 'ospf':
                    self.cli(d, 'configure terminal', 'ip routing', 'router ospf 1',
                             *[f'network {x}.0 0.0.0.255 area 0' for x in ns], 'end')
                else:
                    self.cli(d, 'configure terminal', 'ip routing', f'router bgp {asn}',
                             *[f'neighbor {ip} remote-as {ra}' for ip, ra in nbrs],
                             *([f'network {self.lans[d]}.0 mask 255.255.255.0'] if d in self.lans else []), 'end')

    # ── 観測 ──
    def links_api(self):
        return {(l['a'], l['b']): l for l in req('/api/traffic/links?window=60')['links']}

    def link_of(self, a, b):
        L = self.links_api()
        return L.get((a, b)) or L.get((b, a))

    def logs_since(self, t):
        rows = req(f'/api/traffic/logs?since={t}&limit=200')['logs']
        return [r for r in rows if r['dev'] in self.ids]

    def alerts_since(self, t):
        return [a for a in req('/api/traffic/alerts')['alerts'] if a['t'] >= t and
                (a['src'] in self.ids or a.get('dst') in self.ids)]

    def routes_since(self, seq):
        return [e for e in req(f'/api/traffic/routes?since={seq}&limit=500')['events'] if e['dev'] in self.ids]

    def health(self):
        return {c['key']: c for c in req('/api/traffic/health')['categories']}

    def has_route(self, dev, net, code=None):
        for line in self.show(dev, 'show ip route').splitlines():
            if f'{net}.0' in line and (code is None or line.strip().startswith(code)):
                return line.strip()
        return None

    def snapshot(self, name, t_start, seq_start, shows, note=''):
        """段階の記録(show 出力・ログ・アラート・経路イベント・状態ボード・リンク)"""
        time.sleep(1.5)
        h = self.health()
        L = self.links_api()
        self.phases.append({
            'name': name, 'note': note, 'at': time.strftime('%H:%M:%S'),
            'shows': [(d, c, self.show(d, c)) for d, c in shows],
            'logs': self.logs_since(t_start),
            'alerts': self.alerts_since(t_start),
            'routes': self.routes_since(seq_start),
            'health': {k: (c['state'], c['summary']) for k, c in h.items()},
            'links': [(self.hosts.get(l['a'], l['a']), self.hosts.get(l['b'], l['b']), l['state'], l['down'],
                       l.get('down_sources', []), l.get('evidence', {})) for l in L.values()],
        })

    def shot(self, tag):
        out = os.path.join(IMGDIR, f'{self.tag}-{tag}.png')
        try:
            r = subprocess.run(['node', os.path.join(ROOT, 'tools', 'verification', 'shot.js'),
                                f'{BASE}/cyber-view?view=links', out, '6000', '10'],
                               env=dict(os.environ, NODE_PATH=subprocess.run(['npm', 'root', '-g'], capture_output=True,
                                                                             text=True).stdout.strip()),
                               capture_output=True, text=True, timeout=90)
            if 'JSERR' in r.stdout:
                self.check(tag, '画面にJavaScriptエラーが無い', False, r.stdout)
            return os.path.relpath(out, OUTDIR)
        except Exception as e:  # noqa: BLE001
            print('screenshot failed', e)
            return None

    # ── 実行 ──
    def run(self):
        self.build()
        time.sleep(4)                       # 経路監視の基準取り
        if self.p == 'stp':
            return self._run_stp()
        return self._run_routing()

    def _run_routing(self):
        p, ids = self.p, self.ids
        r1, rn, rm = ids[0], ids[-1], ids[-2]
        lan_n = self.lans[rn]
        fault_if = self.ifs[1]                       # R(N-1) の次段向け IF
        t = time.time(); seq = req('/api/traffic/routes')['seq']
        self.routing()
        nb_cmd = {'rip': 'show ip route', 'ospf': 'show ip ospf neighbor',
                  'bgp': 'show ip bgp summary'}[p]
        r = wait(lambda: self.has_route(r1, lan_n, CODE[p]), 150, 3)
        self.check('正常', f'{self.hosts[r1]} が {self.hosts[rn]} の LAN {lan_n}.0/24 を学習({CODE[p]})', r)
        L = self.links_api()
        self.check('正常', '全リンクが links API で state=ok(画面は緑の NORMAL)',
                   len(L) == self.n - 1 and all(x['state'] == 'ok' for x in L.values()))
        ev = wait(lambda: [e for e in self.routes_since(seq) if e['dev'] == r1 and e['prefix'] == f'{lan_n}.0/24'
                           and e['kind'] == 'add'], 15)
        self.check('正常', '経路追加の INFO(情)が出る', ev)
        h = self.health()
        self.check('正常', '状態ボードのネットワーク観点が正常', h['network']['state'] == 'ok', h['network']['summary'])
        self.snapshot('正常', t, seq, [(r1, 'show ip route'), (ids[1], nb_cmd)],
                      f'{self.hosts[r1]} から {self.hosts[rn]} の LAN まで経路が通っている状態')
        img_n = self.shot('normal')

        t = time.time(); seq = req('/api/traffic/routes')['seq']
        self._shut(rm, fault_if, True)
        ln = wait(lambda: (lambda x: x if x and x['down'] else None)(self.link_of(rm, rn)), 15)
        self.check('障害', f'{self.hosts[rm]}-{self.hosts[rn]} が LINK DOWN', ln)
        self.check('障害', '検知経路に STATE と SYSLOG', ln and {'STATE', 'SYSLOG'} <= set(ln.get('down_sources', [])),
                   ln and ln.get('down_sources'))
        h = self.health()
        self.check('障害', '状態ボードのネットワーク観点が異常', h['network']['state'] == 'crit', h['network']['summary'])
        self.check('障害', 'link_down アラート', [a for a in self.alerts_since(t) if a['rule'] == 'link_down'])
        gone = wait(lambda: not self.has_route(r1, lan_n), 200, 3)
        self.check('障害', f'{self.hosts[r1]} から LAN {lan_n}.0/24 の経路が消える', gone)
        ev = wait(lambda: [e for e in self.routes_since(seq) if e['dev'] == r1 and e['prefix'] == f'{lan_n}.0/24'
                           and e['kind'] in ('del', 'change')], 15)
        self.check('障害', '経路削除の INFO(情)が出る', ev)
        img_f = self.shot('fault')
        self.snapshot('障害', t, seq, [(r1, 'show ip route'), (rm, nb_cmd)],
                      f'{self.hosts[rm]} の {fault_if} を落とした状態')

        t = time.time(); seq = req('/api/traffic/routes')['seq']
        self._shut(rm, fault_if, False)
        r = wait(lambda: self.has_route(r1, lan_n, CODE[p]), 200, 3)
        self.check('復旧', '経路が戻る', r)
        ln = wait(lambda: (lambda x: x if x and not x['down'] else None)(self.link_of(rm, rn)), 15)
        self.check('復旧', 'LINK DOWN が解除される', ln, self.link_of(rm, rn))
        self.snapshot('復旧', t, seq, [(r1, 'show ip route'), (rm, nb_cmd)], '戻した状態')

        t = time.time(); seq = req('/api/traffic/routes')['seq']
        for _ in range(2):
            self._shut(rm, fault_if, True)
            self._shut(rm, fault_if, False)
        fl = wait(lambda: [a for a in self.alerts_since(t) if a['rule'] == 'if_flap'], 10, 1)
        self.check('フラップ', 'if_flap(注 WARN IF FLAP)が出る', fl)
        h = self.health()
        self.check('フラップ', '状態ボードの syslog 観点にフラップ件数', 'フラップ' in h['syslog']['summary'],
                   h['syslog']['summary'])
        time.sleep(5)
        self.snapshot('フラップ', t, seq, [(rm, nb_cmd)], '落とす/戻すを2回繰り返した直後')
        return [('正常時', img_n), ('障害時', img_f)]

    def _run_stp(self):
        ids, r1, r2 = self.ids, self.ids[0], self.ids[1]
        t = time.time(); seq = req('/api/traffic/routes')['seq']
        time.sleep(8)

        def blocked():
            return [d for d in ids if any(k in self.show(d, 'show spanning-tree') for k in BLOCK_WORDS)]
        b = wait(blocked, 20)
        self.check('正常', 'リングのどこか1台が Blocking(ループ回避)', b)
        self.check('正常', 'STP が動いているのでループでも MAC フラップは出ない',
                   not [a for a in self.alerts_since(t) if a['rule'] == 'mac_flap'])
        L = self.links_api()
        self.check('正常', f'リンク {self.n} 本が links API で state=ok',
                   len(L) == self.n and all(x['state'] == 'ok' for x in L.values()))
        n_blk = [l for l in self.logs_since(t) if 'BLOCKING' in l['msg'].upper()]
        self.check('正常', 'Blocking のログが繰り返し出ない(状態変化の時だけ)', len(n_blk) <= self.n, len(n_blk))
        self.snapshot('正常', t, seq, [(d, 'show spanning-tree') for d in ids], f'{self.hosts[r1]} がルートのリング')
        img_n = self.shot('normal')

        t = time.time(); seq = req('/api/traffic/routes')['seq']
        self._shut(r1, self.ifs[1], True)               # ルートの R1→R2 側
        time.sleep(8)
        ln = self.link_of(r1, r2)
        self.check('障害', f'{self.hosts[r1]}-{self.hosts[r2]} が LINK DOWN', ln and ln['down'], ln)
        self.check('障害', '迂回のため Blocking が解除される', not blocked())
        logs = self.logs_since(t)
        self.check('障害', 'トポロジ変更のログ', any(k in l['msg'] for l in logs for k in ('SPANTREE', 'STP', 'TOPO', 'topology')))
        h = self.health()
        self.check('障害', '状態ボードのネットワーク観点が異常', h['network']['state'] == 'crit', h['network']['summary'])
        img_f = self.shot('fault')
        self.snapshot('障害', t, seq, [(d, 'show spanning-tree') for d in ids], f'{self.hosts[r1]} の {self.ifs[1]} を落とした状態')

        t = time.time(); seq = req('/api/traffic/routes')['seq']
        self._shut(r1, self.ifs[1], False)
        time.sleep(8)
        ln = self.link_of(r1, r2)
        self.check('復旧', 'LINK DOWN が解除される', ln and not ln['down'], ln)
        self.check('復旧', 'ふたたびどこか1台が Blocking', blocked())
        self.snapshot('復旧', t, seq, [(d, 'show spanning-tree') for d in ids], '戻した状態')
        return [('正常時', img_n), ('障害時', img_f)]

    def _shut(self, dev, iface, down):
        if self.v == 'sir':
            port = int(iface.replace('lan', '')) + 1
            self.cli(dev, 'configure', f'ether 1 {port} use {"off" if down else "on"}', 'exit')
        else:
            self.cli(dev, 'configure terminal', f'interface {iface}', 'shutdown' if down else 'no shutdown', 'end')

    # ── マークダウン ──
    def markdown(self, images, elapsed):
        V = VENDORS[self.v]
        ok = sum(1 for c in self.checks if c[2])
        L = []
        w = L.append
        w(f'# {V["label"]} × {self.p.upper()} — {self.n}台構成の正常/障害試験\n')
        w(f'- 実行: {_dt.datetime.fromtimestamp(self.t0).strftime("%Y-%m-%d %H:%M")} / 所要 {elapsed:.0f} 秒 / '
          f'専用環境(保存設定を消してエミュレータを新規起動、他シナリオの装置は無し)')
        w(f'- 結果: **{ok} / {len(self.checks)} 項目 OK**' + (' ✅' if ok == len(self.checks) else ' ⚠️'))
        w(f'- 再実行: `python3 tools/verification/scenario_report.py {self.v} {self.p} --devices {self.n}`\n')
        w('## 構成(台数から自動で割り当て)\n')
        if self.p == 'stp':
            w('```\n' + ' → '.join(self.hosts[d] for d in self.ids) + f' → {self.hosts[self.ids[0]]}   (リング)\n```\n')
            w(f'{self.hosts[self.ids[0]]} の priority を 4096 に下げてルートブリッジにする。\n')
        else:
            w('```\n' + f'LAN {self.lans[self.ids[0]]}.0/24 ── ' + ' ══ '.join(self.hosts[d] for d in self.ids)
              + f' ── LAN {self.lans[self.ids[-1]]}.0/24\n```\n')
            w('| 装置 | IF | アドレス |\n|---|---|---|')
            for (d, i), a in sorted(self.addr.items()):
                w(f'| {self.hosts[d]} | {i} | {a} |')
            w('')
        w('| リンク | 装置A IF | 装置B IF | ネットワーク |\n|---|---|---|---|')
        for a, ia, b, ib, net in self.links:
            w(f'| {self.hosts[a]} ⇔ {self.hosts[b]} | {ia} | {ib} | {net} |')
        w('\n## 投入したコマンド\n')
        for d in self.ids:
            w(f'<details><summary>{self.hosts[d]}({len(self.cmds[d])} 行)</summary>\n\n```\n' + '\n'.join(self.cmds[d]) + '\n```\n</details>\n')
        w('## 確認項目\n')
        w('| 段階 | 確認内容 | 結果 | 補足 |\n|---|---|---|---|')
        for ph, name, k, info in self.checks:
            w(f'| {ph} | {name} | {"✅" if k else "❌"} | {info.replace("|", "/").replace(chr(10), " ")[:160]} |')
        if images:
            w('\n## 画面(ROUTER LINKS)\n')
            for cap, img in images:
                if img:
                    w(f'**{cap}**\n\n![{cap}]({img})\n')
        w('## 段階ごとの記録\n')
        for ph in self.phases:
            w(f'### {ph["name"]}({ph["at"]})\n\n{ph["note"]}\n')
            w('**状態ボード**: ' + ' / '.join(f'{k}={s}「{m}」' for k, (s, m) in ph['health'].items()) + '\n')
            w('**リンク**\n\n| A | B | 状態 | LINK DOWN | 検知経路 |\n|---|---|---|---|---|')
            for a, b, st, dn, srcs, ev in ph['links']:
                evs = ', '.join(f'{k}:{v}' for k, v in ev.items() if v)
                w(f'| {a} | {b} | {st} | {"はい" if dn else "-"} | {evs} |')
            for d, c, out in ph['shows']:
                w(f'\n`{self.hosts[d]}# {c}`\n\n```\n{out.strip()[:2500]}\n```')
            w(f'\n**ログ**(/cyber-view が見ているもの、{len(ph["logs"])} 件)\n')
            if ph['logs']:
                w('```')
                for l in ph['logs'][-40:]:
                    w(f"{time.strftime('%H:%M:%S', time.localtime(l['t']))} {self.hosts.get(l['dev'], l['dev'])} sev{l['sev']} {l['msg'][:150]}")
                w('```')
            w(f'\n**アラート**({len(ph["alerts"])} 件)\n')
            for a in ph['alerts'][:15]:
                w(f"- [{a['severity']}/{a.get('source', '')}] {a['name']} — {self.hosts.get(a['src'], a['src'])}: {a['detail'][:120]}")
            if ph['routes']:
                w(f'\n**経路の変化(情 INFO)**({len(ph["routes"])} 件)\n')
                for e in ph['routes'][:15]:
                    w(f"- {e['host']} {e['kind']} {e['text']}")
            w('')
        return '\n'.join(L) + '\n'


def run_one(vendor, proto, n):
    os.makedirs(IMGDIR, exist_ok=True)
    print(f'== {vendor}/{proto} ({n}台)', flush=True)
    logpath = os.path.join('/tmp', f'scenario-{vendor}-{proto}.log')
    t0 = time.time()
    with Emulator(logpath):
        s = Scenario(vendor, proto, n)
        try:
            imgs = s.run()
        except Exception as e:  # noqa: BLE001
            s.check('実行', '例外なく完了', False, repr(e))
            imgs = []
    md = s.markdown(imgs, time.time() - t0)
    path = os.path.join(OUTDIR, f'{s.tag}.md')
    open(path, 'w').write(md)
    ok = sum(1 for c in s.checks if c[2])
    print(f'   -> {path}  {ok}/{len(s.checks)}', flush=True)
    return vendor, proto, n, ok, len(s.checks), time.time() - t0


def write_index(results):
    path = os.path.join(OUTDIR, 'README.md')
    L = ['# 機種 × プロトコル 個別試験の一覧\n',
         '1行 = 1シナリオ = 1つの専用環境。各リンク先に、構成(自動割り当てのアドレス)・投入コマンド・'
         '段階ごとの show 出力/ログ/アラート/状態ボード/画面をまとめてある。\n',
         f'最終更新: {_dt.datetime.now().strftime("%Y-%m-%d %H:%M")}\n',
         '| 機種 | STP | RIP | OSPF | BGP |', '|---|---|---|---|---|']
    by = {(v, p): (ok, tot, n) for v, p, n, ok, tot, _ in results}
    for v in VENDORS:
        cells = []
        for p in PROTOS:
            if (v, p) in by:
                ok, tot, n = by[(v, p)]
                cells.append(f'[{"✅" if ok == tot else "⚠️"} {ok}/{tot}]({v}-{p}.md)')
            else:
                cells.append('-')
        L.append(f'| {VENDORS[v]["label"]} | ' + ' | '.join(cells) + ' |')
    L.append('\n再実行: `python3 tools/verification/scenario_report.py all [--devices N]`'
             '(1シナリオずつエミュレータを立ち上げ直して実行。ライブのエミュレータや全体テストとは同時に動かさない)\n')
    # 既存の README があれば結果の表だけ差し替える(手で書いた説明は残す)
    table = '\n'.join(L[2:L.index(next(x for x in L if x.startswith('\n再実行')))]) + '\n'
    if os.path.exists(path) and '<!-- results:start -->' in open(path).read():
        cur = open(path).read()
        head, rest = cur.split('<!-- results:start -->', 1)
        _, tail = rest.split('<!-- results:end -->', 1)
        open(path, 'w').write(head + '<!-- results:start -->\n' + table + '<!-- results:end -->' + tail)
    else:
        open(path, 'w').write('\n'.join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('vendor', help='catalyst / srs / sir / all')
    ap.add_argument('proto', nargs='?', help='stp / rip / ospf / bgp(all のときは省略)')
    ap.add_argument('--devices', type=int, default=3)
    a = ap.parse_args()
    if not shutil.which('node'):
        print('node が無いので画面は撮らない')
    todo = ([(v, p) for v in VENDORS for p in PROTOS] if a.vendor == 'all' else [(a.vendor, a.proto)])
    results = [run_one(v, p, a.devices) for v, p in todo]
    if a.vendor == 'all':
        write_index(results)
    for r in results:
        print(f'{r[0]}/{r[1]}: {r[3]}/{r[4]} ({r[5]:.0f}s)')


if __name__ == '__main__':
    main()
