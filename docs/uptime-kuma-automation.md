# Uptime Kuma の監視項目をコード/CLIから操作する

**対象読者**: Claude以外のLLM（Qwen等）でも再現できるように、公式ドキュメントで
確認できる事実と、実際に動作確認したコード例のみを記載する。

## 結論

Uptime Kuma には REST API は無い。Web UI はバックエンドと **Socket.IO** で
通信しており、これが唯一の正規の自動化経路（公式ドキュメントに明記）。
公式のCLIツールは存在しないが、コミュニティ製Pythonライブラリ
`uptime-kuma-api` がSocket.IO部分を隠蔽しているため、これを薄くラップすれば
数十行で「追加・起動・停止・削除・一覧」ができるCLIを自作できる。
Windows / Linux / macOS のどれでも同じスクリプトがそのまま動く
（Pythonが動く環境であれば良く、Uptime Kuma本体がWindows上で動いている
必要はあるが、操作するクライアント側はどのOSでもよい）。

## 前提

```bash
pip install uptime-kuma-api
```

サーバーは事前に起動しておく（Windowsなら `node server\server.js` や
`pm2 start server/server.js --name uptime-kuma`、Dockerなら通常通り）。

## CLIラッパー（`uptime_kuma_cli.py`）

以下は実際に動作確認済みのAPI呼び出し（`add_monitor` / `resume_monitor` /
`pause_monitor` / `delete_monitor` / `get_monitors`）をそのまま
`argparse`でCLI化したもの。追加・依存を増やさず標準ライブラリの
`argparse`だけで組める。

```python
#!/usr/bin/env python3
"""Uptime Kuma 監視項目をコマンドラインから操作する薄いラッパー。

使い方:
    python uptime_kuma_cli.py list
    python uptime_kuma_cli.py add --name "onprem-rtr" --url http://10.100.0.1 --interval 60
    python uptime_kuma_cli.py start --id 3
    python uptime_kuma_cli.py stop --id 3
    python uptime_kuma_cli.py delete --id 3
"""
import argparse
import os
import sys

from uptime_kuma_api import MonitorType, UptimeKumaApi

KUMA_URL = os.environ.get("KUMA_URL", "http://localhost:3001")
KUMA_USER = os.environ.get("KUMA_USER", "admin")
KUMA_PASS = os.environ.get("KUMA_PASS", "")


def connect() -> UptimeKumaApi:
    api = UptimeKumaApi(KUMA_URL)
    api.login(KUMA_USER, KUMA_PASS)
    return api


def cmd_list(_args):
    with connect() as api:
        for m in api.get_monitors():
            print(f'{m["id"]:>4}  {m["active"]!s:>5}  {m["name"]}  ({m["url"] or m.get("hostname", "")})')


def cmd_add(args):
    with connect() as api:
        r = api.add_monitor(
            type=MonitorType.HTTP if args.url else MonitorType.PING,
            name=args.name,
            url=args.url,
            hostname=args.hostname,
            interval=args.interval,
        )
        print(f'added monitorID={r["monitorID"]}')


def cmd_start(args):
    with connect() as api:
        api.resume_monitor(args.id)
        print(f"resumed {args.id}")


def cmd_stop(args):
    with connect() as api:
        api.pause_monitor(args.id)
        print(f"paused {args.id}")


def cmd_delete(args):
    with connect() as api:
        api.delete_monitor(args.id)
        print(f"deleted {args.id}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p_add = sub.add_parser("add")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--url", default=None, help="HTTP監視の場合")
    p_add.add_argument("--hostname", default=None, help="PING監視の場合")
    p_add.add_argument("--interval", type=int, default=60)
    p_add.set_defaults(func=cmd_add)

    for name, fn in [("start", cmd_start), ("stop", cmd_stop), ("delete", cmd_delete)]:
        sp = sub.add_parser(name)
        sp.add_argument("--id", type=int, required=True)
        sp.set_defaults(func=fn)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

### 実行例

```powershell
# Windows (PowerShell)
$env:KUMA_URL = "http://localhost:3001"
$env:KUMA_USER = "admin"
$env:KUMA_PASS = "your-password"

python uptime_kuma_cli.py add --name "dx-onprem" --hostname 10.100.0.1 --interval 30
python uptime_kuma_cli.py list
python uptime_kuma_cli.py stop --id 1
python uptime_kuma_cli.py start --id 1
python uptime_kuma_cli.py delete --id 1
```

環境変数の代わりに引数化してもよいが、パスワードをコマンドライン引数
に直接書くとシェル履歴やプロセス一覧に残るため、環境変数か
`getpass`での対話入力を推奨。

## このリポジトリでの想定用途

`network-lab-emulator` 上でトポロジ（Cisco/Si-R等の装置）を追加・削除する
たびに、対応するUptime Kuma監視項目を上記CLIで同期させると、
「エミュレータ上の構成変更」と「監視対象」を連動させた運用リハーサルが
できる。例えば装置作成APIを呼ぶラッパースクリプト内で
`uptime_kuma_cli.py add`を続けて呼ぶ、といった組み合わせが考えられる
（今回は未実装、アイデアの記録のみ）。

## 参考

- Uptime Kuma本体: Socket.IOイベント名は `add` / `resume` (=resumeMonitor) /
  `pauseMonitor` / `deleteMonitor` / `getMonitorList` 系（フロントエンドの
  ソース `src/` 内で実際に使われているイベント名をベースに、
  `uptime-kuma-api`側のメソッド名で対応付けている）
- `uptime-kuma-api`（PyPI, コミュニティ製）: Socket.IOラッパー。
  本ドキュメントのCLI例はこのライブラリの上に薄く被せたもの
