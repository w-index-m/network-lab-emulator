# EVE-NG 実機連携（設定エクスポート → SSH投入 → 検証）

本エミュレータで組んだトポロジ／設定を、EVE-NG 上に立てた実機（IOS/NX-OS/ASA等）へ
**mgmt IP に SSH** で流し込み、show でチェックするワークフロー。

```
[本ツール] --/api/export--> [config一式+inventory] --netmiko(SSH)--> [EVE-NGノード]
   running-config             *.cfg / inventory.json          deploy → verify
```

## 1. エクスポート（投入はしない）

```bash
python tools/eveng_deploy.py export --api http://127.0.0.1:8099 --out ./eveng_out
```

生成物（`./eveng_out/`）:

| ファイル | 内容 |
|---|---|
| `<id>.cfg` | 各機器の running-config（そのまま `send_config_set` で投入） |
| `topology.json` | リンク一覧 `{a, iface_a, b, iface_b}`（EVE-NGの結線確認用） |
| `inventory.json` | netmiko接続情報の雛形（`device_type` / `host` / 認証） |

`netmiko_device_type` の対応:

| 本ツールの機種 | netmiko device_type |
|---|---|
| cisco / catalyst | `cisco_ios` |
| nexus | `cisco_nxos` |
| asa | `cisco_asa` |
| bigip / f5 | `f5_tmsh` |
| sir / srs / apresia | `generic_termserver`（標準未対応 → 要手当て） |

## 2. inventory.json を実機に合わせて編集

`host`（mgmt IP）は running-config 上で最初にIPが振られたIFを**推定値**として入れてあります。
EVE-NG 側で割り当てた管理IPと認証情報に**必ず上書き**してください。

```json
{
  "e1": {"device_type": "cisco_ios", "host": "192.0.2.11",
         "username": "admin", "password": "cisco", "secret": "cisco",
         "config_file": "./eveng_out/e1.cfg"}
}
```

## 3. 投入

```bash
pip install netmiko
python tools/eveng_deploy.py deploy --inventory ./eveng_out/inventory.json
```

各ノードへSSH → `enable` → `send_config_set` → `save_config`（`write memory`）。

## 4. 検証

`checks.json` に機器ごとの show と期待文字列を書く:

```json
{
  "e1": [
    {"cmd": "show ip ospf neighbor", "expect": "FULL"},
    {"cmd": "show ip route", "expect": "10.50.0.0"}
  ]
}
```

```bash
python tools/eveng_deploy.py verify --inventory ./eveng_out/inventory.json --checks checks.json
```

`expect` が出力に含まれれば PASS。全PASSで終了コード0。

## 注意点

- 本ツールのCLIは**簡略化擬似CLI**。Cisco系（IOS/NX-OS/ASA）はほぼ素通りするが、
  暗号系・一部プラットフォーム固有構文は実機で微調整が要る場合あり。
- Si-R（富士通）は netmiko 標準未対応のため `generic_termserver`。投入は
  `send_config_set` ではなくコマンド逐次送出になるので、必要なら専用ハンドラを足す。
- `host` 未設定のノードは deploy/verify で自動SKIP。
- **`deploy` は実機に書き込む破壊的操作**。まず1台で試してから全体へ。
```
