#!/usr/bin/env python3
"""
CDP/LLDPの隣接情報から、ネットワークトポロジー図を自動生成する。

`GET /api/topology/neighbors` を叩いて各装置の実際のCDP/LLDP
ネイバーテーブル（`DeviceState.cdp_neighbors`/`.lldp_neighbors`）を
集め、Mermaid（`mermaid.js`で描画できる形式）またはGraphviz DOT形式の
グラフとして出力する。

あくまでCDP/LLDPという「ネイバー広告」ベースの発見なので、実機の
NetBox/LibreNMS等のトポロジー自動発見ツールと同じ制約を持つ:
リンクが張られていない区間や、CDP/LLDPが無効な装置は見えない。

使い方:
  # Mermaid形式で標準出力へ
  python tools/topology_diagram.py --emulator-url http://localhost:8000

  # Graphviz DOT形式でファイルへ
  python tools/topology_diagram.py --format dot --out topology.dot

CDP/LLDPは`/api/link`でリンクを張った時点で自動的に更新される
（`_rebuild_all_neighbors()`が呼ばれる）ので、このツール側で
明示的に同期させる操作は不要。
"""

import argparse
import sys
import urllib.request
import json


def fetch_neighbors(base_url: str) -> dict:
    with urllib.request.urlopen(f'{base_url}/api/topology/neighbors', timeout=5) as resp:
        return json.loads(resp.read())


_ICON_BY_TYPE = {
    'catalyst': '🖧', 'nexus': '🗄️', 'cisco': '📡', 'sir': '📶',
    'srs': '📶', 'asa': '🛡️', 'apresia': '🔀',
}


def to_mermaid(data: dict) -> str:
    devices = data['devices']
    edges = data['edges']
    lines = ['graph LR']
    for dev_id, info in sorted(devices.items()):
        icon = _ICON_BY_TYPE.get(info['type'], '📦')
        label = f"{icon} {info['hostname']}\\n({info['type']})"
        lines.append(f'    {dev_id}["{label}"]')
    for e in edges:
        a_if = _short_if(e.get('a_if'))
        b_if = _short_if(e.get('b_if'))
        label = f'{a_if} - {b_if}' if (a_if or b_if) else ''
        if label:
            lines.append(f'    {e["a"]} ---|"{label}"| {e["b"]}')
        else:
            lines.append(f'    {e["a"]} --- {e["b"]}')
    return '\n'.join(lines)


def to_dot(data: dict) -> str:
    devices = data['devices']
    edges = data['edges']
    lines = ['graph topology {', '    rankdir=LR;', '    node [shape=box];']
    for dev_id, info in sorted(devices.items()):
        label = f"{info['hostname']}\\n({info['type']})"
        lines.append(f'    "{dev_id}" [label="{label}"];')
    for e in edges:
        a_if = _short_if(e.get('a_if'))
        b_if = _short_if(e.get('b_if'))
        label = f'{a_if} - {b_if}' if (a_if or b_if) else ''
        attr = f' [label="{label}"]' if label else ''
        lines.append(f'    "{e["a"]}" -- "{e["b"]}"{attr};')
    lines.append('}')
    return '\n'.join(lines)


def _short_if(name):
    """GigabitEthernet1/0/1 -> Gi1/0/1 のように短縮して図を見やすくする"""
    if not name:
        return ''
    for full, abbr in (('TenGigabitEthernet', 'Te'), ('GigabitEthernet', 'Gi'),
                       ('FastEthernet', 'Fa'), ('Ethernet', 'Eth'),
                       ('loopback', 'Lo')):
        if name.startswith(full):
            return abbr + name[len(full):]
    return name


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--emulator-url', default='http://localhost:8000')
    p.add_argument('--format', choices=['mermaid', 'dot'], default='mermaid')
    p.add_argument('--out', default=None, help='出力先ファイル(省略時は標準出力)')
    args = p.parse_args()

    data = fetch_neighbors(args.emulator_url)
    if not data['devices']:
        print('装置が見つかりませんでした', file=sys.stderr)
        sys.exit(1)

    out = to_mermaid(data) if args.format == 'mermaid' else to_dot(data)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(out + '\n')
        print(f'{len(data["devices"])}台、{len(data["edges"])}本のリンクを'
              f'{args.out}に書き出しました（{args.format}形式）', file=sys.stderr)
    else:
        print(out)


if __name__ == '__main__':
    main()
