# LogicMonitor REST API v3 エミュレーション

`engine/logicmonitor.py` / `tests/test_logicmonitor_api.py`

## これは何か

LogicMonitorは**完全SaaS型**の監視製品で、Nexpose/InsightVMや
FITELnet/Si-Rのような「実機/オンプレ配布物」が存在しない
（監視対象を巡回する`Collector`というエージェントはあるが、Collector
自体はLogicMonitorのクラウドに向けて通信するだけで、API自体はSaaS側
にしかない）。そのため「LogicMonitorを動かす」ことはできないが、
Nexposeの時と同じ考え方で、**このエミュレータ内にREST API v3の一部
（Device/Alert）を実装**し、装置側の実際の状態（インタフェースの
up/down）がそのままAlertに反映される、という運用のループを再現した。

## LMv1署名認証

実機のLogicMonitor REST APIは、Basic認証でもBearerトークンでもなく
**LMv1**という独自のHMAC署名方式を使う。クライアントは:

```
Authorization: LMv1 <AccessId>:<Signature>:<Epoch>
```

というヘッダーを送る。`Signature`は

```
Base64(HMAC-SHA256(AccessKey, Method + Epoch + RequestBody + ResourcePath))
```

で計算する。`Method`/`ResourcePath`は大文字小文字を含め正確に
一致させる必要があり、`RequestBody`はGETの場合は空文字列。
`Epoch`はミリ秒単位のUNIX時刻で、実機同様このエミュレータも
時計のずれが大きすぎる（既定300秒超）リクエストは拒否する。

このエミュレータでは`LM_ACCESS_ID`/`LM_ACCESS_KEY`
（既定: `emulator-access-id`/`emulator-access-key`、Nexposeの
admin/adminと同じ「デモ用の固定資格情報」という位置づけ）を使う。

認証は`app.py`の`session_auth_middleware`内、`/santaba/rest/`
プレフィックスの分岐で行っている（Nexpose/RESTCONFのBasic認証、
通常APIのセッショントークンとは別の3つ目の認証方式）。

## 実装したエンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/santaba/rest/device/devices` | Device一覧 |
| POST | `/santaba/rest/device/devices` | Device登録（`_device_id`でこのエミュレータの実装置と紐付ける） |
| GET | `/santaba/rest/device/devices/{id}` | Device詳細 |
| DELETE | `/santaba/rest/device/devices/{id}` | Device削除 |
| GET | `/santaba/rest/alert/alerts` | Alert一覧（`?deviceId=`で絞り込み可） |

実機のREST API v3はこの他にDataSource・Collector・Dashboard等
多数のリソースを持つが、Nexposeの時と同じく「Device登録→実際の
状態に応じたAlert」という筋が通る最小限に絞っている。

## Alertは実際の装置状態から動的に組み立てる

Nexposeの脆弱性判定と同じ考え方: Alertは固定の一覧ではなく、
Device登録時に紐付けた`_device_id`（このエミュレータの実装置ID）の
**実際のインタフェース状態**を見て、その場で組み立てる。

### 「意図した状態」はアラートにしない（実機と同じ設計）

実機のLogicMonitorの既定インタフェースDataSourceは、
`ifAdminStatus=up`かつ`ifOperStatus=down`のポートだけをアラート
対象にする。ケーブルが挿さっていないだけの`notconnect`ポートや、
管理者が意図的に`shutdown`したポートまでアラートにすると、
素のスイッチを繋いだだけで大量のノイズになってしまうため。

このエミュレータも同じ設計にしている:

```python
# 意図した状態ではない down だけを拾う
_UNEXPECTED_DOWN_STATUSES = {'down', 'err-disabled'}
```

最初にこれを実装した際、単純に「upでもconnectedでもなければ
アラート」という条件にしたところ、何も設定していない素のCatalyst
9300（未使用ポートが`notconnect`のまま多数残っている）を登録した
だけで**25件のアラートが立った**。実機ではまず起こらない挙動なので、
「意図的な状態は除外する」条件に直してから、Catalyst 9300の
デモ用err-disabledポート（bpduguard違反）1件だけが正しくアラート
になることを確認した。

## 実際に動かして確認した結果

エミュレータを実際に起動し、本物のLMv1クライアント（HMAC-SHA256で
署名するPythonスクリプト）から叩いた:

```
=== 1. 署名なしでアクセス(401) ===
401 {"status":401,"errmsg":"Unauthorized: missing or malformed Authorization header..."}

=== 2. 不正な署名でアクセス(401) ===
401 {"status":401,"errmsg":"Unauthorized: signature mismatch"}

=== 3. 正しい署名でデバイス登録 ===
200 {'id': 1, 'name': '10.50.0.1', 'displayName': 'LM-Demo-Switch',
     '_device_id': 'lm-demo', 'createdOn': 1789800292}

=== 4. GET /device/devices (正しい署名) ===
200 {'total': 1, 'items': [...]}

=== 5. アラート一覧(何も故障させていない状態) ===
200 {'total': 1, 'items': [{
  'instanceName': 'GigabitEthernet1/0/3', 'severity': 4, 'severityLabel': 'critical',
  'alertValue': 'err-disabled', 'detail': 'GigabitEthernet1/0/3 is err-disabled (expected up)'
}]}
```

（`GigabitEthernet1/0/3`はCatalyst 9300のデモ用固定フィクスチャで
bpduguard違反によりerr-disabledにしてある1ポート。それ以外の
未使用ポートは`notconnect`のままだが、アラートには出ない。）

期限切れのepoch（クロックスキュー超過）や、リクエストbodyを含めずに
計算した署名（実際に送るbodyと不一致）が正しく401になることも確認
した。

## 使い方

```python
import hashlib, hmac, base64, time, json, urllib.request

ACCESS_ID = 'emulator-access-id'
ACCESS_KEY = 'emulator-access-key'

def lmv1_signature(method, epoch, body, path):
    data = f'{method}{epoch}{body}{path}'
    digest = hmac.new(ACCESS_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(digest.encode()).decode()

epoch = str(int(time.time() * 1000))
body = json.dumps({'name': '10.1.1.1', 'displayName': 'My-Switch', '_device_id': 'my-switch'})
sig = lmv1_signature('POST', epoch, body, '/device/devices')

req = urllib.request.Request(
    'http://localhost:8000/santaba/rest/device/devices',
    data=body.encode(),
    headers={'Authorization': f'LMv1 {ACCESS_ID}:{sig}:{epoch}',
             'Content-Type': 'application/json'},
    method='POST')
urllib.request.urlopen(req)
```

## テスト

```bash
pytest tests/test_logicmonitor_api.py -v
# 18/18 成功
```

このテストファイルは他の大半のテストと違い、**`NETLAB_AUTH_DISABLE`
を意図的に外して**本物の認証ミドルウェアを動かした状態でLMv1署名の
検証まで確認している（`_AUTH_DISABLED`のグローバル状態をテストの
装置セットアップ操作の間だけ一時的に切り替え、モジュール終了時に
必ずTrueへ戻す。この復元を「読み込み時点の値を覚えておく」方式に
すると、pytestの収集順序次第でこのファイルが最初にapp.pyを
importした場合に元の値としてFalseを覚えてしまい機能しないことを、
他のテストファイルと一緒に実行して実際に確認した——そのため
`True`を直接復元する方式にしている）。

固定している内容：
1. LMv1署名の検証ロジック単体（欠落ヘッダ、不明なAccess ID、
   署名不一致、期限切れepoch、正しい署名の受理）
2. 署名がMethod/Epoch/Body/ResourcePathの組み合わせに依存すること
3. ミドルウェアを通した実際の401/200判定（署名なし、不正な署名、
   正しい署名、bodyを含めない署名でのPOST）
4. Device登録が実際のエミュレータ装置（`_device_id`）と紐付くこと
5. **インタフェースをshutdownすると実際にAlertが立つこと**
6. **未使用のnotconnectポートはAlertにならないこと**（このモジュールの
   設計上の要）
7. `deviceId`によるAlert絞り込み
8. 存在しないDeviceの削除は404、name無しでの登録は422

## 制約・今後の拡張余地

- DataSource/Collector/Dashboard等、実機の他の主要リソースは未実装
- Alertは「今の状態」を毎回動的に計算するだけで、Nexposeの
  スキャンのような「実行した時点のスナップショットを保存する」
  概念が無い（Alert履歴・ACK・SDT(メンテナンスウィンドウ)も無い）
- CPU使用率等、インタフェース状態以外のメトリクスに基づくAlertは
  未実装（SNMPダッシュボードのCPU値取得がMIBビルド処理と密結合して
  おり、素直に流用できなかったため今回は見送った）
- LMv1署名の`Method`はエンドポイント側で明示的に渡す必要があり、
  FastAPIのルーティングとは独立している（ミドルウェアで一括検証）
