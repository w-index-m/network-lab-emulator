# BIG-IP / F5OS 診断・バックアップ 一括取得（qkview / UCS）

複数の BIG-IP から **qkview** と **UCS / F5OSバックアップ** を一括取得しローカル保存するツール。
`tools/bigip_qkview_collector.py`（Windows用 `.bat` 同梱）。

- **TMOS**: SSH(paramiko) で `qkview` / `tmsh save sys ucs` を実行 → SCP回収。
- **F5OS**: **REST API(RESTCONF)** で qkview / config-backup を生成 → SCP回収。

> **実機検証状況**
> - **TMOS: qkview / UCS ともに実機取得OK（確認済み）**
> - F5OS: REST API 実装済み・**実機デバッグ中**（エンドポイント/完了判定/DLパスを調整中）

## 事前準備
```
pip install paramiko scp requests
```

## hosts.txt 書式
```
<IPアドレス>  [tmos|f5os]  [ホスト名]
```
- 2列目でプラットフォーム固定（省略時は自動判別 root→admin）
- **3列目=ホスト名**（ファイル名に使用。例: `LTM0344A-TMOS`）。省略時はIP
```
10.202.127.253   tmos   LTM0344A
10.202.254.68    f5os   LTM0344A
```

## 使い方
```bash
# qkview + UCS/バックアップ 両方（既定）
python tools/bigip_qkview_collector.py hosts.txt -p 'P@ssword'

# qkview のみ / UCS のみ
python tools/bigip_qkview_collector.py hosts.txt --mode qkview -p 'P@ss'
python tools/bigip_qkview_collector.py hosts.txt --mode ucs    -p 'P@ss'

# 鍵認証・保存先指定
python tools/bigip_qkview_collector.py hosts.txt -k ~/.ssh/id_rsa -o ./bigip_output
```

### Windows ワンクリック
`tools/` 内で（同ディレクトリの `hosts.txt` を参照）:
- `get_qkview.bat` … qkview のみ
- `get_ucs.bat` … UCS / バックアップ のみ

## プラットフォーム別の挙動
| 項目 | TMOS | F5OS |
|---|---|---|
| 生成方式 | SSH(tmsh) | REST API(RESTCONF) |
| 既定ユーザ | `root` | `admin` |
| qkview生成 | `qkview -f /var/tmp/<name>.qkview` | `.../f5-diagnostics:qkview/capture` → status ポーリング |
| qkview回収 | `/var/tmp/` から SCP | `diags/shared/qkview/<name>` を SCP |
| バックアップ生成 | `tmsh save sys ucs /var/local/ucs/<name>.ucs` | `.../f5-database:database/config-backup` |
| バックアップ回収 | `/var/local/ucs/` から SCP | `configs/<name>` を SCP |
| リモート掃除 | qkview/UCSを `rm -f` | 残置 |

## オプション
| オプション | 既定 | 説明 |
|---|---|---|
| `-p, --password` | 対話入力 | SSH/API パスワード |
| `-k, --key-file` | — | SSH 秘密鍵 |
| `-o, --output-dir` | `bigip_output` | 保存先（`qkview/`・`ucs/`・`backup/`） |
| `--mode` | `all` | `qkview` / `ucs` / `all` |

## 注意点
- qkview生成は数分。タイムアウトは qkview=600s / UCS=300s。
- 認証・TLSはラボ運用前提（F5OS APIは自己署名のため `verify=False`）。運用ではCA/アカウント分離を。
- F5OS API はまだ実機デバッグ中。詰まる場合は `qkview_collector.log` を確認。
