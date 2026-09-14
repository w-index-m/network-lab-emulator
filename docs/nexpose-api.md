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

## 3. 検出のしくみ — 本物のソケットで確かめる

このエミュレータは NETCONF(830) / gNMI(50052) / SNMP(161) を
**本物のソケットで待ち受けている**ので、スキャナ側も本当に接続して
確かめる。`detectedBy` に、どう確かめたかが残る。

| detectedBy | 確認方法 |
|---|---|
| `tcp-connect` | 実際に TCP 接続できた |
| `snmp-get` | 実際に SNMP v2c GET (sysDescr) を投げて応答があった |
| `configuration` | 設定を読んだだけ（`probe: false` 指定時） |

走査するのは「設定から予想される候補ポート」＋`WELL_KNOWN_PORTS`
（22/23/80/443/830/9339/50051/50052/161）。候補だけを確かめる作りだと
**設定が消えたのにリスナーが残っている状態を見逃す**ので、
設定に出ていないポートも叩く。

```
実スキャン (probe=true)
   udp/161    SNMP    snmp-get
   tcp/830    SSH     tcp-connect
   tcp/50052  gNMI    tcp-connect

no gnxi server の後
   udp/161    SNMP    snmp-get
   tcp/830    SSH     tcp-connect        ← 50052 は消える
```

`probe: false` を渡すと、以前の「設定を読むだけ」の挙動に戻せる。

### 制約 — TestClient では SNMP を実プローブできない

FastAPI の `TestClient` は**リクエストを処理している間しか
イベントループを回さない**。SNMP UDP エージェントは同じループ上の
asyncio DatagramProtocol なので、リクエスト外に届いたパケットが
処理されず、実プローブだと 161 が常に閉じて見える
（ソケットは bind 済みで `Recv-Q` にパケットが溜まったままになる）。

そのため実プローブの確認は `tests/test_nexpose_real_scan.py` が
**本物の uvicorn サーバをサブプロセスで立てて**行っている。
`tests/test_nexpose_api.py` 側の API 挙動テストの多くは `probe: false`
を使うが、SNMPの実検出そのものを固定している一部のテスト
（`test_remediation_loop_drives_the_risk_score_down` 等）は
`probe: false` を使わず実プローブに依存している。

なお、スキャンは同期I/Oなので `POST .../scans` は
`asyncio.to_thread` 経由で呼んでいる。直接 await せずに呼ぶと
イベントループを止めてしまい、同じループ上の SNMP エージェントが
応答できず自分で自分を「閉じている」と誤検出する。

#### `TestClient` を `with` なしで使うと SNMPだけ実プローブが失敗する

上の制約には、もう一段落とし穴があった。`TestClient(app)` を
`with TestClient(app) as client:` を使わずに素朴に呼び出すと、
**リクエストごとに使い捨てのイベントループ（`anyio`のBlockingPortal）
が作られては閉じられる**。SNMP UDP エージェントは
`asyncio.get_event_loop()` でその時点の実行中ループに
DatagramProtocolを登録するため、「`ip address`/`snmp-server community`
を設定したリクエスト」と「実際にスキャンするリクエスト」が
別々の使い捨てポータルで動くと、スキャン側のポータルにはもう
存在しないループ宛てにパケットが届くだけになる。

TCPベースの検出（SSH/NETCONF/gNMI）はこの影響を受けない。
`connect()` によるTCPの3ウェイハンドシェイクはカーネルレベルで
完結し、アプリ側が`accept()`するイベントループの生死とは無関係に
成立するため。**UDPは応答を返す側のイベントループが生きている
必要がある**ぶん、この落とし穴が可視化された。

`ss -uapn` で見るとソケット自体は `bind` されたままなので
「開いてはいるが誰も見ていない」状態になり、`probe_snmp()`は
タイムアウトして`False`を返す。ログや`ss`だけでは
「ポートが閉じている」ようにしか見えないので気付きにくい。

対処は `tests/test_nexpose_api.py` の `client = TestClient(app)`
の直後で `client.__enter__()` を呼び、モジュール内の全テストで
同じ永続的なポータル（イベントループ）を使い回すようにすること
（`with`ブロックで囲むのと同じ効果。プロセス終了まで開けっぱなしで
問題ない）。これにより実行時間も短縮された（実測: 33秒→8秒。
リクエストのたびにイベントループを作り直すオーバーヘッドが
無くなるため）。

回帰テストは既存の `tests/test_nexpose_api.py::test_remediation_loop_drives_the_risk_score_down`
ほか、`probe: false` を使わずSNMP検出に依存する9件。

---

## 3.1 候補の出しかた

`engine/nexpose.py` の `candidate_services(state)` が、装置の
`DeviceState` から**有効化されている管理サービス**をポート一覧に落とす。

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

### 認証スキャン — 資格情報は本当に試す

サイトに資格情報（`site_credentials`）を登録すると、スキャン時に
**実際にログインを試す**。通った装置だけが認証スキャン扱いになり、
`requires_auth` の所見まで評価される。
「外から見えるポート」と「config を読んで初めて分かること」を
分けるための仕組み。

| service | 確かめ方 |
|---|---|
| `ssh` | 本物のSSH認証（paramiko クライアント → 22 のCLIサーバ / 830 のNETCONF） |
| `telnet` | 本物のTelnetログイン（23 のCLIサーバ）→ [`telnet-cli-server.md`](./telnet-cli-server.md) |
| `snmp` | 本物のSNMP v2c GET を、そのコミュニティで投げる |
| `https` | **試しようがないので未検証**（`verified: false`）。RESTCONF は装置ごとの `:443` ではなく、アプリ自身のポートで `/restconf/{device_id}/` として提供しており、認証もアプリ全体のユーザだから |

**22（SSH）や 23（Telnet）でログインできた場合は、続けて
`show running-config` を実際に実行して設定を持ち帰る**
（`engine/ssh_cli_agent.py` / `engine/telnet_cli_agent.py` の実CLIサーバ）。
`requires_auth` の所見は、その**取得した本文**を解析して判定する。
`DeviceState` を直接覗くのは、config を取れなかったときの代替経路。

| 所見 | config 上の判定 |
|---|---|
| `netlab-no-aaa-authentication` | `aaa new-model` の行が無い |
| `netlab-weak-local-password` | `username X privilege 15 secret Y` の Y が8文字未満 |
| `netlab-snmp-rw-community` | `snmp-server community X RW` の行がある |

830 は netconf サブシステムしか受け付けないので、そちらでログインした
場合は**認証の可否しか分からない**（`note` に取得有無が出る）。

### 権限昇格（`permissionElevation`）— privilege 1 でも config を読む

privilege 1 のアカウントでは `show running-config` が user EXEC で
拒否される（[`ssh-cli-server.md`](./ssh-cli-server.md) の enable
昇格）。実機の InsightVM はこれに対応するため、SSH の site
credential に `permissionElevation` を持たせられる。これは
**Rapid7公式Swaggerに実在するフィールド**（`SharedCredentialAccount`）で、
値は `none` / `sudo` / `sudosu` / `su` / `pbrun` / `privileged-exec`
の6種類。`privileged-exec` が Cisco の `enable` に相当する。

```json
{
  "name": "elevated",
  "account": {
    "service": "ssh",
    "username": "lowpriv", "password": "LowP@ss",
    "permissionElevation": "privileged-exec",
    "permissionElevationUsername": "enable",
    "permissionElevationPassword": "En@bleSecret1"
  }
}
```

`permissionElevation` が `none`/`pbrun` 以外なら、実機の規則どおり
`permissionElevationUsername`/`permissionElevationPassword` の両方を
必須にしている（`privileged-exec` に実際のユーザ名は要らないが、
このフィールド自体は汎用モデルの一部で、値は使わず必須チェックだけ
実機に合わせている）。

**このエミュレータで実際に昇格を試みるのは `privileged-exec` だけ**。
`sudo`/`sudosu`/`su`/`pbrun` はUNIX系装置向けで、このエミュレータに
その実体が無いため、受理はするが何もしない（素通し）。

`privileged-exec` を指定すると、SSHログイン後に対話シェルで
`enable` → パスワードを実際に送り、昇格できたかどうかで
`show running-config` を読むかどうかが変わる。

```
=== 昇格無し（比較用）===
   plain  ssh  verified=True  authenticated on tcp/22

=== permissionElevation: privileged-exec + 正しいパスワード ===
   elevated  ssh  verified=True
             authenticated on tcp/22 (elevated via enable), read running-config (...)
   findings に netlab-no-aaa-authentication が乗る（aaa new-model 未設定のため）

=== permissionElevation: privileged-exec + 間違ったパスワード ===
   bad-elev  ssh  verified=True   authenticated on tcp/22, enable failed
```

`verified` はSSHログイン自体が通ったかどうかで、昇格の成否とは
別。昇格に失敗しても資格情報自体は無効にならない（実機も同様、
権限不足で一部データが取れないだけで認証情報としては有効）。

`permissionElevationPassword` は他の秘密フィールドと同じく
GETしても返らない。`permissionElevation` と
`permissionElevationUsername` は秘密ではないので返る。

`credentialStatus` に結果が出る。Swagger仕様にこの列挙は無い
（レポート側のデータモデルにあり、API定義には現れない）ので、
実機の表記に寄せた独自の値。

| 値 | 意味 |
|---|---|
| `no-credentials-supplied` | 有効な資格情報が登録されていない |
| `credential-status-success` | 1つ以上が本当に通った |
| `credential-status-login-failed` | サービスはあったが、どれも通らなかった |
| `credential-status-service-not-found` | 試せるサービスが開いていなかった |

`asset['credentials']` に1件ずつの結果（`verified` と理由）が入る。

```
=== 資格情報なし ===
   services: [('tcp', 22), ('udp', 161)]
   findings: ['netlab-snmp-default-community']

=== 正しいSSH資格情報（22でログイン → show running-config 取得）===
   credentialStatus=credential-status-success
     ssh  ssh  verified=True
          authenticated on tcp/22, read running-config (3445 bytes)
   findings: ['netlab-snmp-default-community', 'netlab-no-aaa-authentication',
              'netlab-weak-local-password', 'netlab-snmp-rw-community']

=== 装置を是正（aaa new-model / rwコミュニティ削除 / 強いパスワード）===
   credentialStatus=credential-status-success
     ssh   ssh  verified=False login failed        ← パスワードを変えたので通らない
     ssh2  ssh  verified=True  authenticated on tcp/22, read running-config
   findings: ['netlab-snmp-default-community']
```

認証の成否だけを見る場合（830 しか開いていない装置など）:

```
=== 1. 資格情報なし ===
   credentialStatus=no-credentials-supplied  total=1
=== 2. 間違ったパスワードのSSH資格情報 ===
   credentialStatus=credential-status-login-failed  total=1
     bad-ssh      ssh    verified=False login failed
     authenticated-only findings: []
=== 3. 正しいSSH資格情報を追加 ===
   credentialStatus=credential-status-success  total=2
     bad-ssh      ssh    verified=False login failed
     good-ssh     ssh    verified=True  authenticated on tcp/830
     authenticated-only findings: ['netlab-no-aaa-authentication']
=== 4. 間違ったSNMPコミュニティだけ ===
   credentialStatus=credential-status-login-failed  total=1
     bad-snmp     snmp   verified=False community rejected
=== 5. 正しいSNMPコミュニティ ===
   credentialStatus=credential-status-success  total=2
     good-snmp    snmp   verified=True  community accepted
```

`probe: false` のときは実際に試せないので、以前どおり
「有効な資格情報があれば通ったものとして扱う」に戻る。

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

### 実ポートスキャンを入れて見つかった不具合（5件）

モデルを読むのをやめて本当にソケットを叩いた瞬間に出てきたもの。
**どれも、それまでのテストは全部通っていた。**

5. **NETCONF と gNMI が装置IPで待ち受けられていなかった** —
   SNMPエージェントだけが `ip addr add <ip>/32 dev lo` でIPを
   足しており、NETCONF/gNMI はやっていなかった。そのため
   `Cannot assign requested address` で毎回起動に失敗していたのに、
   スキャナが設定を読むだけだったので「開いている」と report していた。
   共通化して `engine/loopback_alias.py` に置いた。

6. **SNMPエージェントが管理IPの変更に追従しなかった** —
   装置作成時のIPに張り付いたままで、あとから `ip address` を
   振っても古いIPで待ち受け続けていた。関数のdocstringに
   「追従しない（今のところ十分）」と書いてあったが、十分ではなかった。

7. **`no netconf-yang` がポートを解放していなかった** —
   `stop()` が `close()` しかしておらず、`accept()` でブロックしている
   スレッドが起きないため fd が残り、TCP/830 が LISTEN のままだった。
   その状態で再度 `netconf-yang` を打つと
   `Address already in use` で起動に失敗する。
   `shutdown()` してから閉じるよう修正。

8. **装置を削除してもリスナーが止まらなかった** —
   `DELETE /api/device/{id}` は各エンジンの登録を掃除するだけで、
   NETCONF/gNMI/SNMP/OSPF の実リスナーを止めていなかった。
   消したはずの装置のポートが開いたまま残る。

9. **ポートスキャンのたびに paramiko がトレースバックを吐いていた** —
   スキャナはTCPを開けてすぐ閉じるので、毎回
   `Error reading SSH protocol banner` が ERROR で出る。
   RFC 4253 では双方が接続直後に識別文字列を送るので、
   何も送らずに切った相手は SSH ではない。accept 後に peek して
   EOF なら静かに落とすようにした。

5〜8 の回帰テストは `tests/test_nexpose_real_scan.py`。

### 資格情報を本当に試すようにして見つかった不具合（3件）

「登録されていれば認証成功」をやめて、本物のSSHログインと
SNMP GET で確かめるようにした瞬間に出てきたもの。
**3件ともSNMPのコミュニティ照合が壊れていた。**

10. **でたらめなコミュニティで `snmpwalk` するとMIBが丸ごと読めた** —
    `get()` はコミュニティを照合していたが、`getnext()` は
    **引数としてコミュニティを受け取ってすらいなかった**。
    GET だけ守って GETNEXT/GETBULK が素通り、という逆の状態。

    ```
    --- 誤ったコミュニティで WALK（修正前）---
    iso.3.6.1.2.1.1.1.0 = STRING: "Cisco IOS XE Software, ..."
    iso.3.6.1.2.1.1.5.0 = STRING: "SNMP-T"
    ```

11. **`_auth()` が設定に関わらず `public` を常に許していた** —
    `return community in ('public', cfg)`。
    `snmp-server community s3cret-only ro` と設定しても
    public で読めてしまう。これでは
    `netlab-snmp-default-community` の所見を直しても意味が無い。

12. **設定したコミュニティがエージェントに届いていなかった** —
    `snmp_agent.register()` は装置作成時にしか呼ばれず、
    `snmp-server community` を打っても既定の `public` のままだった。
    しかも1つしか保持できず、2つ目以降のコミュニティでは読めなかった。
    結果として**正しいコミュニティでは読めず、public では読める**という
    完全に逆の状態になっていた。

    ```
    --- 正しいコミュニティで GET（修正前）---
    iso.3.6.1.2.1.1.5.0 = No Such Object available on this agent at this OID
    ```

### 認証後の読み取りを本物のSSHにして分かったこと

### 認証スキャンを telnet まで広げて分かったこと（2件）

14. **`netlab-telnet-cleartext` は一度も成立しない死んだ判定だった** —
    検出条件が `state.telnet_enabled` を見ていたのに、
    **この属性を立てるコードがどこにも無かった**。
    さらに `transport input ssh telnet` は running-config に
    **ハードコード**されていてコマンド自体が未実装だったので、
    平文管理を有効にすることも止めることもできなかった。
    実Telnetサーバと `transport input` を実装した
    → [`telnet-cli-server.md`](./telnet-cli-server.md)

15. **`https` の候補サービス（装置の :443）は嘘だった** —
    `candidate_services` が `restconf_enabled` で 443 を挙げていたが、
    RESTCONF は装置ごとの :443 ではなく**アプリ自身のポート**で
    `/restconf/{device_id}/` として提供している。そのアドレスでは
    誰も待ち受けておらず、実プローブでは絶対に確認できない候補
    だったので外した。`https` 資格情報は未検証のままとし、
    その理由を note に出すようにした。

---

13. **そもそもCLIをSSHで叩く手段が無かった** —
    CLIは `/api/cli` からしか呼べず、NETCONFサーバ(830)は
    `netconf` サブシステムしか受け付けない。つまり
    「認証は本物、読み取りは `DeviceState` を直接覗く」から
    先へ進めなかった。実SSH CLIサーバ（TCP/22）を実装した
    → [`ssh-cli-server.md`](./ssh-cli-server.md)

### `enable` 権限昇格を入れて分かったこと

SSH/Telnet CLIサーバに user EXEC / privileged EXEC の区別
（[`ssh-cli-server.md`](./ssh-cli-server.md) の「enableによる権限昇格」）
を入れた結果、**privilege 15 未満のローカルユーザは
`show running-config` を実行できなくなった**。認証（ログイン）は
通るが、設定は読めない、という状態が実際に起こるようになった。

これに対して `probe_ssh_login` は、権限不足の応答
（`% Invalid input detected...`）をそのまま config として
扱わないようにしている。もしそのまま扱うと、`aaa new-model` や
`RW` を正規表現で探しても見つからず「読めているのに中身が
空振り」という壊れ方をする。config が読めない場合は、
これまで通り `DeviceState` を直接見る経路にフォールバックする。

```
資格情報: lowpriv (privilege 1, aaa new-model設定済み, snmp RWあり)
  verified=True note='authenticated on tcp/22'   ← config は読めていない
  findings: ['netlab-snmp-rw-community']          ← DeviceState直読みで検出
  （'netlab-no-aaa-authentication' は出ない。aaa new-modelは設定済みなので）
```

回帰テストは `tests/test_nexpose_real_scan.py` の
`test_config_fetch_denial_falls_back_to_reading_the_device_state`。

---

### 非同期スキャン（`async`）— pause/resume/stop を本当に効かせる

実機の `pause`/`resume`/`stop` は Swagger 上に定義があるが、
このエミュレータの `start_scan` は元々**同期I/O**で、
`POST .../scans` がその場でスキャンを完了させて返していた
（§5 に書いたとおり）。そのため `pause`/`resume`/`stop` の状態遷移
バリデーションは実装されていても、**呼び出す頃には対象のスキャンが
既に `finished` になっている**という到達不能なコードだった。

これを実機の非同期実行に寄せるのではなく、**エミュレータ独自の
オプトイン拡張**として `POST .../scans` のボディに `"async": true` を
渡したときだけ、バックグラウンドスレッドでスキャンを進行させ、
`pause`/`resume`/`stop` が装置1台ずつの評価の合間で本当に効くように
した。`async` を渡さない場合（デフォルト）は今まで通り、
返ってきた時点でスキャンは完了している。

```bash
curl -u admin:admin -X POST localhost:8000/api/3/sites/1/scans \
  -H 'Content-Type: application/json' -d '{"async": true}'
# → {"id": 7, ...}  この時点ではまだ running のことがある

curl -u admin:admin -X POST localhost:8000/api/3/scans/7/pause
curl -u admin:admin localhost:8000/api/3/scans/7   # status: paused, assets が途中で止まっている
curl -u admin:admin -X POST localhost:8000/api/3/scans/7/resume
curl -u admin:admin -X POST localhost:8000/api/3/scans/7/stop    # 途中終了。status: stopped
```

`"async": true` は Rapid7 の実 API には存在しないフィールドで、
このエミュレータでのみ意味を持つ。実機の非同期実行そのもの
（Scan Engine 経由のジョブキュー等）を再現したものではない。

回帰テストは `tests/test_nexpose_real_scan.py` の
`test_async_scan_starts_running_and_reaches_finished`、
`test_async_scan_can_really_be_paused_and_resumed`、
`test_async_scan_can_really_be_stopped`、
`test_pause_and_stop_are_rejected_once_a_scan_has_finished`、
`test_default_sync_scan_behaviour_is_unchanged`（`async` を渡さない
既定動作が変わっていないことの固定）。

---

修正後は実機同様、コミュニティが合わない要求は**黙って捨てる**
（応答を返すとコミュニティ名の総当たりに手掛かりを与えるため）。

```
--- 正しいコミュニティで GET ---   iso.3.6.1.2.1.1.5.0 = STRING: "SNMP-T2"
--- 正しいコミュニティで WALK ---  iso.3.6.1.2.1.1.5.0 = STRING: "SNMP-T2"
--- public で GET (設定していない) ---  Timeout: No Response
--- 誤ったコミュニティで WALK ---       Timeout: No Response
```

なお、コミュニティを1つも設定していない装置はこれまで通り
既定の `public` で読める（このエミュレータは装置作成時に
暗黙の public で登録しているため）。

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
- バナー取得・サービス識別。ポートの開閉は本物のソケットで確かめるが、
  製品名やバージョンは `DeviceState` から埋めているだけ
- ポートスイープ。叩くのは候補＋`WELL_KNOWN_PORTS` だけで、
  1-65535 の全走査はしない
- https の資格情報の照合（認証できる実体が無い）。
  ssh・telnet・snmp は本当に試す（22/23でログインできれば
  `show running-config` を実際に実行して読む。詳細は §3.5 の 13）
- Nexposeの site_credentials が公開鍵を扱えない（username/passwordのみ）。
  SSH CLIサーバ自体は `ip ssh pubkey-chain` の公開鍵認証に対応済み
  → [`ssh-cli-server.md`](./ssh-cli-server.md)
- `permissionElevation` は `privileged-exec`（Cisco enable）のみ実装。
  `sudo`/`sudosu`/`su`/`pbrun` は受理するが機能的には素通し
  （UNIX系向けでこのエミュレータに実体が無いため）
- Scan Engine / Engine Pool、スケジュールスキャン。
  `POST .../scans` は既定ではその場で完了して `finished` を返すが、
  `"async": true`（エミュレータ独自拡張）を渡すとバックグラウンドで
  進行し `pause`/`resume`/`stop` が本当に効く（詳細は §3.5 の後）
- Scan Template のチューニング（3種類の固定テンプレートのみ）
- Policy / SCAP・CIS ベンチマーク
- レポートの実ファイル出力（PDF/CSV）。`/content` はプレーンテキスト
- Tag、Asset Group、Solution のロールアップ
- 例外の期限切れ（`expires`）と、既存スキャン結果への遡及適用
  （承認は**次のスキャンから**効く）
- 永続化。プロセス再起動で Site もスキャン結果も消える

---

## 6. テスト

`tests/test_nexpose_api.py`（55 件、TestClient）、
`tests/test_nexpose_real_scan.py`（41 件、**本物の uvicorn サーバ**）、
`tests/test_ssh_cli_server.py`（10 件、実SSHクライアント →
[`ssh-cli-server.md`](./ssh-cli-server.md)）、
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

実プローブ側（`test_nexpose_real_scan.py`）が固定しているのは、
§3.5 の 5〜8 そのもの: 装置IPで本当に待ち受けていること、
リスナーを止めれば検出からも消えること、`no netconf-yang` が
ポートを解放すること、装置を消せばポートが閉じること、
そして**設定に無いポートでも開いていれば見つけること**。
加えて §3.5 の 10〜12: 間違ったパスワード／コミュニティでは
認証スキャンにならないこと、装置側のパスワードを変えたら同じ
資格情報が通らなくなること、`snmpwalk` がコミュニティ照合を
素通りしないこと、複数コミュニティのどれでも読めること。
さらに `permissionElevation`: `privileged-exec` で正しいパスワード
なら privilege 1 でも config が読めること、間違っていれば読めない
まま（DeviceState直読みへフォールバック）になること、
`sudo`/`su`/`pbrun` 等は受理だけして何もしないこと、
`permissionElevationPassword` がGETで返らないこと、
必須チェック（`none`/`pbrun` 以外はユーザ名・パスワード必須）が
実機の規則どおりに効くこと。

```bash
python3 -m pytest tests/test_nexpose_api.py tests/test_nexpose_real_scan.py \
                 tests/test_snmp_community_config.py -q
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
