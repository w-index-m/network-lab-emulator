# BIG-IP / F5OS 診断・バックアップ 一括取得（qkview / UCS）

複数の BIG-IP から **qkview**（診断アーカイブ）と **UCS / F5OS バックアップ** を
SSH で一括生成し、ローカルへ SCP ダウンロードするツール。
`tools/bigip_qkview_collector.py`（Windows 用ワンクリック `.bat` 同梱）。

> TMOS と F5OS で生成コマンド・保存先・既定ユーザが異なるため、接続後に自動判別して切り替える。

## できること

- **接続**: paramiko(SSH)。パスワード認証 / 秘密鍵認証。
- **一括**: `hosts.txt` に列挙した複数機器を順に処理、末尾にサマリー表示。
- **取得モード** `--mode`:
  - `qkview` … qkview のみ
  - `ucs` … UCS(TMOS) / バックアップ(F5OS) のみ
  - `all` … 両方（デフォルト）
- **自動判別**: `tmsh show sys version`→TMOS / `show system information`→F5OS。
- **認証切替**: TMOS=既定 `root` / F5OS=既定 `admin`。自動判別時は **root→admin** の順で試行。
- **転送**: SCP（進捗%表示）。TMOSは取得後リモート一時ファイルを削除。
- **ログ**: 標準出力＋`qkview_collector.log` に記録。

## プラットフォーム別の挙動

| 項目 | TMOS | F5OS |
|---|---|---|
| 判別コマンド | `tmsh show sys version` | `show system information` |
| 既定ユーザ | `root` | `admin` |
| qkview生成 | `qkview -f /var/tmp/<name>.qkview` | `system diagnostics qkview capture filename <name>` |
| qkview取得元 | `/var/tmp/` | `/var/export/chassis/diagnostics/qkview/` |
| バックアップ生成 | `tmsh save sys ucs /var/local/ucs/<name>.ucs` | `system backup create name <name>` |
| バックアップ取得元 | `/var/local/ucs/` | `/var/F5OS/backup/` |
| リモート掃除 | qkview/UCSを `rm -f` | 残置（管理領域のため） |

## hosts.txt 書式

1列目=ホスト（必須）。2列目=`tmos`/`f5os`（任意・省略で自動判別）。

```
192.168.1.1              # 自動判別（root→admin）
192.168.1.2   tmos       # TMOS 固定 (user=root)
192.168.1.3   f5os       # F5OS 固定 (user=admin)
```

> ユーザ名はプラットフォーム単位で `--tmos-username` / `--f5os-username` により変更。
> （このアップロード版は hosts.txt でのホスト別ユーザ指定は行わない）

## 使い方

```bash
pip install paramiko scp

# qkview + UCS/バックアップ 両方（既定）
python tools/bigip_qkview_collector.py hosts.txt -p 'P@ssword'

# qkview のみ / UCS のみ
python tools/bigip_qkview_collector.py hosts.txt --mode qkview -p 'P@ss'
python tools/bigip_qkview_collector.py hosts.txt --mode ucs    -p 'P@ss'

# TMOS/F5OSともadminで、鍵認証、保存先指定
python tools/bigip_qkview_collector.py hosts.txt --tmos-username admin --f5os-username admin -k ~/.ssh/id_rsa -o ./bigip_output
```

### Windows ワンクリック
`tools/` 内の以下を実行（同ディレクトリの `hosts.txt` を参照）:
- `get_qkview.bat` … qkview のみ（自動判別）
- `get_ucs.bat` … UCS / バックアップ のみ（自動判別）
- **`get_qkview_tmos.bat`** … qkview のみ・**TMOS固定**（`--platform tmos`。F5OS判別せずroot接続）
- **`get_ucs_tmos.bat`** … UCS のみ・**TMOS固定**

> **実機検証状況**: **TMOS / qkview は実機取得OK（2026-08-19）**。F5OS は取得フローが
> 異なる（capture→list→export）ため現状未対応の可能性あり（調査中）。当面 TMOS 運用は
> 上記 `*_tmos.bat` を使うと F5OS 判別で詰まらず確実です。

### プラットフォーム固定 `--platform`
`--platform {auto,tmos,f5os}`（既定 auto）で全ホストのプラットフォームを固定できます。
`tmos` 指定時は F5OS 判別を行わず TMOS 処理（root接続）に固定。

### 依存: paramiko 必須 / scp 任意
接続・回収は **paramiko** で完結します。`scp` が入っていれば進捗表示付きSCP、
無ければ **paramiko の SFTP** で自動フォールバックするので `scp` 未インストールでも動作します。
（`pip install paramiko` は必須、`scp` は任意）

追加オプションは `.bat` の後ろにそのまま渡せる（例: `get_qkview.bat -p P@ss`）。

## オプション

| オプション | 既定 | 説明 |
|---|---|---|
| `-p, --password` | 対話入力 | SSHパスワード |
| `-k, --key-file` | — | 秘密鍵ファイル |
| `-o, --output-dir` | `bigip_output` | ローカル保存先（`qkview/`・`ucs/`・`backup/` に振り分け） |
| `--mode` | `all` | `qkview` / `ucs` / `all` |
| `--tmos-username` | `root` | TMOSの既定ユーザ |
| `--f5os-username` | `admin` | F5OSの既定ユーザ |

## 注意点

- qkview生成は数分かかるため実行タイムアウトは qkview=600s / UCS=300s。
- SCP到達性（管理IPへSSH/SCP可）が前提。
- 認証情報はラボ運用向けの簡略前提。運用ではアカウント/鍵の分離を。
- この本ツール(擬似CLI)側の BIG-IP エミュレータは qkview/UCS を実生成しない。
  本スクリプトは **実機BIG-IP** に対して使う外部ツール（`tools/eveng_deploy.py` と同系統）。
