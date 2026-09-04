# OSPF NSSA（2026-09-04）

## 何をしたか

`docs/feature-inventory.md`に「計画中」だったOSPF NSSAを実装した。

このエミュレーターのOSPFはエリア=ノード単位の簡易モデル（実機のような
ABRでの複数エリア境界を跨いだLSDBは持たない）のため、実機フル互換の
Type-7↔Type-5変換までは再現していない。その代わり、**最も観測しやすく
実務でも確認頻度が高い違い**を実装した:

> NSSAエリアで再配信された経路は、実機では`show ip route`上で
> `E2`ではなく`N2`（NSSA External）と表示される。

## 設定

```
router ospf 1
 network 10.50.50.0 0.0.0.255 area 5
 area 5 nssa
 redistribute static subnets
```

`area <id> stub`も同じ経路で受け付ける（`no-summary`は構文として
許容するが、Totally NSSA/Totally Stubby特有のsummary抑制動作までは
今回実装していない）。

## 動作確認

```
catalyst# show ip protocols
Routing Protocol is "ospf 1"
  ...
  Number of areas in this router is 1. 0 normal 0 stub 1 nssa

cat# show ip route
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

N2       10.9.9.0/24 [110/20] via catalyst,
```

同一エリアの隣接に再配信すると`O E2`ではなく`O N2`で表示される。
通常エリア（NSSAでない）での再配信は従来どおり`O E2`のまま
（回帰テストで確認済み）。

## 実装箇所

- `OspfEngine.set_area_type()` / `is_area_nssa()` — `n['area_types']`
  にエリアID→'nssa'/'stub'を保持
- Hello送信時に`area_is_nssa`フラグを載せ、受信側は
  `_learned_external`にNSSA由来かどうかを記録
- `_inject_external_routes()`でNSSA由来なら`type: 'O N2'`、
  そうでなければ従来どおり`'O E2'`
- `RibEngine.format_show_ip_route()`のコード表示を`ospf_code`優先に
  変更し、N1/N2/E1/E2の凡例を追加
- `show ip protocols`のエリア集計（normal/stub/nssa）を実際の設定
  から動的に算出（以前は`1 normal 0 stub 0 nssa`固定だった）

## わかっている制約

- ABRでのType-7→Type-5変換（バックボーンへ跨いだ先はE2に戻る）は
  未実装。このエミュレーターの「エリア=ノード」という簡易モデルでは、
  ABRが両エリアに同時に属するという状態そのものを表現していないため。
- `no-summary`（Totally NSSA）によるsummary LSA抑制は未実装。
- **`show ip ospf`（サフィックス無し）は、実は`ospf_engine`ではなく
  `rules.py`側の別の簡易テンプレート実装が使われている**
  （`state.ospf`という別データを見ている）。今回のNSSA実装は
  `ospf_engine`側にしか反映しておらず、この bare `show ip ospf`
  では見えない。`show ip protocols`（今回対応）や`show ip route`
  では正しく反映される。これは今回のスコープ外の既存の食い違いで、
  次に触る機会があれば直したい。

## テスト

`tests/test_protocols.py`の`TestOspfNssa`に5件追加。
