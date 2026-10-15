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

## スコープ外(実装していない)

- httpモニターの実際のHTTPリクエスト送信/レスポンス検証(この
  エミュレータの装置は実HTTPサーバを持たないため、tcp相当の接続性
  チェックに留める)
- SSL/TLSプロファイル、iRules、persistence(セッション永続化)
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

`tests/test_bigip_lb_healthcheck.py`(11 tests、`bigip`に対する
初めての専用テストファイル——従来は`tests/test_untested_device_types.py`
の最小限のスモークテストのみだった)が、CLI設定(pool/virtual作成)、
実TCPヘルスチェック(up/down/monitor無し)、`VirtualServerProxy`単体の
振り分けロジック(round-robin/least-connections/down除外、簡易TCP
バックエンドで検証)、および実SSHリスナーを持つ2台のバックエンドへの
エンドツーエンドのround-robin振り分け+実フェイルオーバー(本物の
uvicornサーバ+本物のparamikoクライアント)を固定する。
