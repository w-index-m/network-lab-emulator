# Netmiko テスト・統合ガイド

このドキュメントは、Network Lab Emulator (エミュレーター) および実機環境での Netmiko テストについて説明します。

---

## 概要

### Netmiko 対応状況

| 機種 | Device Type | 対応状況 | SSH接続 | CLI送受信 |
|------|------------|--------|--------|---------|
| **Catalyst (IOS-XE)** | `cisco_ios` | ✅ 完全対応 | ✅ | ✅ |
| **Cisco ISR (IOS)** | `cisco_ios` | ✅ 完全対応 | ✅ | ✅ |
| **Cisco ASA** | `cisco_asa` | ✅ 完全対応 | ✅ | ✅ |
| **Nexus (NX-OS)** | `cisco_nxos` | ✅ 完全対応 | ✅ | ✅ |
| **Si-R (富士通)** | 標準未対応 | ⚠️ 代替: `generic_termserver` | △ | △ |
| **SR-S (富士通)** | 標準未対応 | ⚠️ 代替: `generic_termserver` | △ | △ |

---

## 実装済みテストツール

### 1. エミュレーター HTTP API テスト （実機不要）

**ファイル**: `tools/test_emulator_api.py`

エミュレーター上のCatalystに対して、HTTP APIを経由してCLIコマンドを送信し、設定投入・状態確認を行います。

#### セットアップ

```bash
# 依存なし（Python標準ライブラリのみ使用）
# ただし、エミュレーターサーバーが起動している必要があります
```

#### 実行

**ターミナル1: エミュレーターサーバー起動**
```bash
cd /home/user/network-lab-emulator
python app.py

# 出力例:
# INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

**ターミナル2: テスト実行**
```bash
# 基本実行
python tools/test_emulator_api.py

# ホストとポートを指定
python tools/test_emulator_api.py --host 127.0.0.1 --port 8000

# テスト対象デバイス指定
python tools/test_emulator_api.py --device catalyst
```

#### 期待される出力

```
======================================================================
🧪 Network Lab Emulator - Catalyst テスト
======================================================================

サーバー: http://localhost:8000
デバイス: catalyst

⏳ エミュレーター接続確認中...
✅ エミュレーター接続成功

======================================================================
📍 Test 1: インターフェース設定投入・確認
======================================================================
✅ インターフェース設定成功
   IP: 10.100.1.1/24

======================================================================
📍 Test 2: OSPF設定投入・確認
======================================================================
✅ OSPF設定成功
   プロセス ID: 1

📊 テスト結果レポート
======================================================================

合計: 6/6 テスト成功
  ✅ Interface Config
  ✅ OSPF Config
  ✅ BGP Config
  ✅ VLAN Config
  ✅ ACL Config
  ✅ Device State

======================================================================
```

#### テスト項目

1. **インターフェース設定** — GigabitEthernet1/0/1 に IP設定
2. **OSPF設定** — OSPF プロセス 1 設定
3. **BGP設定** — BGP AS 65001、neighbor 設定
4. **VLAN設定** — VLAN 100 作成・名前設定
5. **ACL設定** — ACL TEST_ACL 投入
6. **デバイス状態確認** — ホスト名、インターフェース、ルート確認

---

### 2. 実機・EVE-NG Netmiko テスト

**ファイル**: `tools/test_netmiko_integration.py`

実際のCatalystやCisco ISRに対して、netmiko経由でSSH接続し、設定変更と状態確認を行います。

#### セットアップ

```bash
# netmiko インストール
pip install netmiko

# 実機へのSSH接続確認（事前に実施）
# Catalyst/ISR に admin ユーザーで SSH ログイン可能であることを確認
ssh admin@192.168.1.100
```

#### 実行

**接続情報を直接指定**
```bash
python tools/test_netmiko_integration.py \
  --host 192.168.1.100 \
  --username admin \
  --password admin \
  --device-type cisco_ios
```

**環境変数で指定（推奨）**
```bash
export CATALYST_HOST=192.168.1.100
export CATALYST_USER=admin
export CATALYST_PASS=admin
export CATALYST_SECRET=admin    # enable password（オプション）

python tools/test_netmiko_integration.py --auto-env
```

**ポート・タイムアウトをカスタマイズ**
```bash
python tools/test_netmiko_integration.py \
  --host 192.168.1.100 \
  --port 2222 \           # SSH ポート (デフォルト: 22)
  --timeout 60            # タイムアウト秒数 (デフォルト: 30)
```

#### 期待される出力

```
======================================================================
🧪 Netmiko 実機テストツール
======================================================================

接続先: 192.168.1.100
Device Type: cisco_ios
User: admin

📡 デバイスへ接続中... 192.168.1.100
✅ 接続成功: 192.168.1.100

======================================================================
📍 Test 1: インターフェース設定投入・確認
======================================================================
✅ インターフェース設定投入成功
   出力: configure terminal
Enter configuration commands, one per line. End with CNTL/Z.
Dist-SW(config)#
✅ 設定確認成功 - IP アドレスが設定されている
   状態情報を取得

======================================================================
📍 Test 2: OSPF設定・隣接確認
======================================================================
✅ OSPF設定投入成功
✅ OSPF プロセス稼働確認

======================================================================
📊 テスト結果レポート
======================================================================

合計: 6/6 テスト成功
  ✅ Interface Config
  ✅ OSPF Config
  ✅ BGP Config
  ✅ VLAN Config
  ✅ ACL Config
  ✅ Device State

======================================================================
🔌 切断完了
```

#### テスト項目

1. **インターフェース設定** — IP設定投入・確認
2. **OSPF設定** — プロセス設定・隣接状態確認
3. **BGP設定** — AS・neighbor設定・セッション確認
4. **VLAN設定** — VLAN作成・確認
5. **ACL設定** — Access-list投入・確認
6. **デバイス状態取得** — ホスト名、インターフェース状態、ルーティングテーブル

---

### 3. Pytest 統合テスト

**ファイル**: `tests/test_netmiko_catalyst.py`

HTTP API テストと実機テストをpytest形式で実装しています。

#### 基本実行

```bash
# エミュレーター API版テスト（エミュレーター起動必須）
pytest tests/test_netmiko_catalyst.py::TestCatalystNetmikoStyle -v

# 実機テスト（実機接続情報が環境変数で設定されている場合）
NETMIKO_CATALYST_HOST=192.168.1.100 \
NETMIKO_USERNAME=admin \
NETMIKO_PASSWORD=admin \
pytest tests/test_netmiko_catalyst.py::TestNetmikoRealDeviceStub -v

# 全テスト実行
pytest tests/test_netmiko_catalyst.py -v
```

---

## 設定例・トラブルシューティング

### SSH接続失敗の場合

**症状**: `NetmikoTimeoutException` または `NetmikoAuthenticationException`

**対応**
```bash
# 1. SSH 接続確認（直接）
ssh -v admin@192.168.1.100
# → ユーザー名、パスワード、キー認証を確認

# 2. SSH ポートカスタマイズ
python tools/test_netmiko_integration.py \
  --host 192.168.1.100 \
  --port 2222 \
  --username admin \
  --password admin

# 3. タイムアウト時間を増加
python tools/test_netmiko_integration.py \
  --host 192.168.1.100 \
  --timeout 60
```

### エミュレーター接続失敗の場合

**症状**: `API呼び出し失敗`

**対応**
```bash
# 1. エミュレーター起動確認
ps aux | grep app.py

# 2. サーバーログ確認
python app.py 2>&1 | head -50

# 3. ポート確認
netstat -tuln | grep 8000
# または
lsof -i :8000

# 4. ファイアウォール確認（必要に応じて）
sudo ufw allow 8000
```

### Si-R / SR-S での Netmiko 利用

Si-R と SR-S はnetmiko標準未対応のため、以下の対応があります：

**eveng_deploy.py での対応**
```python
_NETMIKO_TYPE = {
    'sir': 'generic_termserver',   # 富士通Si-R: generic_termserver 代替
    'srs': 'generic_termserver',   # SR-S も同様
}
```

**制限事項**
- `generic_termserver` は基本的なtelnet/SSH接続のみサポート
- 機種固有のコマンド体系に対応していない可能性
- より詳細なテストにはカスタムドライバが必要

---

## EVE-NG での実機デプロイ

エミュレーターから生成した設定を実機（またはEVE-NG上の仮想機）に投入する場合は、`tools/eveng_deploy.py` を使用します。

```bash
# エミュレーターから設定をエクスポート
python tools/eveng_deploy.py export \
  --api http://127.0.0.1:8099 \
  --out ./out

# 実機へデプロイ（inventory.json を編集後）
python tools/eveng_deploy.py deploy \
  --inventory ./out/inventory.json

# 検証（show コマンドを実行）
python tools/eveng_deploy.py verify \
  --inventory ./out/inventory.json \
  --checks ./checks.json
```

詳細は `docs/eveng-deploy.md` を参照。

---

## API リファレンス

### エミュレーター HTTP API

#### CLI コマンド実行

```
POST /api/cli
Content-Type: application/json

{
  "device_id": "catalyst",
  "command": "show ip route"
}

レスポンス:
{
  "output": "Codes: C - connected, S - static, R - RIP, O - OSPF ...",
  "success": true
}
```

#### デバイス登録

```
POST /api/device
Content-Type: application/json

{
  "id": "catalyst",
  "type": "catalyst",
  "hostname": "Cat-Test"
}
```

#### ステータス確認

```
GET /api/status

レスポンス:
{
  "status": "running",
  "timestamp": "2026-08-30T12:34:56Z"
}
```

---

## ベストプラクティス

### 1. 環境変数を使った接続情報管理

```bash
# ~/.bash_profile または ~/.bashrc に追加
export CATALYST_HOST=192.168.1.100
export CATALYST_USER=admin
export CATALYST_PASS=admin

# ツール実行
python tools/test_netmiko_integration.py --auto-env
```

### 2. 複数デバイスのテスト

```bash
# Catalyst テスト
CATALYST_HOST=192.168.1.100 \
CATALYST_USER=admin \
CATALYST_PASS=admin \
python tools/test_netmiko_integration.py --auto-env

# ISR ルーターテスト
CATALYST_HOST=192.168.1.101 \
CATALYST_USER=admin \
CATALYST_PASS=admin \
python tools/test_netmiko_integration.py --auto-env --device-type cisco_ios
```

### 3. テスト結果の記録

```bash
# ログファイルに記録
python tools/test_emulator_api.py > test_results.log 2>&1

# レポート生成
python tools/test_netmiko_integration.py --auto-env | tee netmiko_test_$(date +%Y%m%d_%H%M%S).log
```

---

## 参考資料

- **Netmiko 公式ドキュメント**: https://github.com/ktbyers/netmiko
- **Netmiko 対応デバイス一覧**: https://github.com/ktbyers/netmiko/blob/develop/docs/index.md
- **Cisco IOS-XE コマンド**: https://www.cisco.com/c/en/us/td/docs/routers/ios-xe/release/release-notes.html

---

## 質問・バグ報告

テストツールの不具合や使い方に関する質問は、以下の手順で報告してください：

1. エラーメッセージの全文をコピー
2. 実行環境（OS、Python版、netmiko版）を記載
3. 実行コマンドを明記
4. GitHub Issues へ投稿

```bash
# 環境情報出力
python --version
pip show netmiko
uname -a
```
