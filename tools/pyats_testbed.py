#!/usr/bin/env python3
"""
エミュレータの装置を pyATS/Genie から扱うためのヘルパー。

1. testbed生成: 実SSHリスナーが動いている装置(crypto key generate rsa 済み)
   から pyATS の testbed YAML を作る(pyATS不要、標準ライブラリのみ)
2. parse:  unicon で本物のSSH接続をして Genie パーサで構造化する
3. snapshot / diff: 設定変更の前後で parse 結果を保存し、Genie の Diff で
   何が変わったかを出す(genie learn/diff と同じ考え方を show 単位で)

    python tools/pyats_testbed.py testbed --url http://localhost:8000 \\
        --username admin --password 'Adm1nP@ss' -o testbed.yaml
    python tools/pyats_testbed.py parse -t testbed.yaml --device R1 \\
        "show ip route" "show ip ospf neighbor"
    python tools/pyats_testbed.py snapshot -t testbed.yaml --device R1 -d before
    (... 設定変更 ...)
    python tools/pyats_testbed.py snapshot -t testbed.yaml --device R1 -d after
    python tools/pyats_testbed.py diff before after

parse/snapshot/diff は pyats[library] が必要
(pip install "pyats[library]"、unicon が ssh コマンドを使うので openssh-client も)。
"""

import argparse
import json
import os
import sys
import urllib.request

# pyATS の os 名(unicon プラグイン/Genie パーサの選択に使う)
PYATS_OS = {'cisco': 'iosxe', 'catalyst': 'iosxe', 'wlc': 'iosxe',
            'nexus': 'nxos', 'asa': 'asa', 'arista': 'eos', 'juniper': 'junos'}
PYATS_TYPE = {'cisco': 'router', 'catalyst': 'switch', 'wlc': 'wlc', 'nexus': 'switch',
              'asa': 'firewall', 'arista': 'switch', 'juniper': 'router'}

DEFAULT_SHOWS = ['show version', 'show ip interface brief', 'show interfaces',
                 'show ip route', 'show ip ospf neighbor', 'show inventory']


def _api(url, path, body=None, token=None):
    req = urllib.request.Request(
        url.rstrip('/') + path, method='POST' if body is not None else 'GET',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json',
                 **({'X-Session-Token': token} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ssh_ip_from_show(text: str):
    """`show ip ssh` で有効か判定する(無効ならNone)。"""
    return 'SSH Enabled' in (text or '')


def build_testbed(devices: dict, username: str, password: str) -> dict:
    """devices: {hostname: {'type': device_type, 'ip': mgmt_ip}} → testbed dict"""
    tb = {'testbed': {'name': 'netlab-emulator'}, 'devices': {}}
    for host, d in devices.items():
        tb['devices'][host] = {
            'os': PYATS_OS.get(d['type'], 'iosxe'),
            'type': PYATS_TYPE.get(d['type'], 'router'),
            'credentials': {'default': {'username': username, 'password': password}},
            'connections': {'cli': {
                'protocol': 'ssh', 'ip': d['ip'], 'port': d.get('port', 22),
                # 装置ごとに毎回ホスト鍵が変わるので known_hosts は使わない
                'ssh_options': '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
            }},
        }
    return tb


def to_yaml(obj, indent=0) -> str:
    """依存を増やさないための最小YAML出力(dict/str/int のみ)"""
    pad = '  ' * indent
    out = []
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f'{pad}{k}:')
            out.append(to_yaml(v, indent + 1))
        else:
            sv = str(v)
            out.append(f'{pad}{k}: ' + (json.dumps(sv) if isinstance(v, str) else sv))
    return '\n'.join(out)


def discover(url, token=None) -> dict:
    """/api/topology から pyATS 対応 device_type で SSH が有効な装置を拾う。

    待ち受けIPは /api/snmp/dashboard ではなく装置の管理IP(Vlan/物理IFの
    最初のIP)。エミュレータのSSHリスナーは engine/ssh_cli_agent.py の
    _management_ip() と同じ規則で選ぶので、それを show ip interface brief
    から再現するより /api/ssh/listeners があればそれを使う。
    """
    devs = _api(url, '/api/topology', token=token).get('devices', {})
    out = {}
    try:
        listeners = _api(url, '/api/ssh/listeners', token=token)
    except Exception:
        listeners = {}
    for dev_id, info in devs.items():
        if info.get('type') not in PYATS_OS:
            continue
        ip = (listeners.get(dev_id) or {}).get('ip')
        if not ip:
            continue
        out[info.get('hostname') or dev_id] = {'type': info['type'], 'ip': ip,
                                               'port': listeners[dev_id].get('port', 22)}
    return out


def _connect(testbed, device):
    from genie.testbed import load
    tb = load(testbed)
    dev = tb.devices[device]
    dev.connect(log_stdout=False, learn_hostname=True, connection_timeout=30)
    return dev


def parse(testbed, device, commands) -> dict:
    dev = _connect(testbed, device)
    res = {}
    try:
        for cmd in commands:
            try:
                res[cmd] = dev.parse(cmd)
            except Exception as e:      # SchemaEmptyParserError 等は結果として残す
                res[cmd] = {'_error': f'{type(e).__name__}: {e}'}
    finally:
        dev.disconnect()
    return res


def snapshot(testbed, device, out_dir, commands=None):
    os.makedirs(out_dir, exist_ok=True)
    res = parse(testbed, device, commands or DEFAULT_SHOWS)
    for cmd, data in res.items():
        with open(os.path.join(out_dir, cmd.replace(' ', '_') + '.json'), 'w') as fh:
            json.dump(data, fh, indent=2, sort_keys=True, default=str)
    return res


def diff_dirs(before, after) -> dict:
    """同名のスナップショット同士を Genie の Diff で比較する。"""
    from genie.utils.diff import Diff
    out = {}
    for name in sorted(set(os.listdir(before)) | set(os.listdir(after))):
        a = json.load(open(os.path.join(before, name))) if os.path.exists(os.path.join(before, name)) else {}
        b = json.load(open(os.path.join(after, name))) if os.path.exists(os.path.join(after, name)) else {}
        # 稼働時間・タイマーは毎回変わるので除外(genie diff の --exclude と同じ)
        d = Diff(a, b, exclude=['uptime', 'dead_time', 'up_time', 'last_change', 'counters',
                                'in_rate', 'out_rate', 'age'])
        d.findDiff()
        if str(d):
            out[name[:-5].replace('_', ' ')] = str(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('testbed')
    t.add_argument('--url', default='http://localhost:8000')
    t.add_argument('--token', default=os.environ.get('NETLAB_TOKEN'))
    t.add_argument('--username', required=True)
    t.add_argument('--password', required=True)
    t.add_argument('-o', '--output', default='testbed.yaml')
    for name in ('parse', 'snapshot'):
        p = sub.add_parser(name)
        p.add_argument('-t', '--testbed', required=True)
        p.add_argument('--device', required=True)
        if name == 'snapshot':
            p.add_argument('-d', '--dir', required=True)
        p.add_argument('commands', nargs='*')
    d = sub.add_parser('diff')
    d.add_argument('before')
    d.add_argument('after')
    a = ap.parse_args(argv)

    if a.cmd == 'testbed':
        devs = discover(a.url, a.token)
        if not devs:
            print('SSHが有効なpyATS対応装置がありません(crypto key generate rsa を投入してください)',
                  file=sys.stderr)
            return 1
        with open(a.output, 'w') as fh:
            fh.write(to_yaml(build_testbed(devs, a.username, a.password)) + '\n')
        print(f'wrote {a.output}: {", ".join(devs)}')
    elif a.cmd == 'parse':
        print(json.dumps(parse(a.testbed, a.device, a.commands or DEFAULT_SHOWS),
                         indent=2, default=str))
    elif a.cmd == 'snapshot':
        res = snapshot(a.testbed, a.device, a.dir, a.commands or None)
        for cmd, data in res.items():
            print(f'{"FAIL" if "_error" in data else "ok  "} {cmd}')
    elif a.cmd == 'diff':
        res = diff_dirs(a.before, a.after)
        if not res:
            print('差分なし')
        for cmd, text in res.items():
            print(f'### {cmd}\n{text}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
