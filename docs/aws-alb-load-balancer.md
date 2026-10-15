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

## スコープ外(実装していない)

- HTTPSのTLS終端(証明書/TLSプロファイルを持たないため、HTTPS指定でも
  平文TCPとして扱う)
- header-based/query-string-based条件、重み付けターゲットグループ、
  リダイレクト/固定レスポンスアクション(forwardアクションのみ)
- least_outstanding_requestsアルゴリズム(round robinのみ)
- スティッキーセッション(target group stickiness)

## テスト

`tests/test_alb_load_balancer.py`(17 tests)がCLI設定(create-*/
register-targets/deregister-targets/describe-*、ARN解決、エラー
ケース)、実HTTPヘルスチェック(健全/不健全/非HTTPサービスへの誤検知
防止)、`AlbListenerProxy`単体の振り分けロジック(path-pattern
ルーティング、ターゲットグループ内round-robin、ルール不一致時の
503)、および実http.serverを使ったエンドツーエンド(本物のuvicorn
サーバ+本物のurllibクライアント、round-robin+path-basedルーティング+
`fuser -k`による実フェイルオーバー)を固定する。
