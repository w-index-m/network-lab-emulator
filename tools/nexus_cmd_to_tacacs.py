#!/usr/bin/env python3
"""
Nexus(等)投入コマンド -> TACACS+ コマンド認可/アカウンティング転送ツール

network-lab-emulatorはまだTACACS+クライアント機能を持っていない
（装置側からAAAサーバーへ実際に問い合わせる実装は無い）。この制約を
明示した上で、実際のTACACS+ワイヤプロトコル（RFC8907）で本物の
TACACS+サーバーに「コマンド認可(authorize)」「アカウンティング
(account)」リクエストを送る橋渡しツール。

エミュレーターのCLIに投入したコマンドを、実際に本物のTACACS+
サーバー（tools/... で起動したtac_plus-ng等）へ転送し、サーバー側の
ログ(author.log/accounting.log)に記録されることを確認できる。

使い方:
  # コマンド認可+アカウンティング(操作ログ相当)
  python tools/nexus_cmd_to_tacacs.py \
      --tacacs-host 127.0.0.1 --tacacs-key demo \
      --device nexus --user demo --command "show version"

  # ログイン(EXEC start)/ログアウト(EXEC stop)のアカウンティング
  python tools/nexus_cmd_to_tacacs.py \
      --tacacs-host 127.0.0.1 --tacacs-key demo \
      --device nexus --user demo --session login
  python tools/nexus_cmd_to_tacacs.py \
      --tacacs-host 127.0.0.1 --tacacs-key demo \
      --device nexus --user demo --session logout
"""

import argparse
import sys

from tacacs_plus.client import TACACSClient
from tacacs_plus.flags import TAC_PLUS_ACCT_FLAG_START, TAC_PLUS_ACCT_FLAG_STOP


def send_command_accounting(client: TACACSClient, args) -> int:
    """コマンド認可(authorize) + アカウンティング(操作ログ相当)"""
    cmd_parts = args.command.split()
    cmd_name = cmd_parts[0]
    cmd_args = cmd_parts[1:]
    auth_args = [b'service=shell', f'cmd={cmd_name}'.encode()] + \
                [f'cmd-arg={a}'.encode() for a in cmd_args] + [b'cmd-arg=<cr>']

    print(f'💬 装置: {args.device} / ユーザー: {args.user} / コマンド: "{args.command}"')
    print('🔍 TACACS+へ認可(authorize)リクエスト送信...')
    try:
        authz = client.authorize(args.user, arguments=auth_args, rem_addr=args.device)
        print(f'   -> {authz.human_status} (valid={authz.valid})')
    except Exception as e:
        print(f'❌ authorize失敗: {e}', file=sys.stderr)
        return 1

    print('📝 TACACS+へアカウンティング(start/stop)送信...')
    try:
        client.account(args.user, TAC_PLUS_ACCT_FLAG_START, arguments=auth_args, rem_addr=args.device)
        client.account(args.user, TAC_PLUS_ACCT_FLAG_STOP, arguments=auth_args, rem_addr=args.device)
        print('   -> 送信完了')
    except Exception as e:
        print(f'❌ accounting失敗: {e}', file=sys.stderr)
        return 1

    print('✅ TACACS+サーバーのauthor.log / accounting.logに記録されました')
    return 0


def send_session_accounting(client: TACACSClient, args) -> int:
    """ログイン(EXEC start)/ログアウト(EXEC stop)のアカウンティング
    (aaa accounting default group <name> 相当)"""
    session_args = [b'service=shell', b'task_id=1']
    flag = TAC_PLUS_ACCT_FLAG_START if args.session == 'login' else TAC_PLUS_ACCT_FLAG_STOP
    label = 'ログイン(START)' if args.session == 'login' else 'ログアウト(STOP)'

    print(f'💬 装置: {args.device} / ユーザー: {args.user} / セッション: {label}')
    print('📝 TACACS+へアカウンティング送信...')
    try:
        reply = client.account(args.user, flag, arguments=session_args, rem_addr=args.device)
        print(f'   -> {reply.human_status if hasattr(reply, "human_status") else reply}')
    except Exception as e:
        print(f'❌ accounting失敗: {e}', file=sys.stderr)
        return 1

    print(f'✅ TACACS+サーバーのaccounting.logに{label}が記録されました')
    return 0


def main():
    parser = argparse.ArgumentParser(description='装置投入コマンド/セッションをTACACS+サーバーへ転送')
    parser.add_argument('--tacacs-host', default='127.0.0.1')
    parser.add_argument('--tacacs-port', type=int, default=4949)
    parser.add_argument('--tacacs-key', default='demo')
    parser.add_argument('--device', required=True, help='投入元の装置名(rem_addrとして送る)')
    parser.add_argument('--user', default='demo')
    parser.add_argument('--command', help='実際に投入したコマンド文字列（コマンド認可/アカウンティング用）')
    parser.add_argument('--session', choices=['login', 'logout'],
                        help='ログイン/ログアウトのアカウンティングを送信（--commandとは排他）')
    args = parser.parse_args()

    if not args.command and not args.session:
        print('❌ --command か --session のどちらかを指定してください', file=sys.stderr)
        return 1

    client = TACACSClient(args.tacacs_host, args.tacacs_port, args.tacacs_key, timeout=5)

    if args.session:
        return send_session_accounting(client, args)
    return send_command_accounting(client, args)


if __name__ == '__main__':
    sys.exit(main())
