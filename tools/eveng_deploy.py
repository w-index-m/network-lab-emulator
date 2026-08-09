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


# ── verify: show を実行し期待文字列をチェック ─────────────────
def cmd_verify(args):
    inv = json.load(open(args.inventory))
    checks = json.load(open(args.checks))   # {dev_id: [{"cmd":..,"expect":..}, ...]}
    rc = 0
    for dev_id, items in checks.items():
        entry = inv.get(dev_id)
        if not entry or not entry.get('host'):
            print(f"[SKIP] {dev_id}: inventory に host なし")
            continue
        try:
            conn = _connect(entry)
            conn.enable()
            for it in items:
                out = conn.send_command(it['cmd'])
                ok = it['expect'] in out
                print(f"  [{'PASS' if ok else 'FAIL'}] {dev_id}: '{it['cmd']}' "
                      f"expect '{it['expect']}'")
                if not ok:
                    rc = 1
                    if args.verbose:
                        print(out)
            conn.disconnect()
        except Exception as e:
            print(f"[FAIL] {dev_id}: {e}")
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
