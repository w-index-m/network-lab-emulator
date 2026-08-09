#!/usr/bin/env python3
"""
EVE-NG 実機投入 / 検証ツール（mgmt IP へ SSH）

本エミュレータの /api/export で出力した構成を取得し、
EVE-NG 上に立てた各ノードの管理IPへ netmiko(SSH) で

  1) export  … 構成をファイルへ書き出すだけ（投入しない）
  2) deploy  … running-config を各ノードへ投入
  3) verify  … show を実行し期待文字列をチェック

の3モードで動かす。

前提:
  - pip install netmiko
  - EVE-NG 側で各ノードに mgmt IP を割当済み、SSH到達可能
  - ノードID → 接続先(host/user/pass) の対応を inventory.yaml で与える
    （mgmt IP は /api/export の推定値をデフォルトにするが、上書き可）

使い方:
  python tools/eveng_deploy.py export  --api http://127.0.0.1:8099 --out ./out
  python tools/eveng_deploy.py deploy  --inventory inventory.json
  python tools/eveng_deploy.py verify  --inventory inventory.json --checks checks.json
"""
import argparse
import json
import os
import re
import sys
import urllib.request


# ── 実機プラットフォーム向けインターフェース名変換 ────────────
# 本ツールのexport configは Gi0/0/x（ルータ流）等になるため、
# 実機のポート体系へ寄せる。Catalyst 3650 は Gi1/0/x / Te1/1/x。
_IF_RE = re.compile(
    r'\b(TenGigabitEthernet|GigabitEthernet|FastEthernet|Te|Gi|Fa)'
    r'(\d+)/(\d+)(?:/(\d+))?\b')


def build_if_map(cfg: str, platform: str) -> dict:
    """cfg中に出現するインターフェース名を実機体系へ写す辞書を作る。

    出現順に 1 始まりで採番するので、ポート0や重複衝突を起こさない
    （合成ポート番号は物理ポートと無関係なため、連番化して安全側に倒す）。
    戻り値: {元の名前: 変換後の名前}
    """
    if platform in (None, '', 'generic'):
        return {}
    mapping = {}
    gi_n = [0]
    te_n = [0]
    for m in _IF_RE.finditer(cfg):
        name = m.group(0)
        if name in mapping:
            continue
        is_ten = m.group(1) in ('TenGigabitEthernet', 'Te')
        if platform == 'c3650':
            if is_ten:
                te_n[0] += 1
                mapping[name] = f'TenGigabitEthernet1/1/{te_n[0]}'
            else:
                gi_n[0] += 1
                mapping[name] = f'GigabitEthernet1/0/{gi_n[0]}'
    return mapping


def _apply_if_map(text: str, mapping: dict) -> str:
    if not text or not mapping:
        return text
    # 長い名前から先に置換（GigabitEthernet を Gi より先に）
    for src in sorted(mapping, key=len, reverse=True):
        text = re.sub(r'\b' + re.escape(src) + r'\b', mapping[src], text)
    return text


def fetch_export(api_base):
    with urllib.request.urlopen(api_base.rstrip('/') + '/api/export', timeout=15) as r:
        return json.loads(r.read())


# ── export: 構成をファイルへ書き出す ─────────────────────────
def cmd_export(args):
    data = fetch_export(args.api)
    os.makedirs(args.out, exist_ok=True)
    inv = {}
    if_maps = {}   # dev_id -> {元IF名: 変換後}
    for d in data['devices']:
        path = os.path.join(args.out, f"{d['id']}.cfg")
        cfg = d['running_config']
        ifmap = build_if_map(cfg, args.platform)
        if_maps[d['id']] = ifmap
        cfg = _apply_if_map(cfg, ifmap)
        with open(path, 'w') as f:
            f.write(cfg.rstrip() + '\n')
        mgmt = d.get('mgmt') or {}
        inv[d['id']] = {
            'device_type': d['netmiko_device_type'],
            'host':        mgmt.get('ip', ''),      # ← 実機/EVE-NG の mgmt IP に要確認/上書き
            'username':    'admin',
            'password':    'admin',
            'secret':      'admin',
            'config_file': path,
        }
        print(f"  wrote {path}  (type={d['type']} netmiko={d['netmiko_device_type']} "
              f"mgmt={mgmt.get('ip','?')})")
        if ifmap:
            for src, dst in ifmap.items():
                print(f"      IF {src} -> {dst}")
    # トポロジ（リンク）: IF名も同じ変換を適用して整合させる
    links_out = []
    for lk in data['links']:
        links_out.append({
            'a':       lk['a'],
            'iface_a': if_maps.get(lk['a'], {}).get(lk['iface_a'], lk['iface_a']),
            'b':       lk['b'],
            'iface_b': if_maps.get(lk['b'], {}).get(lk['iface_b'], lk['iface_b']),
        })
    with open(os.path.join(args.out, 'topology.json'), 'w') as f:
        json.dump(links_out, f, indent=2, ensure_ascii=False)
    inv_path = os.path.join(args.out, 'inventory.json')
    with open(inv_path, 'w') as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)
    print(f"\ntopology -> {os.path.join(args.out, 'topology.json')}")
    print(f"inventory 雛形 -> {inv_path}")
    print("  ※ host/username/password を EVE-NG 実機に合わせて編集してから deploy してください。")


def _connect(entry):
    from netmiko import ConnectHandler
    return ConnectHandler(
        device_type=entry['device_type'],
        host=entry['host'],
        username=entry.get('username', 'admin'),
        password=entry.get('password', 'admin'),
        secret=entry.get('secret', entry.get('password', 'admin')),
        fast_cli=False,
    )


# ── deploy: 各ノードへ running-config を投入 ──────────────────
def cmd_deploy(args):
    inv = json.load(open(args.inventory))
    rc = 0
    for dev_id, entry in inv.items():
        if not entry.get('host'):
            print(f"[SKIP] {dev_id}: host 未設定")
            continue
        cfg_path = entry.get('config_file')
        if not cfg_path or not os.path.exists(cfg_path):
            print(f"[SKIP] {dev_id}: config_file なし")
            continue
        cfg_lines = [l for l in open(cfg_path).read().splitlines()
                     if l.strip() and not l.strip().startswith('!')]
        try:
            conn = _connect(entry)
            conn.enable()
            out = conn.send_config_set(cfg_lines)
            try:
                conn.save_config()
            except Exception:
                conn.send_command_timing('write memory')
            conn.disconnect()
            print(f"[OK]   {dev_id} @ {entry['host']}  ({len(cfg_lines)} lines)")
            if args.verbose:
                print(out)
        except Exception as e:
            print(f"[FAIL] {dev_id} @ {entry['host']}: {e}")
            rc = 1
    return rc


# ── RESTCONF ヘルパ（IOS-XE 16.x, HTTPS/JSON, 自己署名前提） ──
def _restconf_get(entry, path):
    import requests
    from requests.auth import HTTPBasicAuth
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    port = entry.get('restconf_port', 443)
    url = f"https://{entry['host']}:{port}/restconf/data/{path}"
    r = requests.get(
        url,
        auth=HTTPBasicAuth(entry.get('username', 'admin'),
                           entry.get('password', 'admin')),
        headers={"Accept": "application/yang-data+json"},
        verify=entry.get('tls_verify', False),
        timeout=entry.get('timeout', 20),
    )
    r.raise_for_status()
    return r.json()


def _collect_values(obj, key):
    """JSON中から指定キー名（名前空間接頭辞は無視）の値をすべて集める"""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key or k.split(':')[-1] == key:
                if not isinstance(v, (dict, list)):
                    found.append(v)
            found += _collect_values(v, key)
    elif isinstance(obj, list):
        for it in obj:
            found += _collect_values(it, key)
    return found


def _find_route_dicts(obj, prefix):
    """JSON中から、指定prefixを持つ「経路エントリ」の部分木を集める。
    キー名の名前空間接頭辞は無視。prefix は "10.0.0.0/24" 形式でも
    ネットワークアドレスのみ "10.0.0.0" でも一致させる。"""
    net_only = prefix.split('/')[0]
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.split(':')[-1] == 'prefix' and isinstance(v, str):
                if v == prefix or v.split('/')[0] == net_only:
                    hits.append(obj)
                    break
        for v in obj.values():
            hits += _find_route_dicts(v, prefix)
    elif isinstance(obj, list):
        for it in obj:
            hits += _find_route_dicts(it, prefix)
    return hits


def _route_matches(route, protocol=None, next_hop=None):
    """経路エントリ部分木が protocol / next_hop 制約を満たすか"""
    if protocol:
        want = protocol.lower()
        protos = [str(v).lower().replace('proto-', '')
                  for v in _collect_values(route, 'source-protocol')]
        protos += [str(v).lower().replace('proto-', '')
                   for v in _collect_values(route, 'protocol')]
        if not any(want in p for p in protos):
            return False
    if next_hop:
        nhs = [str(v) for v in _collect_values(route, 'next-hop-address')]
        nhs += [str(v) for v in _collect_values(route, 'next-hop')]
        if not any(next_hop in n for n in nhs):
            return False
    return True


def _eval_restconf_item(entry, it, verbose=False):
    """RESTCONF項目を評価。対応形式:
      {"path": <RESTCONFデータパス>, "expect": <生JSON部分文字列>}
      {"path": ..., "all_equal": {"key": <キー名>, "value": <期待値>}}
      {"path": ..., "route_present": {"prefix": "10.0.0.0/24",
                                      "protocol": "ospf",       # 任意
                                      "next_hop": "10.1.12.2"}} # 任意
    戻り値: (ok: bool, label: str)
    """
    path = it['path']
    try:
        data = _restconf_get(entry, path)
    except Exception as e:
        return False, f"GET {path} -> {e}"
    if 'route_present' in it:
        spec = it['route_present']
        prefix = spec['prefix']
        proto = spec.get('protocol')
        nh = spec.get('next_hop')
        routes = _find_route_dicts(data, prefix)
        ok = any(_route_matches(r, proto, nh) for r in routes)
        cond = prefix + (f", proto={proto}" if proto else "") + \
            (f", nh={nh}" if nh else "")
        label = f"{path} : route_present({cond})  " \
                f"(prefix候補 {len(routes)}件)"
        return ok, label
    if 'all_equal' in it:
        key = it['all_equal']['key']
        want = str(it['all_equal']['value']).lower()
        vals = [str(v).lower() for v in _collect_values(data, key)]
        ok = bool(vals) and all(v == want for v in vals)
        label = (f"{path} : all '{key}' == '{want}'  "
                 f"(found {len(vals)}: {vals[:5]})")
        return ok, label
    if 'expect' in it:
        raw = json.dumps(data, ensure_ascii=False)
        ok = it['expect'] in raw
        return ok, f"{path} : contains '{it['expect']}'"
    return False, f"{path} : 不正なcheck定義"


# ── verify: show(SSH) / RESTCONF(GET) を実行しチェック ────────
def cmd_verify(args):
    inv = json.load(open(args.inventory))
    checks = json.load(open(args.checks))
    rc = 0
    for dev_id, items in checks.items():
        if dev_id.startswith('_'):   # _comment 等のメタキーは無視
            continue
        entry = inv.get(dev_id)
        if not entry or not entry.get('host'):
            print(f"[SKIP] {dev_id}: inventory に host なし")
            continue
        ssh_items = [it for it in items if 'cmd' in it]
        rc_items = [it for it in items if 'path' in it]
        # RESTCONF（SSH接続不要）
        for it in rc_items:
            ok, label = _eval_restconf_item(entry, it, args.verbose)
            print(f"  [{'PASS' if ok else 'FAIL'}] {dev_id} (restconf): {label}")
            if not ok:
                rc = 1
        # SSH(netmiko) は必要なときだけ接続
        if ssh_items:
            try:
                conn = _connect(entry)
                conn.enable()
                for it in ssh_items:
                    out = conn.send_command(it['cmd'])
                    ok = it['expect'] in out
                    print(f"  [{'PASS' if ok else 'FAIL'}] {dev_id} (ssh): "
                          f"'{it['cmd']}' expect '{it['expect']}'")
                    if not ok:
                        rc = 1
                        if args.verbose:
                            print(out)
                conn.disconnect()
            except Exception as e:
                print(f"[FAIL] {dev_id} (ssh): {e}")
                rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description="EVE-NG deploy/verify via netmiko(SSH)")
    sub = ap.add_subparsers(dest='mode', required=True)

    pe = sub.add_parser('export', help='構成をファイルへ書き出す')
    pe.add_argument('--api', default='http://127.0.0.1:8099')
    pe.add_argument('--out', default='./eveng_out')
    pe.add_argument('--platform', default='generic', choices=['generic', 'c3650'],
                    help='インターフェース名を実機体系へ変換 (c3650: Gi1/0/x, Te1/1/x)')

    pd = sub.add_parser('deploy', help='running-config を投入')
    pd.add_argument('--inventory', required=True)
    pd.add_argument('--verbose', action='store_true')

    pv = sub.add_parser('verify', help='show を実行しチェック')
    pv.add_argument('--inventory', required=True)
    pv.add_argument('--checks', required=True)
    pv.add_argument('--verbose', action='store_true')

    args = ap.parse_args()
    if args.mode == 'export':
        cmd_export(args); return 0
    if args.mode == 'deploy':
        return cmd_deploy(args)
    if args.mode == 'verify':
        return cmd_verify(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
