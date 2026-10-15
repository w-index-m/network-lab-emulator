#!/usr/bin/env python3
"""仮想クライアントツール: 複数の仮想クライアント(それぞれ別の送信元IP
を持つ)からVIPへ接続し、どのバックエンドに振り分けられたかを集計する。

ユーザー依頼「仮想クライアントを作るソフトなかったっけか？Pythonで
作った気がする。仮想クライアントのソフトありますか？」への対応——
このリポジトリを検索した限り該当するものは存在しなかったため新設した。
ALB/BIG-IPのロードバランシング・パーシステンス(セッション維持)・
SNAT(送信元IP変換)の試験で使う。

各仮想クライアントには、ベースIP(--base-ip、既定127.0.10.1)から
連番で割り当てた専用の送信元IPを持たせる(engine.loopback_aliasで
実際にループバックへエイリアスする——このエミュレータ本体のVIP/
SNATプールと同じ仕組みをそのまま流用する、本物のソケットバインド)。
同一client_idからの複数リクエストは常に同じ送信元IPから送られるため、
パーシステンス/スティッキーセッションの検証にそのまま使える。HTTP
モードはCookieJar相当(Set-Cookieを次回リクエストへ引き継ぐ)を各
仮想クライアントごとに個別に維持するので、ALBのstickinessテストに
直結する。

使い方:
  # ALB(HTTP)に5台の仮想クライアントからそれぞれ3回アクセスし、
  # 振り分け結果・負荷分散を集計する
  python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \\
      --clients 5 --requests 3 --path /

  # path-basedルールの確認(例: /api/*)
  python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \\
      --clients 3 --requests 2 --path /api/widgets

  # BIG-IP(SSH/TCP)に3台の仮想クライアントから接続し、実際にログイン
  # してプロンプトのホスト名まで確認する(--ssh-loginにはparamikoが
  # 必要)
  python3 tools/virtual_client.py tcp --vip 10.250.5.1 --port 2224 \\
      --clients 3 --requests 2 --ssh-login

  # --ssh-loginを付けなければ、生のTCPバナー(SSHなら"SSH-2.0-..."等)
  # を見るだけの軽量モード
  python3 tools/virtual_client.py tcp --vip 10.250.5.1 --port 2224 \\
      --clients 3 --requests 2
"""

import argparse
import http.client
import os
import socket
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)


def _assign_source_ip(base_ip: str, index: int) -> str:
    octets = base_ip.split('.')
    host_octet = int(octets[3]) + index
    return '.'.join(octets[:3] + [str(host_octet)])


def _try_alias(ip: str) -> bool:
    try:
        from engine.loopback_alias import ensure_loopback_alias
        return ensure_loopback_alias(ip)
    except Exception:
        return False


def run_http_client(vip, port, src_ip, path, requests_count, timeout=5.0):
    """1台の仮想クライアントがrequests_count回GETする。Set-Cookieは
    次回のリクエストへ引き継ぐ(ALBのstickinessテスト用)。"""
    results = []
    cookie_value = None
    for _ in range(requests_count):
        conn = http.client.HTTPConnection(vip, port, timeout=timeout, source_address=(src_ip, 0))
        try:
            headers = {'Host': vip, 'Connection': 'close'}
            if cookie_value:
                headers['Cookie'] = cookie_value
            conn.request('GET', path, headers=headers)
            resp = conn.getresponse()
            body = resp.read().decode(errors='replace').strip()
            set_cookie = resp.getheader('Set-Cookie')
            if set_cookie:
                cookie_value = set_cookie.split(';')[0]
            results.append({'backend': body, 'cookie': cookie_value})
        except OSError as e:
            results.append({'backend': None, 'error': str(e)})
        finally:
            conn.close()
    return results


def run_tcp_client(vip, port, src_ip, requests_count, ssh_login=False, timeout=5.0):
    """1台の仮想クライアントがrequests_count回接続する。--ssh-loginなら
    実際にSSHログインしてプロンプトのホスト名まで確認する(どの
    バックエンドが応答したか一目でわかる、BIG-IPの実デモで使った
    のと同じ手法)。"""
    results = []
    for _ in range(requests_count):
        try:
            if ssh_login:
                try:
                    import paramiko
                except ImportError:
                    results.append({'backend': None,
                                    'error': 'paramiko未導入(--ssh-loginにはpip install paramikoが必要)'})
                    continue
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.bind((src_ip, 0))
                sock.connect((vip, port))
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cli.connect(vip, port=port, username='admin', password='admin',
                           sock=sock, timeout=timeout, look_for_keys=False, allow_agent=False)
                chan = cli.invoke_shell()
                time.sleep(0.5)
                banner = chan.recv(2000).decode(errors='replace')
                lines = [l for l in banner.strip().splitlines() if l.strip()]
                results.append({'backend': lines[-1].rstrip('#>').strip() if lines else '(empty)'})
                cli.close()
            else:
                with socket.create_connection((vip, port), timeout=timeout,
                                              source_address=(src_ip, 0)) as s:
                    s.settimeout(timeout)
                    data = s.recv(256)
                    results.append({'backend': data.decode(errors='replace').strip()})
        except OSError as e:
            results.append({'backend': None, 'error': str(e)})
    return results


def main():
    ap = argparse.ArgumentParser(
        description='複数の仮想クライアントからVIPへ接続し、振り分け結果を集計する',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('mode', choices=['http', 'tcp'], help='http: ALB等のL7 VIP / tcp: BIG-IP等のL4 VIP')
    ap.add_argument('--vip', required=True, help='VIPのIPアドレス')
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--clients', type=int, default=3, help='仮想クライアント台数(既定3)')
    ap.add_argument('--requests', type=int, default=3, help='クライアント1台あたりのリクエスト回数(既定3)')
    ap.add_argument('--path', default='/', help='httpモード: GETするパス')
    ap.add_argument('--base-ip', default='127.0.10.1',
                     help='仮想クライアントの送信元IP割当の起点(既定127.0.10.1、client-Nは末尾+N)')
    ap.add_argument('--ssh-login', action='store_true', help='tcpモード: 実際にSSHログインしてホスト名を確認する')
    ap.add_argument('--timeout', type=float, default=5.0)
    args = ap.parse_args()

    client_backend_counts = defaultdict(Counter)
    overall_counts = Counter()
    errors = 0

    for i in range(args.clients):
        src_ip = _assign_source_ip(args.base_ip, i)
        if not _try_alias(src_ip):
            print(f'[warn] client-{i}: {src_ip} のループバックエイリアスに失敗——'
                  f'OS既定の送信元IPで試行します(root権限が無い環境等)', file=sys.stderr)
        if args.mode == 'http':
            results = run_http_client(args.vip, args.port, src_ip, args.path, args.requests, args.timeout)
        else:
            results = run_tcp_client(args.vip, args.port, src_ip, args.requests, args.ssh_login, args.timeout)
        print(f'\n=== client-{i} (src={src_ip}) ===')
        for n, r in enumerate(results):
            if r.get('backend') is not None:
                cookie_note = f'  cookie={r["cookie"]}' if r.get('cookie') else ''
                print(f'  request {n}: backend={r["backend"]!r}{cookie_note}')
                client_backend_counts[i][r['backend']] += 1
                overall_counts[r['backend']] += 1
            else:
                print(f'  request {n}: ERROR {r.get("error")}')
                errors += 1

    print('\n=== サマリー ===')
    print('クライアントごとの振り分け(1種類だけならパーシステンス/スティッキーセッションが効いている):')
    for i in range(args.clients):
        counts = client_backend_counts.get(i, Counter())
        if not counts:
            print(f'  client-{i}: (応答なし)')
            continue
        tag = 'STICKY' if len(counts) == 1 else 'DISTRIBUTED'
        print(f'  client-{i}: {dict(counts)}  [{tag}]')

    print('\n全体の振り分け(台数間での負荷分散の均等さ):')
    if overall_counts:
        for backend, count in overall_counts.most_common():
            print(f'  {backend}: {count}')
    else:
        print('  (応答なし)')

    if errors:
        print(f'\n{errors}件のリクエストでエラーが発生しました(上記ログ参照)', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
