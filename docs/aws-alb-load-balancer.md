# AWS ALB(Application Load Balancer): 実ヘルスチェック + 実L7トラフィック分散

## これは何か

「ここにモックロードバランサーって存在しますか？」(既存の`bigip`で
存在を確認)→「ちなみにalbを実装することもできますか？実働様で
おねがいします」への対応。

実機AWS ALBには他の装置のようなSSH/コンソールログインという概念が
無い(AWS CLI/API/Terraformで設定する管理対象オブジェクト)。ユーザーが
選んだ「aws-cli構文を部分対応」方式に従い、`aws elbv2 ...`構文を
`/api/cli`経由で受け付ける新しい`alb` device_typeを実装した
(`engine/rules.py` `_alb_process`)。

F5 BIG-IP実装(`docs/bigip-load-balancing.md`)と同じ「実ヘルスチェック
+実トラフィック分散」の考え方だが、ALBはL7(HTTPリクエストのpath/
Hostヘッダに基づくリスナールール評価)である分、一段実装が多い:

1. **実HTTPヘルスチェック**: health-check-protocolがHTTP/HTTPSなら、
   メンバーの実IP:portへ本物のHTTP GETを送り、2xx/3xxなら健全と
   みなす。HTTPを話さない相手(例えば生のTCPバナーを返すだけのサー
   ビス)には正しく不健全判定になる——フェイクの検知ではない。
2. **実L7トラフィック分散**: リスナーのdestination(ALBのVIP:port)に
   本物のTCPリスナーを立て、新規接続ごとに実際にHTTPリクエストライン
   +ヘッダーを読み取り、リスナールールのpath-pattern/host-header条件
   (優先度順)を評価してターゲットグループを選び、その時点で健全な
   メンバーへround-robinで実中継する。

## 新規に追加したもの

- **`engine/alb_lb.py`**: `member_http_healthy`/`member_tcp_reachable`/
  `target_is_healthy`(実ヘルスチェック)、`rule_matches`(path-pattern/
  host-headerの評価、fnmatchベース)、`AlbListenerProxy`(実TCP
  リスナー+HTTPリクエストパース+ルール評価+round-robin+双方向
  リレー)、`ensure_alb_listener_proxy`/`stop_alb_listener_proxy`。
- **`engine/mock_http_server.py`**: pc装置向けの`python3 -m
  http.server <port>`——このエミュレータの装置は元々実HTTPサーバを
  持っていなかった(SSH/SNMP/NETCONF/BGP等のみ)ため、ALB/BIG-IPの
  実ヘルスチェック・実L7ルーティングを本物のHTTP往復でデモできる
  ようにする追加。Python標準ライブラリの`http.server`をそのまま使い、
  装置のホスト名を返すだけの最小限のGETハンドラを持たせた(ロード
  バランサのデモで「どのバックエンドが応答したか」が一目でわかる
  ようにするため)。停止は`fuser -k <port>/tcp`(実機Linuxの実コマンド
  ——このエミュレータはCLIが1リクエスト=1コマンドの応答モデルのため
  実機のCtrl-Cそのものは再現できないが、同じ目的を果たす実コマンド
  として受け付ける)。
- **`engine/rules.py`**: `_alb_process`(aws-cli構文のパース・
  `create-load-balancer`/`create-target-group`/`register-targets`/
  `deregister-targets`/`create-listener`/`create-rule`/
  `describe-load-balancers`/`describe-target-groups`/
  `describe-target-health`/`describe-listeners`/`describe-rules`、
  いずれも実機aws-cliのJSON出力形状を再現)、`DeviceState`の`alb`分岐
  (`alb_load_balancers`/`alb_target_groups`/`alb_listeners`/
  `alb_rules`——設定値のみ、ランタイムの接続試行/リスナーソケットは
  `engine/alb_lb.py`が別途保持する、他のengineと同じパターン)。
- **`app.py`**: `create-listener`/`create-rule`/`register-targets`/
  `deregister-targets`/`create-target-group`実行後(他の実リスナー
  起動と同じ理由で`rule_engine.process()`実行後)に、設定済みの全
  リスナーへ`ensure_alb_listener_proxy()`を呼び直す。

## 実装中に見つけた/注意した点

- **ALBには`show running-config`という概念が無い**: `app.py`の
  `handle_protocol_show`/`handle_protocol_config`(二層ディスパッチの
  app.py側、RuleEngineより先に評価される)の汎用正規表現群に
  `device_type == 'alb'`が巻き込まれると、ASA/bas/fortigateで繰り返し
  踏んだのと同じ「Si-Rスタイルのダミー出力にフォールバックする」バグ
  になる。今回は実装時点で両関数の先頭に`if state.device_type ==
  'alb': return None`を追加して先回りで防いだ(同じ理由で
  `_expand_abbreviation`のCisco略称展開スキップリストにも`alb`を
  追加)。
- **全テスト装置がデフォルトで同じVIP(172.31.0.10)を共有する**:
  実機ALBはAWSが払い出すDNS名/IPを使うため、このエミュレータでも
  `bas`/`yamaha`同様固定の初期IPを持たせたが、複数のALBテスト装置が
  同じポート(80)でリスナーを作成すると実ソケットが衝突する——
  ASAの実SSHテストで踏んだのと同じ class の問題。テストは
  `stop_alb_listener_proxy()`で後片付けすることで回避している。

## ライブ検証(実行結果そのまま)

pc装置3台(`web1`=10.70.0.1、`web2`=10.70.0.2、`api1`=10.70.0.3)に
それぞれ`python3 -m http.server 9000`で実HTTPサーバを起動し、ALBで
2つのターゲットグループ(`web-tg`=web1+web2、`api-tg`=api1)+
path-patternルール(`/api/*` → api-tg、それ以外 → web-tg、既定action)
を設定:

```
aws elbv2 create-load-balancer --name demo --type application
aws elbv2 create-target-group --name web-tg --protocol HTTP --port 9000 --health-check-protocol HTTP --health-check-path /
aws elbv2 create-target-group --name api-tg --protocol HTTP --port 9000 --health-check-protocol HTTP --health-check-path /
aws elbv2 register-targets --target-group-arn web-tg --targets Id=10.70.0.1,Port=9000 Id=10.70.0.2,Port=9000
aws elbv2 register-targets --target-group-arn api-tg --targets Id=10.70.0.3,Port=9000
aws elbv2 create-listener --load-balancer-arn demo --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=web-tg
aws elbv2 create-rule --listener-arn <listener-arn> --priority 10 --conditions Field=path-pattern,Values=/api/* --actions Type=forward,TargetGroupArn=api-tg
```

本物のcurlでALBのVIP(`172.31.0.10:80`)へ`/index.html`を4回叩くと、
実際にweb-tg内でround-robinする(各レスポンスのボディはバックエンド
自身のホスト名):

```
$ curl http://172.31.0.10:80/index.html  (×4)
web1
web2
web1
web2
```

`/api/widgets`は実際にapi-tgのみへルーティングされる:

```
$ curl http://172.31.0.10:80/api/widgets  (×3)
api1
api1
api1
```

`web1`の実http.serverを`fuser -k 9000/tcp`で止めると、
`describe-target-health`が実際にunhealthyへ変わり:

```
"Target": {"Id": "10.70.0.1", "Port": 9000},
"TargetHealth": {"State": "unhealthy", "Reason": "Target.FailedHealthChecks", ...}
```

以降の`/index.html`へのアクセスは実際にすべて`web2`のみへ
フェイルオーバーする:

```
$ curl http://172.31.0.10:80/index.html  (×4、web1停止後)
web2
web2
web2
web2
```

## スティッキーセッション(duration-based cookie stickiness)

ユーザー依頼「疑似クライアントから、パーシステンスの確認とかも
お願いできますか？」への対応。実機ALBのduration-based cookie
stickiness(lb_cookie、既定モード)を実装した:

```
aws elbv2 modify-target-group-attributes --target-group-arn web-tg \
  --attributes Key=stickiness.enabled,Value=true \
               Key=stickiness.lb_cookie.duration_seconds,Value=3600
```

有効化すると、`AlbListenerProxy`が実際にレスポンスへ`Set-Cookie:
AWSALB=<member>; Max-Age=<secs>; Path=/`を注入し(本物のHTTPヘッダ
操作)、同じCookieを持つ次回以降のリクエストは同じメンバーへ固定
される。

ライブ検証: `curl -c/-b`でCookieを維持する疑似クライアントから5回
アクセスすると(stickiness無しならround-robinで交互になるはずが)
実際に全て同じバックエンド(`web-svr-1`)に固定され、保存された
Cookieファイルにも実際の`AWSALB=10.80.0.1:9100`が記録されている
ことを確認:

```
$ curl -c cookies.txt -b cookies.txt http://172.31.0.10:80/  (×5)
web-svr-1
web-svr-1
web-svr-1
web-svr-1
web-svr-1

$ cat cookies.txt
172.31.0.10  FALSE  /  FALSE  <expiry>  AWSALB  10.80.0.1:9100
```

Cookieを持たない別クライアントは、既存のround-robinのまま独立して
振り分けられることも確認(`web-svr-2`→`web-svr-1`→`web-svr-2`)。

## 仮想クライアントツールでのALBデモ(before/after stickiness)

ユーザー依頼「仮想クライアントからサーバにアクセスするソフトで、ALB
経由で動作を見たら面白そうやって欲しい」への対応。`tools/
virtual_client.py`(`docs/load-balancer-testing-quickstart.md`参照)を
使い、同じ4台の仮想クライアントに対してstickiness無効/有効の2回
アクセスさせ、挙動が切り替わる様子をそのまま記録した。

Web用target-group(`web-tg`、2バックエンド)+path-patternルール
(`/api/*` → `api-tg`、1バックエンド)を構成した上で、まずstickiness
無効のまま4台の仮想クライアントから`/index.html`へ各3回アクセス:

```
$ python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \
    --clients 4 --requests 3 --path /index.html

クライアントごとの振り分け:
  client-0: {'vcdemo-web1': 2, 'vcdemo-web2': 1}  [DISTRIBUTED]
  client-1: {'vcdemo-web2': 2, 'vcdemo-web1': 1}  [DISTRIBUTED]
  client-2: {'vcdemo-web1': 2, 'vcdemo-web2': 1}  [DISTRIBUTED]
  client-3: {'vcdemo-web2': 2, 'vcdemo-web1': 1}  [DISTRIBUTED]

全体の振り分け:
  vcdemo-web1: 6
  vcdemo-web2: 6
```

同じ4台のパスを`/api/widgets`に変えると、path-patternルールにより
全員が`api-tg`の唯一のメンバーに固定される(`STICKY`扱いだが、これは
persistenceではなくルーティングの結果であることに注意):

```
$ python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \
    --clients 3 --requests 2 --path /api/widgets
  client-0: {'vcdemo-api1': 2}  [STICKY]
  client-1: {'vcdemo-api1': 2}  [STICKY]
  client-2: {'vcdemo-api1': 2}  [STICKY]
```

次に`web-tg`へ`modify-target-group-attributes`でstickinessを有効化し、
**同じ4台の仮想クライアント**(送信元IP・Cookie保持は仮想クライアント
ツール側が個別に維持)で全く同じコマンドを再実行すると、今度は各
クライアントが実際に1種類のバックエンドへ固定される
(`Set-Cookie: AWSALB=<member>`が実際に注入・引き継がれていることを
`--path`出力のcookie欄で確認):

```
$ aws elbv2 modify-target-group-attributes --target-group-arn web-tg \
    --attributes Key=stickiness.enabled,Value=true \
                 Key=stickiness.lb_cookie.duration_seconds,Value=3600

$ python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \
    --clients 4 --requests 3 --path /index.html

=== client-0 (src=127.0.10.1) ===
  request 0: backend='vcdemo-web1'  cookie=AWSALB=10.92.0.1:9300
  request 1: backend='vcdemo-web1'  cookie=AWSALB=10.92.0.1:9300
  request 2: backend='vcdemo-web1'  cookie=AWSALB=10.92.0.1:9300
=== client-1 (src=127.0.10.2) ===
  request 0: backend='vcdemo-web2'  cookie=AWSALB=10.92.0.2:9300
  ...

クライアントごとの振り分け:
  client-0: {'vcdemo-web1': 3}  [STICKY]
  client-1: {'vcdemo-web2': 3}  [STICKY]
  client-2: {'vcdemo-web1': 3}  [STICKY]
  client-3: {'vcdemo-web2': 3}  [STICKY]

全体の振り分け:
  vcdemo-web1: 6
  vcdemo-web2: 6
```

stickiness有効/無効のON/OFFで同じツール・同じクライアント集合の結果が
実際に切り替わり、かつ全体では依然として2バックエンドへ均等に分散
されている(1クライアントに固定されているだけで、全体としての
ロードバランシングは壊れていない)ことが一目で確認できる——「仮想
クライアントからALB経由で動作を見る」という依頼の狙い通りのデモに
なった。

## BIG-IPとの比較

同じ`tools/virtual_client.py`は`tcp`モードでBIG-IP(L4 VIP)にも使える
(`docs/bigip-load-balancing.md`「仮想クライアントツールでの負荷分散/
パーシステンス確認」参照)。ALBはHTTPモード+Cookie、BIG-IPはTCP
モード+送信元IPという、それぞれの実機が実際に使うパーシステンスの
方式の違いがそのままツールの引数の違い(`http`か`tcp`か)に対応して
いる。

## スコープ外(実装していない)

- HTTPSのTLS終端(証明書/TLSプロファイルを持たないため、HTTPS指定でも
  平文TCPとして扱う)
- header-based/query-string-based条件、重み付けターゲットグループ、
  リダイレクト/固定レスポンスアクション(forwardアクションのみ)
- least_outstanding_requestsアルゴリズム(round robinのみ)
- app_cookie(lb_cookieのみ実装)

## テスト

`tests/test_alb_load_balancer.py`(25 tests)がCLI設定(create-*/
register-targets/deregister-targets/describe-*/modify-target-group-
attributes、ARN解決、エラーケース)、名前付きオブジェクトの一意性
(target-groupの再登録で重複しないこと、複数target-group/listenerが
独立して管理されること)、実HTTPヘルスチェック(健全/不健全/非HTTP
サービスへの誤検知防止)、`AlbListenerProxy`単体の振り分けロジック
(path-patternルーティング、ターゲットグループ内round-robin、ルール
不一致時の503、Cookieベースstickinessの付与/固定/フォールバック)、
および実http.serverを使ったエンドツーエンド
(本物のuvicornサーバ+本物のurllibクライアント、round-robin+
path-basedルーティング+`fuser -k`による実フェイルオーバー)を固定する。
