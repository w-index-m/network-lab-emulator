#!/usr/bin/env python3
"""
CSVから複数装置を一括登録するツール。

Cisco DevNetのjohann (flopach/johann-network-device-monitoring) が持つ
「CSVで複数デバイスを一括追加する」機能を参考に、このエミュレータの
/api/device / /api/link に対して一括投入できるようにしたもの。

CSVフォーマット（1行目はヘッダー）:
    id,type,hostname
    leaf1,nexus,NX-Leaf1
    leaf2,nexus,NX-Leaf2

リンクも同時に張りたい場合は --links で別CSVを指定できる:
    a,b,iface_a,iface_b
    leaf1,leaf2,Ethernet1/1,Ethernet1/1

使い方:
    python tools/bulk_device_import.py devices.csv
    python tools/bulk_device_import.py devices.csv --links links.csv
    python tools/bulk_device_import.py devices.csv --base-url http://localhost:8000
"""

import argparse
import csv
import os
import sys

import httpx


def _login(base_url: str, user: str, password: str) -> str:
    r = httpx.post(f"{base_url}/api/login",
                   json={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise SystemExit(f"ログイン失敗: {data.get('error')}")
    return data["token"]


def import_devices(base_url: str, csv_path: str, token: str = "") -> list[str]:
    headers = {"X-Session-Token": token} if token else {}
    created = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            device_id = row["id"].strip()
            payload = {
                "id": device_id,
                "type": row["type"].strip(),
                "hostname": row.get("hostname", device_id).strip() or device_id,
            }
            r = httpx.post(f"{base_url}/api/device", json=payload,
                           headers=headers, timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                created.append(device_id)
                print(f"[ok]   {device_id} ({payload['type']}, {payload['hostname']})")
            else:
                print(f"[fail] {device_id}: HTTP {r.status_code} {r.text}", file=sys.stderr)
    return created


def import_links(base_url: str, csv_path: str, token: str = "") -> int:
    headers = {"X-Session-Token": token} if token else {}
    count = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            payload = {
                "a": row["a"].strip(), "b": row["b"].strip(),
                "iface_a": row["iface_a"].strip(), "iface_b": row["iface_b"].strip(),
            }
            r = httpx.post(f"{base_url}/api/link", json=payload,
                           headers=headers, timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                count += 1
                print(f"[ok]   link {payload['a']}({payload['iface_a']}) <-> "
                      f"{payload['b']}({payload['iface_b']})")
            else:
                print(f"[fail] link {payload['a']}<->{payload['b']}: "
                      f"HTTP {r.status_code} {r.text}", file=sys.stderr)
    return count


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("devices_csv", help="装置一覧CSV（列: id,type,hostname）")
    p.add_argument("--links", help="リンク一覧CSV（列: a,b,iface_a,iface_b）")
    p.add_argument("--base-url", default=os.environ.get("NETLAB_BASE_URL", "http://localhost:8000"))
    p.add_argument("--user", default=os.environ.get("NETLAB_AUTH_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("NETLAB_AUTH_PASS", "admin"))
    p.add_argument("--no-auth", action="store_true",
                   help="NETLAB_AUTH_DISABLE=1のサーバー向け。ログインをスキップする")
    args = p.parse_args()

    token = ""
    if not args.no_auth:
        token = _login(args.base_url, args.user, args.password)

    created = import_devices(args.base_url, args.devices_csv, token)
    print(f"\n装置 {len(created)} 台を登録しました。")

    if args.links:
        n = import_links(args.base_url, args.links, token)
        print(f"リンク {n} 本を作成しました。")


if __name__ == "__main__":
    main()
