# BIG-IP qkview 一括取得（TMOS / F5OS 自動判別）

複数の BIG-IP から qkview（診断アーカイブ）を SSH で一括生成し、ローカルへ
ダウンロードするツール。`tools/bigip_qkview_collector.py`。

> qkview = F5サポート提出用の診断スナップショット。TMOS と F5OS で生成コマンド・
> 保存先・既定ユーザが異なるため、接続後に自動判別して切り替える。

## できること

- **接続**: paramiko(SSH)。パスワード認証 / 秘密鍵認証。
- **一括**: `hosts.txt` に列挙した複数BIG-IPを順に処理。
- **取得**: qkview生成 → SFTPでローカル保存 → リモート一時ファイル削除（TMOS）。
- **自動判別**: `tmsh show sys version`→TMOS / `show system information`→F5OS。
- **認証切替**: TMOS=既定 `root` / F5OS=既定 `admin`。自動判別時は **root→admin** の順で試行。

## プラットフォーム別の挙動

| 項目 | TMOS | F5OS |
|---|---|---|
| 判別コマンド | `tmsh show sys version` | `show system information` |
| 既定ユーザ | `root` | `admin` |
| qkview生成 | `run /util qkview -f /var/tmp/<name>` | `system diagnostics qkview capture filename <name>` |
| 取得元 | `/var/tmp/` | `/var/export/chassis/diagnostics/qkview/` |
| リモート掃除 | 生成ファイルを `rm -f` | （残置） |

## hosts.txt 書式

1列目=ホスト（必須）。2列目以降は順不同・任意：
- `tmos` / `f5os` … プラットフォーム固定（省略時は自動判別）
- それ以外の語 … そのホスト専用のユーザ名（最優先）

```
# 例
192.168.1.1
192.168.1.2  tmos
192.168.1.3  f5os  admin
10.0.0.9  operator
```

## 使い方

```bash
pip install paramiko

# パスワード認証（未指定なら対話入力）
python tools/bigip_qkview_collector.py hosts.txt -p 'P@ssword'

# TMOS/F5OS ともに admin で入りたい場合
python tools/bigip_qkview_collector.py hosts.txt --tmos-username admin --f5os-username admin -p 'P@ss'

# 秘密鍵認証・保存先指定
python tools/bigip_qkview_collector.py hosts.txt -k ~/.ssh/id_rsa -o ./qkview_out
```

## オプション

| オプション | 既定 | 説明 |
|---|---|---|
| `-p, --password` | 対話入力 | SSHパスワード |
| `-k, --key` | — | 秘密鍵ファイル（パスワードの代わり） |
| `--tmos-username` | `root` | TMOSの既定ユーザ |
| `--f5os-username` | `admin` | F5OSの既定ユーザ |
| `-o, --output` | `./qkview_out` | ローカル保存先 |
| `--port` | `22` | SSHポート |

## 注意点

- qkview生成は数分かかるため、SSH実行タイムアウトは既定30分。
- SFTP到達性が前提（管理IPへSSH/SFTP可）。
- 認証情報はラボ運用向けの簡略前提。運用ではアカウント/鍵の分離を。
- この本ツール(擬似CLI)側の BIG-IP エミュレータは qkview を実生成しない。
  本スクリプトは **実機BIG-IP** に対して使う外部ツール（`tools/eveng_deploy.py` と同系統）。
