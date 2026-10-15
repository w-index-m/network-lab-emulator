# ロードバランサ(ALB/BIG-IP)試験クイックスタート

ユーザー依頼「あと動作については、マークダウンにメモするとかして
次回の起動は早くなるように改善してほしい」への対応。ALB/BIG-IPの
ロードバランシング・パーシステンス・SNATを試験する際に、毎回ゼロから
考え直さなくて済むよう、実際に動作確認済みのコマンド列をそのまま
ここに貼っておく。

## 前提

- 実装の詳細・設計意図は`docs/aws-alb-load-balancer.md`(ALB)/
  `docs/bigip-load-balancing.md`(BIG-IP)を参照。このファイルは
  「とにかく早く再現して試す」ためのコマンド集。
- 本番相当の確認は`NETLAB_AUTH_DISABLE=1 python -m uvicorn app:app
  --host 127.0.0.1 --port <空いてるポート>`で起動し、**全体回帰
  テスト(`pytest tests/`)と同時に実行しない**(実ソケットのポート
  衝突で両方とも失敗する、CLAUDE.md既出の注意点)。

## ALB: 2台のWebバックエンド + round-robin + stickiness

```bash
B=http://127.0.0.1:<port>
cli() { curl -s -X POST $B/api/cli -d "{\"device_id\":\"$1\",\"command\":$2}" -H 'Content-Type: application/json'; }
mkdev() { curl -s -X POST $B/api/device -d "{\"id\":\"$1\",\"type\":\"$2\",\"hostname\":\"$1\"}" -H 'Content-Type: application/json' >/dev/null; }

# バックエンド2台(pcに実http.server)
mkdev web-a pc; mkdev web-b pc
cli web-a '"ip addr add 10.90.0.1/24 dev eth0"'
cli web-b '"ip addr add 10.90.0.2/24 dev eth0"'
cli web-a '"python3 -m http.server 9200"'
cli web-b '"python3 -m http.server 9200"'

# ALB本体
mkdev demo-alb alb
cli demo-alb '"aws elbv2 create-load-balancer --name demo --type application"'
cli demo-alb '"aws elbv2 create-target-group --name tg --protocol HTTP --port 9200 --health-check-protocol HTTP --health-check-path /"'
cli demo-alb '"aws elbv2 register-targets --target-group-arn tg --targets Id=10.90.0.1,Port=9200 Id=10.90.0.2,Port=9200"'
cli demo-alb '"aws elbv2 create-listener --load-balancer-arn demo --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=tg"'

# stickiness有効化(任意)
cli demo-alb '"aws elbv2 modify-target-group-attributes --target-group-arn tg --attributes Key=stickiness.enabled,Value=true Key=stickiness.lb_cookie.duration_seconds,Value=3600"'
```

ALBのVIPは常に`172.31.0.10`(`state.interfaces['eth0']['ip']`の既定値、
AWSデフォルトVPC CIDRにちなむ)。`curl http://172.31.0.10:80/`で疎通
確認できる。

## BIG-IP: 2台のSSHバックエンド + round-robin + persistence + SNAT

```bash
mkdev ssh-a catalyst; mkdev ssh-b cisco
for d in ssh-a ssh-b; do
  cli $d '"configure terminal"'
  cli $d '"crypto key generate rsa modulus 2048"'
  cli $d '"end"'
done

mkdev demo-bigip bigip
cli demo-bigip '"create ltm pool pool1 { members add { 192.168.10.1:22 203.0.113.2:22 } monitor tcp load-balancing-mode round-robin }"'

# SNAT(automap または snatpool)
cli demo-bigip '"create ltm snatpool snat1 { members add { 10.95.0.1 10.95.0.2 } }"'

# persistence(source-addr)
cli demo-bigip '"create ltm persistence source-addr persist1 { timeout 120 }"'

# 仮想サーバにまとめて適用
cli demo-bigip '"create ltm virtual vs1 { destination 10.96.0.1:2225 pool pool1 persist replace-all-with { persist1 } source-address-translation { type snat pool snat1 } }"'
```

BIG-IPのバックエンドIPは`catalyst`=`192.168.10.1`(Vlan10 SVI)、
`cisco`=`203.0.113.2`(GigabitEthernet0/0/0、`show ip interface
brief`で確認)が既定値。

BIG-IP自身のデフォルトSelf IP/インタフェースは`mgmt`=`192.168.245.1`、
`1.1`=`203.0.113.245`、`1.2`=`10.1.20.245`(いずれもcisco/arista/
fortigate/asa/pc等のデフォルトとは衝突しない専用のIP)。実機同様
VLANへSelf IPを割り当てる場合は:

```bash
cli demo-bigip '"create net vlan internal { interfaces add { 1.2 } }"'
cli demo-bigip '"create net self internal_self address 10.1.20.246/24 vlan internal"'
cli demo-bigip '"show net self"'
```

## 仮想クライアントツール(`tools/virtual_client.py`)

複数の仮想クライアント(別々の実送信元IP)からVIPへ接続し、どの
バックエンドに振り分けられたかを一括で集計するツール。「仮想
クライアントを作るソフトなかったっけか？」という質問への対応として
このセッションで新設した(それ以前は存在しなかった)。

```bash
# ALB(HTTP)に4台の仮想クライアントからそれぞれ3回アクセス
python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \
    --clients 4 --requests 3

# path-basedルールの確認
python3 tools/virtual_client.py http --vip 172.31.0.10 --port 80 \
    --clients 3 --requests 2 --path /api/widgets

# BIG-IP(SSH/TCP)に4台の仮想クライアントから、実際にログインして
# プロンプトのホスト名まで確認する(--ssh-loginにはparamikoが必要。
# 無ければ生のTCPバナーのみ見る軽量モードにフォールバック)
python3 tools/virtual_client.py tcp --vip 10.96.0.1 --port 2225 \
    --clients 4 --requests 3 --ssh-login
```

出力は「クライアントごとの振り分け(STICKY= persistence/stickiness
有効 / DISTRIBUTED=無効)」と「全体の振り分け(台数間の負荷分散の
均等さ)」の2段サマリー。stickinessやpersistenceを有効/無効化した
前後でこのコマンドを再実行するだけで、挙動の変化を一発で確認できる。

## 既知の落とし穴(このセッションで実際に踏んだもの)

1. **BIG-IPの`show running-config`は、`asa`/`bas`/`fortigate`/`alb`と
   同じ「二層ディスパッチでapp.py側の汎用ハンドラが先に勝つ」バグを
   一度踏んでいる**(`app.py`の`_build_running_config`に`bigip`の
   明示的委譲が無かった)。新しいdevice_typeを追加・拡張する際は
   必ず`_build_running_config`・`handle_protocol_show`・
   `handle_protocol_config`の3箇所を確認すること。
2. **127.0.0.0/8の全アドレスはデフォルトでループバックにルーティング
   される**ため、`engine.loopback_alias.ensure_loopback_alias()`で
   明示的にエイリアスする必要が無い(127.0.0.1だけでなく127.x.x.x
   すべて)——`tools/virtual_client.py`の仮想クライアント送信元IP
   (`127.0.10.N`)で確認。また`ip addr add`の「既に割り当て済み」
   エラー文言はiproute2のバージョンによって`File exists`以外に
   `Address already assigned`になることもある(両方とも成功扱いに
   修正済み)。
3. **全テスト装置が同じデフォルトVIP/ポートを共有する**(ALBは常に
   `172.31.0.10`、BIG-IPのバックエンドは`catalyst`=`192.168.10.1`/
   `cisco`=`203.0.113.2`固定)ため、同じプロセス内で複数のテスト/
   ライブ検証を続けて行う場合は、前のデバイスの実リスナー
   (`stop_alb_listener_proxy`/`stop_virtual_server_proxy`)や
   実SSHリスナー(`crypto key zeroize rsa`)を明示的に後片付けしないと
   ポート衝突する。
