# 「変わった検証」3件: gNMI over BIG-IP / CPS新規接続レート / automapのSelf IP

## これは何か

ユーザー質問「ちなみにClaude側で思いつく変わった検証ってどんなのあります？」
への回答として3件を提案し、「3つくらい上げてその検証を進めてマークダウンに
残してほしい」との指示で実際に手を動かして確認した。狙いは、このセッション中
に別々に実装された機能同士を、普段は組み合わせないやり方で突き合わせること
——それぞれ「確認して終わり」ではなく、実際に1件の真正なバグを見つけた。

1. **gNMI(gRPC/HTTP2)がBIG-IPのTCPリレーを素通りするか** — `VirtualServerProxy`
   はHTTP/1.1とSSHでは実証済みだが、全く違うプロトコル(gRPC/HTTP2)でも
   「プロトコルを理解しない純粋なバイトリレー」という設計通り動くか。
2. **BIG-IPのSSHバックエンドに対するCPS(秒間新規コネクション数)試験** —
   k6のHTTP試験は「同時並列数」を測ったが、これは「新規接続の生成レート」
   という別の軸でのストレス試験。
3. **automap SNATが実際に設定済みのSelf IPを使うか** — 「モックIP/Self IP」
   機能と「SNAT」機能は別々のタイミングで実装されたため、組み合わせテストが
   一度も無かった。

## 1. gNMI(gRPC/HTTP2) over BIG-IP

### やったこと

`pip install pygnmi`でクライアントを用意し、Cisco装置に`gnxi`/`gnxi server`
で実gNMIリスナー(ポート50052)を立て、それをBIG-IPのpool+virtualの背後に
配置した。

```
create ltm pool gnmipool { members add { 10.98.0.1:50052 } monitor tcp }
create ltm virtual gnmivs { destination 10.98.0.100:50052 pool gnmipool }
```

実際の`pygnmi.client.gNMIclient`で**BIG-IPのVIP**(`10.98.0.100:50052`、
Cisco装置本体ではない)に対して`Capabilities`・`Get`を実行:

```python
from pygnmi.client import gNMIclient
with gNMIclient(target=('10.98.0.100', 50052), username='admin', password='admin', insecure=True) as c:
    cap = c.capabilities()
    r = c.get(path=['interfaces/interface[name=GigabitEthernet0/0/0]'], encoding='json_ietf')
```

### 結果: 成功

```
CAPABILITIES via BIG-IP OK, gNMI_version= None
GET via BIG-IP notification count: 1
{'notification': [{'update': [{'path': 'interfaces/interface[name=GigabitEthernet0/0/0]',
  'val': {'name': 'GigabitEthernet0/0/0', 'enabled': True,
          'ietf-ip:ipv4': {'address': [{'ip': '10.98.0.1', 'netmask': '255.255.255.0'}]}}}]}]}
```

実際にBIG-IPのVIP経由で、Cisco装置の実インタフェース設定(`10.98.0.1`)が
gRPC/HTTP2のバイナリプロトコルで正しく往復した。コード変更は不要——
`VirtualServerProxy`が本当にプロトコルを一切解釈しないバイトリレーである
ことの、HTTP/SSH以外での初めての実証になった。

### 検証中に踏んだ環境的な落とし穴(エミュレータのバグではない)

最初`203.0.113.2`宛でgNMI接続を試みたところ、grpcが
`PERMISSION_DENIED ... Trying to connect an http1.x server (HTTP status 403)`
という意味不明なエラーを返した。原因はこのサンドボックスの`https_proxy`/
`no_proxy`環境変数——`no_proxy`には`127.0.0.0/8`・`10.0.0.0/8`・
`172.16.0.0/12`・`192.168.0.0/16`・`100.64.0.0/10`は含まれるが、
**`203.0.113.0/24`(TEST-NET-3、cisco/arista/fortigateのデフォルトWAN
アドレスに使われている)は含まれていない**ため、grpc-pythonがこの
sandboxのエージェントプロキシ(`127.0.0.1:38343`)経由でルーティングして
しまい、プロキシ側がプレーンなgRPC平文トラフィックをHTTP/1.1として誤認
して403を返していた。`10.0.0.0/8`の範囲(`no_proxy`に含まれる)の
インタフェースIPを使うことで回避——このエミュレータ自体やBIG-IPの
実装には問題がない、サンドボックス環境特有の制約。

## 2. CPS(秒間新規コネクション数)試験 — BIG-IPのSSHバックエンド

### 背景

別プロジェクト(CPS/VU負荷試験GUI)の話をしていた際に出てきた
「CPS(Spirent Avalancheのような新規コネクション処理性能そのものの
試験)」という考え方を、このラボのBIG-IPに対して実際に適用してみた。
k6は主にHTTPを想定しているため、SSHの新規コネクションレート試験は
Pythonで自作した(`paramiko`で毎回新規にTCP+SSHハンドシェイクを行い、
一切再利用しない)。

### セットアップ

```
# バックエンド: Catalystに実SSH
configure terminal / interface Vlan10 / ip address 10.99.1.1 255.255.255.0
crypto key generate rsa modulus 2048

# BIG-IP
create ltm pool cpspool { members add { 10.99.1.1:22 } monitor tcp }
create ltm virtual cpsvs { destination 10.99.1.100:22 pool cpspool }
```

試験スクリプト(`run_burst`)は目標CPSに応じた間隔でスレッドを起動し、
各スレッドが`socket.create_connection`→`paramiko.Transport.start_client`
→`auth_none`(認証失敗はSSHハンドシェイク自体の成否には関係ないので
許容)という、実際のTCP+SSHハンドシェイクを毎回フルで行う。

### 結果: 低レートでは完璧、~100 CPS付近で明確な壁

| target CPS | attempted | achieved CPS | ok | fail | avg latency |
|---|---|---|---|---|---|
| 10  | 30  | 9.9  | 30  | 0   | 56.9ms |
| 30  | 89  | 29.2 | 89  | 0   | 54.8ms |
| 60  | 171 | 56.7 | 171 | 0   | 53.4ms |
| 100 | 282 | 92.4 | 282 | 0   | 48.9ms |
| 200 | 494 | 105.2| 476 | 18  | 424.8ms |
| 400 | 718 | 102.7| 454 | 264 | 1110.9ms |
| 800 | 754 | 109.4| 529 | 225 | 1148.9ms |

10〜100 CPSの目標ではほぼ100%達成・失敗ゼロ・レイテンシ安定(~50ms)。
200 CPS以降は**実際に達成できるレートが~100〜110 CPSで頭打ちになり**、
それを超えて無理に接続しようとした分は失敗(`Connection reset by peer`
でSSHバナー受信前に切断)かレイテンシの激増(1秒超)という形で現れる。

**これは以前k6のHTTPテストで見つけたのと同じ根本原因(`engine/bigip_lb.py`
の`VirtualServerProxy`が接続ごとにスレッドを2〜3本生成する素朴な
thread-per-connection実装によるCPythonのGIL競合)だが、現れ方が違う**:
HTTPテストは「同時並列数(200 VU同時)」で壊れたのに対し、このCPS試験は
「新規接続の生成レート(秒間100件超)」で壊れる——両方とも最終的には
「同時に生きているPythonスレッド数」に帰着するが、負荷のかけ方の軸が
違うと壊れ方の表現も違う、という実務的に重要な区別が実際に確認できた。

## 3. automap SNATが設定済みのSelf IPを使うか(実バグ発見)

### 仮説と検証

今セッションで「モックIP/Self IP」(`net vlan`/`net self`)と「SNAT
(automap/snatpool)」は別々のタイミングで実装された。automapは
「仮想サーバ自身のVIPを送信元にする」という実装だったが、実機のF5
AutoMap SNATは**仮想サーバのdestination(VIP)ではなく、発信側VLANの
Self IPを送信元に使う**——VIPとSelf IPは別概念で、VIPがSelf IPである
必要すら無い。この2機能を組み合わせたテストがこれまで一度も無かった
ため、見逃されていた可能性を疑い、実際に確認した。

### ライブ検証(修正前)

```
create net vlan internal { interfaces add { 1.2 } }
create net self my_self address 10.95.50.245/24 vlan internal
create ltm pool peerpool { members add { 10.95.60.1:9701 } monitor tcp }
create ltm virtual peervs { destination 10.95.70.1:9701 pool peerpool
    source-address-translation { type automap } }
```

接続元IPをそのまま返す簡易バックエンドへVIP経由で接続した結果:

```
configured Self IP: 10.95.50.245
virtual server destination (vip): 10.95.70.1
backend saw source IP: 10.95.70.1   ← Self IPではなくVIPが見えていた(バグ)
```

### 修正

`engine/bigip_lb.py`の`get_snat_source()`のautomap分岐を、
`state.f5_selfips`(`net self`で設定済みのSelf IP)があればそれを使い、
一つも設定されていない場合のみ(既存のテスト/ドキュメントとの後方互換の
ため)従来通りVIPへフォールバックするよう修正。

### ライブ検証(修正後)

```
backend saw source IP: 10.95.50.245   ← 設定したSelf IPが正しく使われる
```

Self IP未設定のケースでも従来通りVIPへフォールバックすることを別装置で
再確認済み(`backend saw source IP (no net self configured): 10.95.80.1`)。

### テスト

`tests/test_bigip_lb_healthcheck.py`に`TestBigipAutomapUsesConfiguredSelfIp`
(2 tests、ファイル計37 tests)を追加: `net self`設定済みならそのIPを
automapが使うこと、未設定ならVIPへフォールバックすること(regression
防止)を固定する。

## まとめ

| # | 検証 | 結果 |
|---|---|---|
| 1 | gNMI over BIG-IP | 成功(設計通りプロトコル非依存のリレー) |
| 2 | SSH CPS試験 | ~100 CPSで明確な壁(既知のスケール限界の別側面) |
| 3 | automapのSelf IP | **実バグ発見・修正済み**(VIPではなくSelf IPを使うべきだった) |

3件とも「別々に実装された機能を、普段と違う組み合わせ・別の角度で
実際に動かしてみる」という方針が功を奏し、特に3は見た目には気づき
にくい実装ギャップを実際のライブ検証で初めて発見できた——このセッション
を通じて繰り返し確認されている教訓(「コードレビューだけでは見つからない、
実際に動かして初めて分かるバグがある」)の、また別の実例になった。
