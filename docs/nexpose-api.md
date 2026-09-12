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

`VULN_CATALOG` の各エントリは `match_port` / `match_default_creds` という
検出条件を持ち、`_assess()` がこれを突き合わせて所見を立てる。
検出条件は内部情報なので、API レスポンスには載せない
（`test_internal_match_rules_are_not_exposed` で固定）。

リスクスコアは Real Risk Score と同じ **0〜1000** スケールで、
Asset のスコアは所見の合計。

### 実行例

```
### 2. スキャン実行 ###
{"id": 1, "status": "finished", "assets": 2,
 "vulnerabilities": {"critical": 3, "severe": 1, "moderate": 1, "total": 5}}

### 3. 検出されたAsset ###
  id=1 10.200.0.1  VULN-A  Cisco IOS XE 17.9.3  risk=2635.0  vulns=4 (C2/S1/M1)
      services: [(161, 'SNMP'), (830, 'SSH'), (50052, 'gNMI')]
  id=2 10.200.0.2  VULN-B  Cisco IOS XE 17.9.3  risk=961.0   vulns=1 (C1/S0/M0)
      services: []

--- レポート本文 ---
Site 1: Lab Segment
  Assets: 2   Risk score: 3596.0
  Vulnerabilities: total=5 critical=3 severe=1 moderate=1
    10.200.0.1  VULN-A  Cisco IOS XE 17.9.3
      [Moderate] SSH Server Supports Weak Key Exchange Algorithms
      [Critical] SNMP Agent Default Community Name (public)
```

管理サービスを開けた VULN-A が 4 件、
ローカルユーザだけ作って締めた VULN-B が 1 件。
この差が出ることがこの実装の肝で、
`test_open_management_services_drive_the_findings` で守っている。

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
  `device_sessions` のモデルを読むだけで、パケットは出していない
- Scan Engine / Engine Pool、スケジュールスキャン、非同期実行
  （`POST .../scans` はその場で完了して `finished` を返す）
- Credential（SSH/SMB 認証スキャン）、Scan Template のチューニング
- Policy / SCAP・CIS ベンチマーク
- レポートの実ファイル出力（PDF/CSV）。`/content` はプレーンテキスト
- Exception（例外承認ワークフロー）、Tag、Vulnerability Exception
- 永続化。プロセス再起動で Site もスキャン結果も消える

---

## 6. テスト

`tests/test_nexpose_api.py`（25 件）。

固定しているのは主にこの 3 点：

1. **設定連動** — 開いたサービスが多い装置ほど所見が多い
2. **遷移の正しさ** — `finished` のスキャンは `pause` できない、
   未知のステータスは 400
3. **再スキャンで Asset が重複しない**、`included_targets` で
   スキャン対象が絞られる

```bash
python3 -m pytest tests/test_nexpose_api.py -q
```

---

## 7. 使い方

```bash
# Site 作成
curl -u admin:admin -X POST localhost:8000/api/3/sites \
  -H 'Content-Type: application/json' \
  -d '{"name":"Lab Segment","scan":{"assets":{"includedTargets":{"addresses":["10.200.0.1"]}}}}'

# スキャン
curl -u admin:admin -X POST localhost:8000/api/3/sites/1/scans -d '{}'

# 結果
curl -u admin:admin localhost:8000/api/3/sites/1/assets
curl -u admin:admin localhost:8000/api/3/vulnerabilities

# レポート
curl -u admin:admin -X POST localhost:8000/api/3/reports \
  -H 'Content-Type: application/json' \
  -d '{"name":"Lab Audit","scope":{"sites":[1]}}'
curl -u admin:admin -X POST localhost:8000/api/3/reports/1/generate
curl -u admin:admin localhost:8000/api/3/reports/1/content
```
