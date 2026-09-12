# Nexpose / InsightVM Console API v3 エミュレーション

Rapid7 の脆弱性管理製品 **Nexpose / InsightVM** のコンソール API v3 を、
このエミュレータ上で再現したもの。

狙いは「スキャナを作ること」ではなく、
**装置側の設定が、そのまま脆弱性スキャンの結果に効く**という関係を
1本の線でつなぐこと。`snmp-server community public ro` を打てば
SNMP のデフォルトコミュニティが Critical として上がり、
`no snmp-server community` すれば次のスキャンで消える。
設定変更 → 再スキャン → リスクスコアの増減、という運用のループを
実機なしで回せる。

---

## 1. 仕様の出どころ

Rapid7 の公式ドキュメントサイト（`*.rapid7.com`）はこの実行環境の
egress プロキシで遮断されている（`403 CONNECT tunnel failed`）。
遮断を迂回はせず、Rapid7 自身が GitHub に公開している
公式クライアントの同梱 Swagger を一次情報として使った。

```
https://raw.githubusercontent.com/rapid7/vm-console-client-python/master/api-files/console-swagger.json
  Swagger 2.0 / InsightVM API v3
  206 paths, 315 definitions
  securityDefinitions: { "Basic": { "type": "basic" } }
```

この JSON 自体はリポジトリに取り込んでいない。gNMI の `.proto` と違い
実行時に必要な成果物ではなく、URL を参照すれば足りるため。

仕様から写し取ったのは次の 3 点：

| 項目 | 実機仕様 | 本実装 |
|---|---|---|
| 認証 | HTTP Basic | `/api/3/*` に Basic 認証（realm を分離） |
| ページング | `PageOf«T»` | `engine/nexpose.py: page_of()` |
| スキャン状態遷移 | `pause` / `resume` / `stop` のみ | 同じ 3 種。それ以外は 400 |
| 資格情報 | パスワードは書き込み専用 | GET しても秘密は返さない |
| 例外の状態 | `under-review` → `approved`/`rejected` | 同じ。再レビューは 400 |

---

## 2. 実装した範囲

206 エンドポイント全部ではなく、脆弱性スキャナとして筋が通る
最小の一本道だけを実装している。

```
Site 作成 → スキャン実行 → Asset 検出 → Vulnerability 検出 → Report 取得
```

| 分類 | エンドポイント |
|---|---|
| ルート | `GET /api/3`, `GET /api/3/administration/info` |
| Site | `GET/POST /api/3/sites`, `GET/DELETE /api/3/sites/{id}`, `PUT /api/3/sites/{id}/included_targets` |
| Scan | `POST/GET /api/3/sites/{id}/scans`, `GET /api/3/scans`, `GET /api/3/scans/{id}`, `POST /api/3/scans/{id}/{pause\|resume\|stop}` |
| Asset | `GET /api/3/sites/{id}/assets`, `GET /api/3/assets`, `GET /api/3/assets/{id}`, `.../services`, `.../vulnerabilities` |
| Vulnerability | `GET /api/3/vulnerabilities`, `GET /api/3/vulnerabilities/{id}`, `.../assets`, `.../solutions` |
| Report | `GET/POST /api/3/reports`, `POST /api/3/reports/{id}/generate`, `.../history`, `.../content` |
| Scan template | `GET /api/3/scan_templates`, `GET /api/3/scan_templates/{id}` |
| Credential | `GET/POST /api/3/sites/{id}/site_credentials`, `DELETE .../{cid}` |
| Exception | `GET/POST /api/3/vulnerability_exceptions`, `GET/DELETE .../{id}`, `POST .../{id}/{approve\|reject}` |

---

## 3. 検出のしくみ

`engine/nexpose.py` の `_services_of(state)` が、装置の `DeviceState` から
**実際に有効化されている管理サービス**をポート一覧に落とす。

| 装置側の設定 | 開くポート |
|---|---|
| `snmp-server community <name>` | 161/udp SNMP |
| `netconf-yang` | 830/tcp SSH |
| `gnxi server` | 50052/tcp gNMI |
| `restconf` | 443/tcp HTTPS |
| telnet 有効 | 23/tcp Telnet |

`VULN_CATALOG` の各エントリは検出条件を持ち、`_assess()` が突き合わせる。

| キー | 意味 |
|---|---|
| `match_port` | そのポートが開いていれば検出（非認証で分かる） |
| `check(state, ports)` | 装置の状態を読む関数 |
| `requires_auth` | 資格情報が登録されたサイトのスキャンでのみ評価する |

検出条件は内部情報なので API レスポンスには載せない
（`test_internal_match_rules_are_not_exposed` で固定）。

リスクスコアは Real Risk Score と同じ **0〜1000** スケールで、
Asset のスコアは所見の合計。

### スキャンテンプレート

| id | 脆弱性評価 |
|---|---|
| `discovery` | しない（サービスの発見まで） |
| `full-audit-without-web-spider`（既定） | する |
| `exhaustive` | する |

`discovery` ではポートは見つかるが `assessedForVulnerabilities: false` に
なり、所見は 0 件。

### 認証スキャン

サイトに資格情報（`site_credentials`）を登録すると、そのサイトのスキャンは
**認証スキャン**になり、`requires_auth` の所見まで評価される。
「外から見えるポート」と「config を読んで初めて分かること」を
分けるための仕組み。

認証スキャンでしか上がらない所見:

| id | 条件 |
|---|---|
| `netlab-no-aaa-authentication` | `aaa new-model` 未設定 |
| `netlab-weak-local-password` | privilege 15 のユーザのパスワードが 8 文字未満 |
| `netlab-snmp-rw-community` | 書き込み可のコミュニティがある |

パスワード／コミュニティ文字列は実機同様、GET しても返さない。

### 脆弱性例外

`POST /api/3/vulnerability_exceptions` で「この所見は許容する」を登録し、
`approve` で承認すると**次のスキャンから**件数にもリスクスコアにも
入らなくなる（登録しただけの `under-review` では効かない）。
スコープは `global` / `site` / `asset` の 3 種。

例外で落とした所見は消えたことにせず、レポートに `[excepted]` として残す。

### 実行例 — 是正ループ

装置 1 台を、discovery → 非認証 → 認証 → 是正 → 例外承認 と
進めたときの実際の出力。

```
1. discovery                       auth=False total=0 risk=    0.0
2. 非認証 full-audit                auth=False total=4 risk= 2635.0
       [+] netlab-ssh-weak-kex
       [+] netlab-snmp-default-community
       [+] netlab-grpc-no-tls
       [+] netlab-default-credentials
3. 認証あり                         auth=True  total=6 risk= 4111.0
       [+] netlab-ssh-weak-kex
       [+] netlab-snmp-default-community
       [+] netlab-grpc-no-tls
       [+] netlab-default-credentials
       [+] netlab-no-aaa-authentication     ← config を読んで初めて分かる
       [+] netlab-snmp-rw-community         ←
4. 是正後                           auth=True  total=2 risk=  802.0
       [+] netlab-ssh-weak-kex
       [+] netlab-grpc-no-tls
5. 例外承認後                       auth=True  total=1 risk=  214.0
       [+] netlab-ssh-weak-kex
       [excepted] netlab-grpc-no-tls
```

4 で打った是正コマンドはこれだけ:

```
no snmp-server community public
no snmp-server community wr1te
snmp-server community n0t-guessable ro
aaa new-model
username netadmin privilege 15 secret Str0ngP@ssw0rd
```

レポート:

```
Site 1: Lab Segment
  Assets: 1   Risk score: 214.0
  Vulnerabilities: total=1 critical=0 severe=0 moderate=1
    10.200.0.1       VULN-A           Cisco IOS XE 17.9.3
      [Moderate] SSH Server Supports Weak Key Exchange Algorithms
      [excepted] gNMI Service Exposed Without TLS
```

この 4111 → 802 → 214 が動くことがこの実装の肝で、
`test_remediation_loop_drives_the_risk_score_down` で一本にして守っている。

---

## 3.5 これを作る過程で見つかった不具合

「設定を直したら所見が消える」を通そうとして、初めて分かったもの。
どれも**最初の実装ではテストが通っていたのに間違っていた**。

1. **既定クレデンシャルの所見が絶対に消えなかった** —
   `_uses_default_creds()` が存在しない属性 `state.local_users` を
   読んでおり、常に「何も設定されていない」と判断していた。
   実際のローカルユーザは `state.users` にリストで入っている。
   最初のテストはこの所見が上がることを前提にしていたため、
   **バグのおかげで通っていた**。

2. **SNMP コミュニティ名を変えても所見が消えなかった** —
   タイトルは "Default Community Name (public)" なのに、
   検出条件がポート 161 が開いているかどうかだけだった。
   コミュニティ名を実際に見るよう `_chk_snmp_default_community` に変更。

3. **`no snmp-server community <name>` が未実装だった**（app.py 側） —
   `no snmp-server host` はあるのにコミュニティ側が無く、
   一度設定したコミュニティを消す手段が無かった。

4. **既存コミュニティの権限変更が無視されていた**（app.py 側） —
   `if not any(name == ...)` で既存名を丸ごと捨てていたため、
   `ro` → `rw` の変更が反映されなかった。実機は上書きする。

3・4 は Nexpose とは無関係の Catalyst CLI の穴で、
`tests/test_snmp_community_config.py` に回帰テストを置いた。
「スキャナ側を作ると装置側の穴が見える」という意味では、
この組み合わせ自体が検査になっている。

---

## 4. 重要な注意 — 脆弱性データは作り物

`VULN_CATALOG` の 5 件はこのエミュレータ専用の**架空の所見**で、
**CVE 番号もダミー**（`netlab-*` という ID を付けてある）。
実在機器・実在ファームウェアの脆弱性情報ではない。

この API を実機の脆弱性判断に使ってはいけない。
用途は「設定 → スキャン → レポート」という**手順の練習と自動化の検証**に限る。

---

## 5. 未対応の範囲

意図的に実装していないもの：

- 認証・権限（`/api/3/users`, `/api/3/roles`, Asset Group の権限境界）
- 実際のポートスキャン／バナー取得。到達性は
  `device_sessions` のモデルを読むだけで、パケットは出していない。
  資格情報も**照合していない**（登録されていれば認証成功とみなす）
- Scan Engine / Engine Pool、スケジュールスキャン、非同期実行
  （`POST .../scans` はその場で完了して `finished` を返す）
- Scan Template のチューニング（3種類の固定テンプレートのみ）
- Policy / SCAP・CIS ベンチマーク
- レポートの実ファイル出力（PDF/CSV）。`/content` はプレーンテキスト
- Tag、Asset Group、Solution のロールアップ
- 例外の期限切れ（`expires`）と、既存スキャン結果への遡及適用
  （承認は**次のスキャンから**効く）
- 永続化。プロセス再起動で Site もスキャン結果も消える

---

## 6. テスト

`tests/test_nexpose_api.py`（55 件）と
`tests/test_snmp_community_config.py`（10 件）。

固定しているのは主にこの 5 点：

1. **設定連動** — 開いたサービスが多い装置ほど所見が多い
2. **所見が消えること** — コミュニティ名を変える／ユーザを作り直すと
   該当の所見が消える（§3.5 の 1・2 の回帰テスト）
3. **遷移の正しさ** — `finished` のスキャンは `pause` できない、
   例外は二度レビューできない、未知のステータスは 400
4. **見えてはいけないものが見えないこと** — 非認証スキャンで
   `requires_auth` の所見が上がらない、資格情報の秘密が GET で返らない、
   検出条件（`match_*` / `check`）が API に漏れない
5. **再スキャンで Asset が重複しない**、`included_targets` で
   スキャン対象が絞られる、例外のスコープが他サイトに漏れない

```bash
python3 -m pytest tests/test_nexpose_api.py tests/test_snmp_community_config.py -q
```

---

## 7. 使い方

```bash
# Site 作成
curl -u admin:admin -X POST localhost:8000/api/3/sites \
  -H 'Content-Type: application/json' \
  -d '{"name":"Lab Segment","scan":{"assets":{"includedTargets":{"addresses":["10.200.0.1"]}}}}'

# 認証スキャン用の資格情報（任意）
curl -u admin:admin -X POST localhost:8000/api/3/sites/1/site_credentials \
  -H 'Content-Type: application/json' \
  -d '{"name":"lab-ssh","account":{"service":"ssh","username":"netadmin","password":"Str0ngP@ss"}}'

# スキャン（templateId 省略時はサイトの scanTemplate）
curl -u admin:admin -X POST localhost:8000/api/3/sites/1/scans \
  -H 'Content-Type: application/json' -d '{"templateId":"discovery"}'
curl -u admin:admin -X POST localhost:8000/api/3/sites/1/scans -d '{}'

# 結果
curl -u admin:admin localhost:8000/api/3/sites/1/assets
curl -u admin:admin localhost:8000/api/3/vulnerabilities

# 所見を許容する（承認して初めて効く。効くのは次のスキャンから）
curl -u admin:admin -X POST localhost:8000/api/3/vulnerability_exceptions \
  -H 'Content-Type: application/json' \
  -d '{"vulnerability":"netlab-grpc-no-tls","reason":"Compensating Control","scope":{"type":"site","id":1}}'
curl -u admin:admin -X POST localhost:8000/api/3/vulnerability_exceptions/1/approve

# レポート
curl -u admin:admin -X POST localhost:8000/api/3/reports \
  -H 'Content-Type: application/json' \
  -d '{"name":"Lab Audit","scope":{"sites":[1]}}'
curl -u admin:admin -X POST localhost:8000/api/3/reports/1/generate
curl -u admin:admin localhost:8000/api/3/reports/1/content
```
