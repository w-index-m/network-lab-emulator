# AWS Direct Connect オンプレ側BGP設計（エミュレータでの事前検証）

**目的**: 物理Direct Connectを引く前に、オンプレ側ルータのBGP設計を
このエミュレータ（Cisco 2台構成）で先に検証する。AWS環境が用意でき次第、
ここで固めた設計値をそのまま実機/AWSコンソールに反映する。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`（このリポジトリ）のHTTP APIに対する
curlコマンドで再現できる。

---

## 前提: Direct Connectの構成要素

```
[オンプレ環境] --- Direct Connect (専用線/クロスコネクト) --- [Private VIF] --- [VGW/DXGW] --- [VPC]
```

- **Private VIF**: オンプレ側とAWS側でBGPピアリングするための仮想インターフェース。
  ピアリングIPは通常 `169.254.x.x/30`（AWSが払い出すか、自分のIP空間を使うかを選べる）
- **BGPセッション**: オンプレ側ASNとAWS側ASN（VGWのデフォルトは64512、DXGWも同様の
  デフォルトがあるが変更可能。実際の値は環境作成時に確認すること）
- Direct Connect自体は**暗号化されない**（同じデータセンター/プロバイダー経由の
  専用線のため）。暗号化が必要な区間は別途VPNを重ねる

## 検証した設計値

| 項目 | 値 | 備考 |
|---|---|---|
| オンプレ側AS番号 | 65010 | プライベートAS。任意の値でよい |
| AWS側AS番号 | 64512 | VGW/DXGWのデフォルトASN。**実環境作成時に必ず確認** |
| BGPピアリングIP | 169.254.100.0/30 | Private VIFで一般的なリンクローカル範囲 |
| オンプレ側CIDR | 10.100.0.0/16 | オンプレからAWSへ広告する集約経路 |
| VPC側CIDR | 10.200.0.0/16 | AWSからオンプレが受信する経路 |

## エミュレータでの検証手順

サーバーを起動:
```bash
NETLAB_AUTH_DISABLE=1 python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
```

装置を2台作成し、リンクを張る:
```bash
B=http://127.0.0.1:8000
curl -s -X POST $B/api/device -d '{"id":"dx-onprem","type":"cisco","hostname":"dx-onprem-rtr"}'
curl -s -X POST $B/api/device -d '{"id":"dx-awsvif","type":"cisco","hostname":"dx-awsvif-rtr"}'
curl -s -X POST $B/api/link -d '{"a":"dx-onprem","b":"dx-awsvif","iface_a":"GigabitEthernet0/1","iface_b":"GigabitEthernet0/1"}'
```

オンプレ側ルータ（`dx-onprem`）:
```
conf t
interface GigabitEthernet0/1
ip address 169.254.100.1 255.255.255.252
no shutdown
exit
interface Loopback0
ip address 10.100.0.1 255.255.255.0
exit
router bgp 65010
network 10.100.0.0 mask 255.255.0.0
neighbor 169.254.100.2 remote-as 64512
end
```

AWS側を模したルータ（`dx-awsvif`。実際はDXGW/VGWだが、BGPの振る舞いを
確認する目的でCiscoルータとして代替している）:
```
conf t
interface GigabitEthernet0/1
ip address 169.254.100.2 255.255.255.252
no shutdown
exit
interface Loopback0
ip address 10.200.0.1 255.255.255.0
exit
router bgp 64512
network 10.200.0.0 mask 255.255.0.0
neighbor 169.254.100.1 remote-as 65010
end
```

数秒待ってから確認:
```bash
curl -s -X POST $B/api/cli -d '{"device_id":"dx-onprem","command":"show ip bgp summary"}'
curl -s -X POST $B/api/cli -d '{"device_id":"dx-onprem","command":"show ip route"}'
```

### 実際に確認できた結果

```
Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
169.254.100.2   4 64512     103      74        1    0    0 00:00:01  1
```

```
B        10.200.0.0/16 [20/0] via 203.0.113.2, GigabitEthernet0/0/0
```

VPC側CIDR (`10.200.0.0/16`) をオンプレ側が正しくBGP経由で学習した。

**既知の表示上の癖**: next-hopが `169.254.100.2` ではなく別インタフェース
(`203.0.113.2`、装置の既定WANインタフェースのIP)として表示される。
これは`icmp_engine.resolve_learned_next_hop`が装置に複数のローカルIPが
ある場合の解決先を誤って選ぶ表示バグで、経路交換自体（AD/metric/prefix）は
正しい。next-hop表示の正確さを検証する用途には現状使えないので注意。

---

## AWS側の対応コンフィグ（実環境作成時に使う値）

上記のオンプレ側Cisco設定を実際のAWS環境に持っていく場合、AWSコンソール/CLIで
入力する項目は以下の通り対応する。

### Customer Gateway（AWS側でオンプレ情報を登録）

```bash
aws ec2 create-customer-gateway \
  --type ipsec.1 \
  --public-ip <オンプレ側の固定グローバルIP> \
  --bgp-asn 65010
```

※ Direct Connect自体にはCustomer Gatewayは不要（VPN専用のリソース）。
Direct Connectの場合は代わりに **Direct Connect Gateway** または
**Virtual Private Gateway** を作成し、そこにPrivate VIFを関連付ける。

### Private VIF（Direct Connect側、AWSコンソールの実際の入力項目）

| AWSコンソールの項目 | 今回の設計値 |
|---|---|
| Virtual Interface Type | Private |
| VLAN | プロバイダーから払い出された値 |
| BGP ASN (Your side) | 65010 |
| Your router peer IP | 169.254.100.1/30 |
| Amazon router peer IP | 169.254.100.2/30 |
| BGP Auth Key（任意） | 設定する場合はMD5鍵 |

### Direct Connect Gateway / VGW側

- VPCにアタッチしたVirtual Private Gatewayに対してPrivate VIFを関連付ける
- 複数VPCがある場合は **Direct Connect Gateway** を経由して
  **Transit Gateway** にアタッチする構成にする（後述）

---

## Transit Gateway を挟む場合（複数VPC構成）

現状の検証は「オンプレ ⇔ 1つのVPC」の単純な構成だが、実際にTransit Gatewayを
使う予定とのことなので、構成イメージを整理しておく。

```
[オンプレ] --- DX ---[DX Gateway]--- [Transit Gateway] --- [VPC-A]
                                            └──────────── [VPC-B]
```

- Direct Connect Gateway と Transit Gateway を **Transit VIF** で接続する
  （Private VIFではなくTransit VIFを使う点に注意。1本のDXで複数VPCに
  到達させるための仕組み）
- オンプレ側から見えるASNはDX Gateway側のASN（Amazon側デフォルト64512とは
  別に、DX Gateway自体のASNを持つ）
- Transit Gateway配下の各VPCへのルーティングは、Transit Gatewayの
  ルートテーブルで制御する（VPCごとにアタッチメントを作り、経路を
  伝播/フィルタする）

この部分（Transit VIF + Transit Gateway ルートテーブル設計）は
Private VIFの単純な構成とは検証の仕方が変わるため、次のステップとして
別途エミュレータ上で複数VPC相当（＝複数の「AWS側ルータ」）を用意して
検証するのが良い。

---

## テストハーネスに関する注意（このリポジトリで作業する開発者/LLM向け）

エミュレータのBGP実装は、ネイバー確立を `_spawn()`（`asyncio.create_task`）で
生成した非同期タスクが `await asyncio.sleep(1.0)` 後に開始する設計になっている
（`engine/protocols.py` の `BgpEngine.add_neighbor` 内 `_delayed_open`）。

**`fastapi.testclient.TestClient` を裸の状態（`with`コンテキストを使わず、
かつ後続のHTTPリクエストを起こさずに）で使い、`time.sleep()` で待っても、
このバックグラウンドタスクは実行されずキャンセルされる。**

実際に `asyncio.Task` を追跡して確認したところ、タスクは生成から
数ミリ秒でキャンセルされていた（`task.cancelled() == True`）。これは
TestClientがリクエストごとに独立したイベントループ実行を行うことに起因する
テストハーネス特有の制約で、**エミュレータ本体のバグではない**。

このリポジトリの `tests/` 配下の既存テストが同じ`TestClient`パターンを
使いながら正常に動いているのは、それらのプロトコル（EIGRP/DPD等）の
検証が「1回のリクエスト内で完結する同期的な処理」または
「ポーリング方式（クエリ時に経過時間を計算する）」に依っているためで、
BGPの`_delayed_open`のように**複数秒にわたって生存し続ける必要のある
バックグラウンドタスク**は、この種のアドホックな検証では正しく動かない。

**対処法（手動でAPIを叩いて確認する場合）**: 実際にuvicornでサーバーを
起動し、本物のHTTPリクエスト（curl等）で検証する。

```bash
NETLAB_AUTH_DISABLE=1 nohup python3 -m uvicorn app:app --host 127.0.0.1 --port 8123 &
# ここから先はcurlでAPIを叩く（TestClientは使わない）
```

**pytestで自動テストを書く場合**: 既存の`tests/test_bgp_advanced.py`は
そもそもHTTP層（`TestClient`）を経由せず、`engine.protocols.bgp_engine`を
pytest-asyncio の単一イベントループ内で直接ドライブしている
（`@pytest.fixture`でエンジンをリセットし、`await bgp_engine.start(...)`等を
直接呼ぶ）。この書き方なら1つの`await`チェーンの中で完結するため、
今回のような「リクエストをまたいで生存する必要のあるバックグラウンド
タスク」の問題自体が発生しない。HTTP API経由でBGPの非同期確立を
テストしたい場合は、`tests/test_ipsec_dpd_timers.py`のように
`TestClient`を**インポート直後に一度も再生成せず使い続け、確認したい
状態変化の直前に十分な`time.sleep`を入れる**形にする（この方式は
本ドキュメントの検証で使ったアドホックスクリプトとは異なり、pytest経由
だと同一プロセス内でイベントループが持続するため問題が起きにくい）。
迷ったら、まず`engine.protocols`を直接ドライブする既存BGPテストの
書き方を踏襲するのが安全。
