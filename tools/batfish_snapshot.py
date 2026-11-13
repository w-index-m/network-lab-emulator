#!/usr/bin/env python3
"""
エミュレータの running-config を Batfish スナップショットに書き出し、
Batfish で静的解析する。さらにエミュレータ自身の `show ip route` と
Batfish が設定から計算した経路を突き合わせる(差分検証)。

    # 書き出しだけ(pybatfish不要)
    python tools/batfish_snapshot.py --url http://localhost:8000 \\
        --devices r1,r2 --out ./snap
    # 書き出し + Batfish解析 + 経路の突き合わせ
    python tools/batfish_snapshot.py --url http://localhost:8000 \\
        --devices r1,r2 --out ./snap --analyze --compare-routes

Batfish本体は docker で起動しておく:
    docker run -d --name batfish -p 9996:9996 -p 9997:9997 batfish/allinone

対象は Batfish が Cisco IOS として読める device_type
(cisco/catalyst/wlc/arista/nexus/asa)。--devices を省くと /api/topology から
該当する装置を全部拾う。標準ライブラリだけで書き出せ、解析部分だけ
pybatfish を使う(無ければ解析をスキップする)。
"""

import argparse
import json
import os
import re
import sys
import urllib.request

BATFISH_TYPES = ('cisco', 'catalyst', 'wlc', 'arista', 'nexus', 'asa')

# show ip route のコード → Batfish の Protocol 列
_PROTO = {'C': 'connected', 'L': 'local', 'S': 'static', 'O': 'ospf', 'R': 'rip',
          'B': 'bgp', 'D': 'eigrp'}


def _request(url, path, body=None, token=None):
    req = urllib.request.Request(url.rstrip('/') + path, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json',
                                          **({'X-Session-Token': token} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def cli(url, device_id, command, token=None) -> str:
    return _request(url, '/api/cli', {'device_id': device_id, 'command': command}, token)['output']


def list_devices(url, token=None):
    devs = _request(url, '/api/topology', token=token).get('devices', {})
    return [d for d, info in devs.items() if info.get('type') in BATFISH_TYPES]


def export_snapshot(url, devices, out_dir, token=None) -> list:
    """装置ごとの running-config を <out>/configs/<id>.cfg に書き、パス一覧を返す。"""
    cfg_dir = os.path.join(out_dir, 'configs')
    os.makedirs(cfg_dir, exist_ok=True)
    paths = []
    for d in devices:
        text = cli(url, d, 'show running-config', token)
        # Batfishのノード名はファイル名ではなくhostname行(小文字化)から
        # 決まる。ファイル名はどの装置のものか分かるようdevice_idにする
        p = os.path.join(cfg_dir, f'{d}.cfg')
        with open(p, 'w') as fh:
            fh.write(text if text.endswith('\n') else text + '\n')
        paths.append(p)
    return paths


_ROUTE_RE = re.compile(r'^([A-Z])\*?\s*(?:[A-Z0-9]{1,2}\s+)?(\d+\.\d+\.\d+\.\d+)(/\d+)?\s')


def parse_show_ip_route(text: str) -> set:
    """IOSの show ip route から {(prefix, protocol)} を取り出す。

    "10.0.0.0/8 is variably subnetted" のような見出し行の下に
    "C   10.1.1.0/24 is directly connected" が並ぶ形式と、
    見出しの /len を省略した行の両方に対応する。
    """
    routes = set()
    current_len = None
    for raw in text.splitlines():
        line = raw.rstrip()
        head = re.match(r'^\s+(\d+\.\d+\.\d+\.\d+)/(\d+) is (?:variably )?subnetted', line)
        if head:
            current_len = head.group(2)
            continue
        m = _ROUTE_RE.match(line)
        if not m or m.group(1) not in _PROTO:
            continue
        plen = m.group(3) or (f'/{current_len}' if current_len else None)
        if not plen:
            continue
        routes.add((m.group(2) + plen, _PROTO[m.group(1)]))
    return routes


def diff_routes(emulator: set, batfish: set) -> dict:
    """プレフィックス単位で比較する(ospfIA/ospfE2等はospfにまとめる)。"""
    def norm(rs):
        return {(p, 'ospf' if proto.startswith('ospf') else proto) for p, proto in rs}
    e, b = norm(emulator), norm(batfish)
    return {'only_emulator': sorted(e - b), 'only_batfish': sorted(b - e), 'common': sorted(e & b)}


def batfish_node_name(config_text: str, default: str) -> str:
    m = re.search(r'^hostname\s+(\S+)', config_text, re.M)
    return (m.group(1) if m else default).lower()


def analyze(out_dir, host='localhost', network='netlab'):
    from pybatfish.client.session import Session
    bf = Session(host=host)
    bf.set_network(network)
    bf.init_snapshot(out_dir, name='emulator', overwrite=True)
    return bf


def batfish_routes(bf, node) -> set:
    df = bf.q.routes(nodes=node).answer().frame()
    return {(r.Network, r.Protocol) for r in df.itertuples()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--url', default='http://localhost:8000')
    ap.add_argument('--token', default=os.environ.get('NETLAB_TOKEN'))
    ap.add_argument('--devices', help='カンマ区切りのdevice_id(省略時は全Cisco系装置)')
    ap.add_argument('--out', default='./batfish_snapshot')
    ap.add_argument('--analyze', action='store_true', help='Batfishで解析する')
    ap.add_argument('--compare-routes', action='store_true',
                    help='エミュレータの show ip route とBatfishの計算経路を比較する')
    ap.add_argument('--batfish-host', default='localhost')
    a = ap.parse_args(argv)

    devices = a.devices.split(',') if a.devices else list_devices(a.url, a.token)
    if not devices:
        print('対象装置がありません', file=sys.stderr)
        return 1
    for p in export_snapshot(a.url, devices, a.out, a.token):
        print(f'wrote {p}')
    if not (a.analyze or a.compare_routes):
        return 0
    try:
        bf = analyze(a.out, a.batfish_host)
    except ImportError:
        print('pybatfish が無いため解析をスキップしました (pip install pybatfish)', file=sys.stderr)
        return 0

    rc = 0
    if a.analyze:
        q = bf.q
        print('\n## fileParseStatus\n', q.fileParseStatus().answer().frame().to_string())
        issues = q.initIssues().answer().frame()
        print('\n## initIssues\n', issues.to_string() if len(issues) else '(none)')
        print('\n## ospfSessionCompatibility\n', q.ospfSessionCompatibility().answer().frame().to_string())
        print('\n## bgpSessionStatus\n', q.bgpSessionStatus().answer().frame().to_string())
        print('\n## filterLineReachability\n', q.filterLineReachability().answer().frame().to_string())
    if a.compare_routes:
        for d in devices:
            emu = parse_show_ip_route(cli(a.url, d, 'show ip route', a.token))
            with open(os.path.join(a.out, 'configs', f'{d}.cfg')) as fh:
                node = batfish_node_name(fh.read(), d)
            res = diff_routes(emu, batfish_routes(bf, node))
            ok = not res['only_emulator'] and not res['only_batfish']
            rc |= 0 if ok else 2
            print(f'\n## routes {d}: {"MATCH" if ok else "DIFF"} ({len(res["common"])} common)')
            for k in ('only_emulator', 'only_batfish'):
                for p, proto in res[k]:
                    print(f'  {k:14s} {proto:9s} {p}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
