#!/usr/bin/env python3
"""
BIG-IP qkview 一括取得ツール（SSH/paramiko）

複数の BIG-IP へ SSH 接続し、qkview を生成してローカルへダウンロードする。
TMOS / F5OS を自動判別し、プラットフォームごとに生成コマンド・保存先・
既定ユーザ名を切り替える。

対応:
  - 接続: paramiko(SSH)。パスワード認証 / 秘密鍵認証。
  - 対象: hosts.txt に列挙した複数BIG-IPを一括処理。
  - 取得: qkview生成 → SFTPでローカルへ保存 → リモート一時ファイル削除。
  - 判別: TMOS(`tmsh show sys version`) / F5OS(`show system information`) を自動判定。
          hosts.txt 2列目でプラットフォーム固定も可。
  - 認証: 自動判別時は root→失敗ならadmin の順に試行。
          tmos は既定 root、f5os は既定 admin。hosts.txt / オプションで上書き。

前提:
  pip install paramiko

hosts.txt 書式（1列目=ホスト。2列目以降は任意・順不同）:
  192.168.1.1
  192.168.1.2  tmos
  192.168.1.3  f5os  admin
  # 行頭# はコメント

使用例:
  python tools/bigip_qkview_collector.py hosts.txt -p 'P@ssword'
  python tools/bigip_qkview_collector.py hosts.txt --tmos-username admin --f5os-username admin -p 'P@ss'
  python tools/bigip_qkview_collector.py hosts.txt -k ~/.ssh/id_rsa -o ./qkview_out
"""
import argparse
import getpass
import os
import sys
import time


TMOS_QKVIEW_DIR = "/var/tmp"
F5OS_QKVIEW_DIR = "/var/export/chassis/diagnostics/qkview"


def _connect(host, username, password=None, key_file=None, port=22, timeout=30):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=host, port=port, username=username,
                  timeout=timeout, allow_agent=False, look_for_keys=False)
    if key_file:
        kwargs['key_filename'] = os.path.expanduser(key_file)
    else:
        kwargs['password'] = password
    client.connect(**kwargs)
    return client


def _exec(client, cmd, timeout=1800):
    """コマンド実行。(exit_status, stdout, stderr) を返す。
    qkview生成は長時間かかるため timeout は既定30分。"""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout,
                                                get_pty=True)
    out = stdout.read().decode(errors='replace')
    err = stderr.read().decode(errors='replace')
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def detect_platform(client):
    """TMOS / F5OS を判別。判別不能なら None。
      - TMOS: `tmsh show sys version` に "Sys::Version"/"Product" が出る
      - F5OS: `show system information` に "F5OS"/"os-version" が出る"""
    rc, out, _ = _exec(client, "tmsh show sys version", timeout=60)
    if rc == 0 and ("Sys::Version" in out or "Product" in out):
        return "tmos"
    rc2, out2, _ = _exec(client, "show system information", timeout=60)
    low2 = out2.lower()
    if rc2 == 0 and ("f5os" in low2 or "os-version" in low2
                     or "system information" in low2):
        return "f5os"
    # tmsh が何か返していれば TMOS とみなすフォールバック
    if rc == 0 and out.strip():
        return "tmos"
    return None


def collect_tmos(client, host, local_dir, remote_name):
    remote_path = f"{TMOS_QKVIEW_DIR}/{remote_name}"
    # tmsh 経由で qkview 生成
    rc, out, err = _exec(client, f"run /util qkview -f {remote_path}")
    if rc != 0:
        # tmsh でなくbashシェルの場合のフォールバック
        rc, out, err = _exec(client, f"qkview -f {remote_path}")
    if rc != 0:
        raise RuntimeError(f"qkview生成失敗(TMOS): {err or out}")
    local_path = _sftp_get(client, remote_path, local_dir)
    _exec(client, f"rm -f {remote_path}", timeout=60)  # リモート掃除
    return local_path


def collect_f5os(client, host, local_dir, remote_name):
    # F5OS: 診断qkviewをキャプチャ
    rc, out, err = _exec(
        client, f"system diagnostics qkview capture filename {remote_name}")
    if rc != 0:
        raise RuntimeError(f"qkview生成失敗(F5OS): {err or out}")
    remote_path = f"{F5OS_QKVIEW_DIR}/{remote_name}"
    local_path = _sftp_get(client, remote_path, local_dir)
    return local_path


def _sftp_get(client, remote_path, local_dir):
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, os.path.basename(remote_path))
    sftp = client.open_sftp()
    try:
        sftp.get(remote_path, local_path)
    finally:
        sftp.close()
    return local_path


def process_host(entry, args):
    host = entry['host']
    platform = entry.get('platform')          # tmos / f5os / None(自動)
    forced_user = entry.get('username')       # hosts.txtで個別指定
    ts = time.strftime("%Y%m%d-%H%M%S")
    remote_name = f"{host.replace(':', '_')}_{ts}.qkview"

    # ユーザ名候補の決定
    if forced_user:
        user_candidates = [forced_user]
    elif platform == 'tmos':
        user_candidates = [args.tmos_username]
    elif platform == 'f5os':
        user_candidates = [args.f5os_username]
    else:
        # 自動判別: root → admin の順で接続試行
        user_candidates = [args.tmos_username, args.f5os_username]
        # 重複除去（順序維持）
        seen = set()
        user_candidates = [u for u in user_candidates
                           if not (u in seen or seen.add(u))]

    last_err = None
    client = None
    used_user = None
    for user in user_candidates:
        try:
            client = _connect(host, user, password=args.password,
                              key_file=args.key, port=args.port)
            used_user = user
            break
        except Exception as e:
            last_err = e
            continue
    if client is None:
        return False, f"接続失敗: {last_err}"

    try:
        if not platform:
            platform = detect_platform(client)
            if not platform:
                return False, "プラットフォーム判別不能"
        print(f"  [{host}] platform={platform} user={used_user} -> qkview生成中…")
        if platform == 'tmos':
            local = collect_tmos(client, host, args.output, remote_name)
        else:
            local = collect_f5os(client, host, args.output, remote_name)
        return True, local
    except Exception as e:
        return False, str(e)
    finally:
        client.close()


def parse_hosts(path):
    entries = []
    for raw in open(path):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        entry = {'host': parts[0]}
        for tok in parts[1:]:
            if tok.lower() in ('tmos', 'f5os'):
                entry['platform'] = tok.lower()
            else:
                entry['username'] = tok    # プラットフォーム語以外はユーザ名扱い
        entries.append(entry)
    return entries


def main():
    ap = argparse.ArgumentParser(
        description="BIG-IP qkview 一括取得 (TMOS/F5OS 自動判別, SSH)")
    ap.add_argument('hosts', help='対象BIG-IP一覧ファイル(hosts.txt)')
    ap.add_argument('-p', '--password', help='SSHパスワード(未指定なら対話入力)')
    ap.add_argument('-k', '--key', help='秘密鍵ファイル(パスワードの代わり)')
    ap.add_argument('--port', type=int, default=22)
    ap.add_argument('--tmos-username', default='root',
                    help='TMOSの既定ユーザ名 (default: root)')
    ap.add_argument('--f5os-username', default='admin',
                    help='F5OSの既定ユーザ名 (default: admin)')
    ap.add_argument('-o', '--output', default='./qkview_out',
                    help='ローカル保存先ディレクトリ')
    args = ap.parse_args()

    if not args.key and not args.password:
        args.password = getpass.getpass("SSH password: ")

    entries = parse_hosts(args.hosts)
    if not entries:
        print("対象ホストがありません", file=sys.stderr)
        return 1

    print(f"対象 {len(entries)} 台 / 保存先 {args.output}")
    ok, ng = 0, 0
    for e in entries:
        success, msg = process_host(e, args)
        if success:
            print(f"[OK]   {e['host']} -> {msg}")
            ok += 1
        else:
            print(f"[FAIL] {e['host']}: {msg}")
            ng += 1
    print(f"\n完了: 成功 {ok} / 失敗 {ng}")
    return 0 if ng == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
