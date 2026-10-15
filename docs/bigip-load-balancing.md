# F5 BIG-IP: 実ヘルスチェック + 実トラフィック分散

## これは何か

「ここにモックロードバランサーって存在しますか？」(既存の`bigip`
device_type/`_bigip_process`で存在を確認)→「実際のヘルスチェック/
振り分け動作を追加」への対応。

これまでの`_bigip_health_check`(app.py)は「メンバーのIPを持つ装置の
インタフェースがshutdownされていないか」というトポロジーベースの
疎通判定だけで、プールの`load-balancing-mode`(round-robin/
least-connections)は設定として保持されるだけで実際に使われていな
かった(振り分け先を選ぶ本体が存在しなかった)。

今回追加した2つ:

1. **実TCPヘルスチェック**: monitorがtcp/http/https系なら、メンバーの
   実IP:portへ本物の`socket.create_connection`を試みる。実SSH
   リスナー等が有効化されていないバックエンドは、インタフェースが
   upでもdown判定になる(gateway_icmpは従来通りトポロジーベース)。
2. **実トラフィック分散**: 仮想サーバ(`create ltm virtual ... pool
   <name>`)のdestinationに本物のTCPリスナーを立て、設定された
   load-balancing-modeで実際にヘルシーなプールメンバーへTCP接続を
   選択・中継する(バイト列をそのまま双方向リレーする本物のTCP
   プロキシ)。

## 実装

- `engine/bigip_lb.py`(新規): `member_tcp_reachable()`(実TCP connect)、
  `member_reachable_topology()`(gateway_icmp相当、icmp_engineの既存
  L3到達性判定を再利用)、`member_is_up()`(両者を統合する単一の判定
  ロジック)、`VirtualServerProxy`(実TCPリスナー+round-robin/
  least-connectionsによる振り分け+双方向リレー)、
  `ensure_virtual_server_proxy()`/`stop_virtual_server_proxy()`。
- `app.py`の`_bigip_health_check`を`member_is_up()`経由に統一(表示用の
  `show ltm pool`/`show running-config`と、実トラフィック分散の両方が
  同じ判定ロジックを共有)。
- `create`/`modify ltm virtual`・`create`/`modify ltm pool`コマンドの
  実行後(他の実リスナー起動と同じ理由で`rule_engine.process()`実行後)
  に、設定済みの全仮想サーバに対して`ensure_virtual_server_proxy()`を
  呼び直す。`delete ltm virtual`でリスナーを停止する。

## パーシステンス(Source Address Affinity)

ユーザー依頼「疑似クライアントから、パーシステンスの確認とかも
お願いできますか？」への対応。実機の`source_addr`パーシステンス
プロファイルを実装し、同じ送信元IPからの接続は(timeout以内・選ばれた
メンバーがまだ健全なら)実際に同じプールメンバーへ固定して中継する。

```
create ltm persistence source-addr sp_demo { timeout 60 }
create ltm virtual web_vs { destination 10.250.5.1:2224 pool web_pool persist replace-all-with { sp_demo } }
```

ライブ検証: 本物のparamikoクライアントで同じ送信元から4回SSH接続すると、
(persistence無しならround-robinで交互になるはずが)実際に全て同じ
バックエンド(`bigip-be1`)に固定されることを確認:

```
--- conn 0 --- bigip-be1#
--- conn 1 --- bigip-be1#
--- conn 2 --- bigip-be1#
--- conn 3 --- bigip-be1#
```

## SNAT(automap / snatpool)

ユーザー依頼「ナット...を実装して...一意性を保って、負荷分散して
るか試験もお願いします」「ちなみにSナットの試験も含めて欲しい」への
対応。プールメンバーへの実アウトバウンド接続(`socket.create_
connection`)の送信元IPを、本物のソケットバインド(`source_address`)
で実際に変える——バックエンド側から見ても本当に別IPとして見える、
シミュレーションではない。

```
create ltm snatpool my_snat { members add { 10.95.0.1 10.95.0.2 } }
create ltm virtual web_vs { destination 10.96.0.1:2225 pool web_pool source-address-translation { type snat pool my_snat } }
# または、自分自身(VIP)のIPをそのまま名乗るautomap:
create ltm virtual web_vs { destination 10.96.0.1:2225 pool web_pool source-address-translation { type automap } }
```

ライブ検証(`tests/test_bigip_lb_healthcheck.py`の
`TestVirtualServerProxySnatDirect`で実際にソケットレベルで確認):
接続元IPをそのまま返すだけの簡易バックエンドを使い、automapなら
バックエンドが実際にVIP自身のIPを送信元として見ること、snatpool
なら指定したプールメンバーのIPを見ること、SNAT未設定ならプロキシ
自身のデフォルトの送信元(通常127.0.0.1)のままであることを確認。

## 仮想クライアントツールでの負荷分散/パーシステンス確認

`tools/virtual_client.py`(新設、`docs/load-balancer-testing-
quickstart.md`参照)で複数の仮想クライアントから接続し、「クライアント
ごとに1種類のバックエンドに固定されるか(persistence)」「全体では
均等に分散されるか(load balancing)」を一括で確認できる。

```bash
python3 tools/virtual_client.py tcp --vip 10.96.0.1 --port 2225 \
    --clients 4 --requests 3 --ssh-login
```

ライブ検証: persistence無効時は4台の仮想クライアントそれぞれが
`{ssh-a: 1, ssh-b: 1}`のように2つのバックエンドへ均等に分散され
(DISTRIBUTED)、persistence(`persist replace-all-with`)を設定すると
各クライアントが`{ssh-a: 3}`のように1種類だけに固定され(STICKY)、
それでも4台全体では`ssh-a: 6 / ssh-b: 6`と均等に分散されることを
確認した。

### 5台の仮想クライアント→BIG-IP→実Webサーバ2台

ユーザー依頼「仮想クライアントを5台にしてモックBIGIPを経由して2台の
Webサーバに負荷をかける試験行けそうですか？」への対応。
`VirtualServerProxy`はHTTPを解釈しない純粋なバイトリレー(TCPプロキシ)
なので、`tools/virtual_client.py`の`http`モード(本来はALB向けに作った
もの)がそのままBIG-IPにも通ることを確認した——ALB用に作ったツールが
コード変更なしでL4ロードバランサにも使える、という横展開。

```bash
mkdev vc5-web1 pc; mkdev vc5-web2 pc
cli vc5-web1 '"ip addr add 10.93.0.1/24 dev eth0"'
cli vc5-web2 '"ip addr add 10.93.0.2/24 dev eth0"'
cli vc5-web1 '"python3 -m http.server 9500"'
cli vc5-web2 '"python3 -m http.server 9500"'

mkdev vc5-bigip bigip
cli vc5-bigip '"create ltm pool vc5pool { members add { 10.93.0.1:9500 10.93.0.2:9500 } monitor tcp load-balancing-mode round-robin }"'
cli vc5-bigip '"create ltm virtual vc5vs { destination 10.93.0.100:9500 pool vc5pool }"'

python3 tools/virtual_client.py http --vip 10.93.0.100 --port 9500 \
    --clients 5 --requests 4 --path /index.html
```

persistence無効(5台×4リクエスト=20リクエスト)では各クライアント内も
round-robin(`DISTRIBUTED`)、全体では`vc5-web1: 10 / vc5-web2: 10`と
完全均等:

```
client-0: {'vc5-web2': 2, 'vc5-web1': 2}  [DISTRIBUTED]
client-1: {'vc5-web2': 2, 'vc5-web1': 2}  [DISTRIBUTED]
client-2: {'vc5-web2': 2, 'vc5-web1': 2}  [DISTRIBUTED]
client-3: {'vc5-web2': 2, 'vc5-web1': 2}  [DISTRIBUTED]
client-4: {'vc5-web2': 2, 'vc5-web1': 2}  [DISTRIBUTED]
vc5-web2: 10
vc5-web1: 10
```

`create ltm persistence source-addr vc5persist { timeout 120 }` +
`modify ltm virtual vc5vs { persist replace-all-with { vc5persist } }`を
投入した後に同じコマンドを再実行すると、今度は5台それぞれが1種類に
固定される(`STICKY`)。クライアント台数が奇数(5台)なので、2バックエンド
への割り振り自体は`12:8`と偏るが、これは「どのクライアントも1台に
固定される」という正しい挙動の結果であり、ロードバランシングが壊れた
わけではない:

```
client-0: {'vc5-web2': 4}  [STICKY]
client-1: {'vc5-web1': 4}  [STICKY]
client-2: {'vc5-web2': 4}  [STICKY]
client-3: {'vc5-web1': 4}  [STICKY]
client-4: {'vc5-web2': 4}  [STICKY]
vc5-web2: 12
vc5-web1: 8
```

## Self IP / VLAN(「モックIPくらいのモック頼みます」)

ユーザー指摘「モックのBIGIPなのでモックIPくらいのモック頼みます\n
モックIPいけたのかな?」への対応。確認したところ、`bigip`は
interfaces定義を専用に持たず、他のCisco系device_typeの既定
(`GigabitEthernet0/0/0` @ `203.0.113.2`、`10.0.0.1`)をそのまま継承
していた——cisco/arista/fortigateと同じIPを名乗ってしまっており、
F5実機の見た目にもなっていなかった。

実機のF5 BIG-IPはインタフェースを`1.1`/`1.2`/`2.1`のような
"slot.port"形式で命名し、管理用は専用の`MGMT`インタフェースを持つ。
Self IPもインタフェースへ直接ではなく**VLANへ**割り当てる
(`tmsh create net self <name> address <ip>/<mask> vlan <vlan_name>`、
事前に`tmsh create net vlan <name> interfaces add { 1.1 }`でVLANへ
インタフェースを割り当てておく必要がある——`clouddocs.f5.com`の
tmsh referenceで構文を確認)。

これに合わせて:

- デフォルトインタフェースを`mgmt`(192.168.245.1/24)・`1.1`
  (203.0.113.245/24)・`1.2`(10.1.20.245/24 — F5公式トレーニングラボ
  `clouddocs.f5.com/training/community/f5cert/html/class2/modules/
  module10.html`の実例値に合わせた)に変更し、cisco/arista/
  fortigate/asa/pc等が共有するデフォルト(203.0.113.2/10.0.0.1/
  192.168.1.10/192.168.10.1等)と衝突しないようにした。
- `_bigip_process`(`engine/rules.py`)に`create/modify net vlan`・
  `create/modify net self`・`show/list net self`・`show/list net
  vlan`・`show net interface`を追加。`net self`でSelf IPを設定すると、
  そのVLANが指すインタフェース(`state.interfaces`)のIPが実際に
  更新される——icmp_engine/LLDP等、他デバイスと共通の
  `state.interfaces`経由の到達性判定にそのまま乗る(新しい判定
  ロジックは追加していない)。`show running-config`にも
  `net vlan`/`net self`ブロックを追加。

```
create net vlan internal { interfaces add { 1.2 } }
create net self internal_self address 10.1.20.246/24 vlan internal
show net self
```

ライブ検証: 上記を投入すると`state.interfaces['1.2']['ip']`が実際に
`10.1.20.246`へ更新されること、`show net self`/`list net self`/
`show running-config`すべてに反映されることを確認した
(`python3 -c`での直接呼び出しで確認、トランスクリプトは下記)。

```
>>> r.process('create net vlan internal { interfaces add { 1.2 } }', s)
''
>>> r.process('create net self internal_self address 10.1.20.246/24 vlan internal', s)
''
>>> r.process('show net self', s)
Net::Self IP: internal_self
  Address: 10.1.20.246/24   Vlan: internal
>>> s.interfaces['1.2']
{'ip': '10.1.20.246', 'prefix': 24, 'status': 'up', 'speed': '1000', 'duplex': 'full'}
```

`tests/test_bigip_lb_healthcheck.py`の`TestBigipMockSelfIp`(5 tests、
計33 tests)が、デフォルトSelf IPが他機種と衝突しないこと・
`net vlan`/`net self`投入でVLANが指すインタフェースのIPが実際に
更新されること・`show`/`list net self`の表示・`show running-config`
への反映・未作成のVLANを指すSelf IPは既存インタフェースに影響しない
こと、を固定する。

## スコープ外(実装していない)

- httpモニターの実際のHTTPリクエスト送信/レスポンス検証(この
  エミュレータの装置は実HTTPサーバを持たないため、tcp相当の接続性
  チェックに留める)
- SSL/TLSプロファイル、iRules
- source-addr以外のパーシステンスタイプ(cookie/hash等)
- UDPベースのロードバランシング(TCPパススルーのみ)

## ライブ検証(実行結果そのまま)

Catalyst(`lb-be1`, 管理IP `192.168.10.1`)とCisco(`lb-be2`, `203.0.113.2`)
の2台に実SSHを有効化し(`crypto key generate rsa modulus 2048`)、
BIG-IPにプール+仮想サーバを設定:

```
create ltm pool web_pool { members add { 192.168.10.1:22 203.0.113.2:22 } monitor tcp load-balancing-mode round-robin }
create ltm virtual web_vs { destination 10.250.0.1:2222 pool web_pool }
```

`show ltm pool web_pool`が実際にTCP connectで両メンバーupと判定:

```
Ltm::Pool: web_pool
------------------------------------------------------------
  Status
    Availability : available (green)
    State        : enabled
    Reason       : The pool is available
    Load Balancing Mode : round-robin
    Monitor      : tcp
    Members      : 2 (up: 2, down: 0)

  Member: 192.168.10.1:22   available (green)
  Member: 203.0.113.2:22   available (green)
```

本物のparamiko SSHクライアントでVIP(`10.250.0.1:2222`)へ4回接続し、
実際にround-robinでバックエンドが交互に選ばれることを確認
(各接続で返ってきたプロンプトのホスト名そのまま):

```
--- conn 0 ---
lb-be1#
--- conn 1 ---
lb-be2#
--- conn 2 ---
lb-be1#
--- conn 3 ---
lb-be2#
```

`lb-be1`の実SSHを無効化(`crypto key zeroize rsa`)すると、
`show ltm pool web_pool`が実際にdown判定に変わり:

```
Members      : 2 (up: 1, down: 1)
  Member: 192.168.10.1:22   offline (red)
  Member: 203.0.113.2:22   available (green)
```

以降の接続はすべて`lb-be2`のみに実際にフェイルオーバーする:

```
--- conn 0 ---
lb-be2#
--- conn 1 ---
lb-be2#
--- conn 2 ---
lb-be2#
--- conn 3 ---
lb-be2#
```

## テスト

`tests/test_bigip_lb_healthcheck.py`(35 tests、`bigip`に対する
初めての専用テストファイル——従来は`tests/test_untested_device_types.py`
の最小限のスモークテストのみだった)が、CLI設定(pool/virtual/
snatpool/persistence作成)、実TCPヘルスチェック(up/down/monitor無し)、
`VirtualServerProxy`単体の振り分けロジック(round-robin/
least-connections/down除外、source-addrパーシステンスの固定/
フォールバック、SNAT automap/snatpoolの実ソケットレベル確認、簡易
TCPバックエンドで検証)、名前付きオブジェクトの一意性(pool/
snatpool/persistence/nodeの再投入で重複しないこと)、デフォルト
Self IP/`net vlan`/`net self`(`TestBigipMockSelfIp`)、5台の仮想
クライアント→実http.server 2台の実HTTPトラフィック中継(round-robin/
persistence、`TestBigipHttpTrafficFromMultipleVirtualClients`)、
および実SSHリスナーを持つ2台のバックエンドへのエンドツーエンドの
round-robin振り分け+実フェイルオーバー(本物のuvicornサーバ+本物の
paramiko
クライアント)を固定する。
