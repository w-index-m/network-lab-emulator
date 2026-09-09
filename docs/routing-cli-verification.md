# RIP/OSPF/EIGRP/BGP show出力の実機突き合わせ

STP/RESTCONFで行った「WebSearch + GitHub実装例で実機出力を確認し、
このエミュレータの出力と突き合わせる」手順を、ルーティングプロトコル
（RIP/OSPF/EIGRP/BGP）の主要な`show`コマンドにも適用した記録。

対象読者はClaude以外のLLM（Qwen等）でも良い。

## 見つけて修正した問題: `show ip eigrp topology`

### 実機の実際の出力（Web検索で確認）

```
P 9.9.9.0/29, 1 successors, FD is 31232 via 10.1.2.2 (31232/30976), GigabitEthernet0/0
        via 10.1.3.3 (33536/30976), FastEthernet1/0
        via 10.1.4.4 (286976/30976), FastEthernet1/1
```

ポイント:
- **1件目の`via`はサマリ行(`FD is X`)と同じ行**に続けて出る
- **各`via`行に送出インタフェース名が付く**（`(FD/RD), <interface>`）
- 2件目以降の経路（フィージブルサクセサ等）だけが次行以降にインデントされる

### 修正前のこのエミュレータの出力

```
P 9.9.9.0/29, 1 successors, FD is 31232
        via 10.1.2.2 (31232/30976)
```

サマリ行と1件目の`via`が別行になっており、インタフェース名も
欠落していた。

### 修正内容

`engine/rules.py`の`_show_eigrp_topology`で:
- 1件目の経路の`via`をサマリ行に連結
- `engine.protocols.rib_engine`の`_iface_for_nexthop`/
  `_iface_for_network`（RIP/OSPF/BGP由来の経路で既に使われている、
  ネクストホップIPから送出インタフェースを逆引きするヘルパー）を
  EIGRPトポロジー表示にも流用してインタフェース名を追加

### 実際に確認した動作

2台のCiscoルータでEIGRP AS 100を組み、`show ip eigrp topology`を
実行:

```
EIGRP-IPv4 Topology Table for AS(100)/ID(0.0.0.0)
Codes: P - Passive, A - Active, U - Update, Q - Query, R - Reply,
       r - reply Status, s - sia Status

P 10.50.0.0/24, 1 successors, FD is 2816 via Connected, GigabitEthernet0/1
```

直結経路(`via Connected`)にもインタフェース名が正しく付き、実機の
形式と一致することを確認した。

## 見つけて修正した問題: `show vpc`（Nexus vPC）

「モックNexusをNexus 93180のマニュアルも参考に見直してほしい」という
依頼を受け、vPC（Nexus固有機能）も同じ手法で確認した。

### 実機の実際の出力（Web検索で確認）

```
vPC domain id                     : 100
Peer status                       : peer adjacency formed ok
vPC keep-alive status             : peer is alive
...
vPC role                          : secondary
```

`Peer status`と`vPC keep-alive status`は**同じ内部状態でも異なる文言**
を使う（前者は隣接形成の可否、後者はキープアライブ疎通の可否という
別々の観点の表現）。

### 修正前のこのエミュレータの出力

```
Peer status                        : alive
vPC keep-alive status              : alive
```

内部の状態変数(`keepalive_state`: `pending`/`alive`/`dead`)をそのまま
両方のフィールドに出しており、実機の文言と異なっていた。

### 修正内容

`engine/protocols.py`の`format_show_vpc`で、内部状態から表示文言への
変換テーブルを追加し、フィールドごとに正しい文言を出すようにした:

| 内部状態 | Peer status | vPC keep-alive status |
|---|---|---|
| alive | peer adjacency formed ok | peer is alive |
| dead / pending | peer adjacency not formed | peer is not alive |

`tests/test_vpc.py`の該当テストも新しい文言に合わせて更新し、
11件全てパスすることを確認した。

## Nexus 93180マニュアルへのアクセスについて

`www.cisco.com`はこの開発環境のegressプロキシでブロックされているため、
Nexus 93180の公式コマンドリファレンスPDFへの直接アクセスはできなかった。
Web検索のスニペットとGitHub上の実装例（フォーラム投稿の引用等）経由での
確認にとどまる。実機やCisco公式サイトに直接アクセスできる環境で、
Nexus 93180固有の出力（プラットフォーム名、ライセンス表示等）を
別途照合することを推奨する。

## 確認して問題なかったもの

以下は実際にCLI経由で使われている実装（app.pyが優先ディスパッチする
本物のパス）を確認し、Web検索で得た実機出力と突き合わせたが、
修正の必要はなかった。

- **`show ip ospf neighbor`**(`ospf_engine.format_show_ospf_neighbor`):
  `Neighbor ID / Pri / State / Dead Time / Address / Interface`の
  列構成、`FULL/DR`のようなstate/role表記が一致
- **`show ip eigrp neighbors`**(`_show_eigrp_neighbors`):
  `H / Address / Interface / Hold / Uptime / SRTT / RTO / Q Cnt / Seq Num`
  の列構成、2行ヘッダの折り返し位置が一致
- **`show ip bgp summary`**(`bgp_engine.format_show_bgp_summary`):
  `docs/aws-directconnect-bgp-design.md`で実際にuvicornを起動して
  検証した際の出力と同じ列構成で、Web検索の実機例とも一致
- **`show ip route`のコードレター**(`L/C/S/R/B/D/EX/O/IA/N1/N2/E1/E2`等):
  以前確認済み（`docs/restconf-catalyst.md`より前の会話で検証）

## 見送った項目

- **`show ip rip database`**: Cisco IOS実機のRIP確認コマンドの一つだが、
  このエミュレータでは`device_type: cisco/catalyst`向けに未実装
  （Si-R向けの`show ip rip route`/`show ip rip protocol`は別途ある）。
  「実装が間違っている」のではなく「対応コマンドが無い」という
  ギャップなので、追加実装は別タスクとして扱う

## 関連ドキュメント

- `docs/restconf-catalyst.md` — 同じ手法で行ったRESTCONF/STPの
  実機突き合わせ記録
- `docs/aws-directconnect-bgp-design.md` — BGP実装の別途の実機検証
  （実際にuvicornを起動してcurlで確認した記録）
