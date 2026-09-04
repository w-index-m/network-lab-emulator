# BGP community（2026-09-04）

## 何をしたか

`docs/feature-inventory.md`に「計画中」と書かれていたBGP communityを
実装した。実際には`BgpRoute.communities`フィールドやCLIパーサー
（`set community`）はすでに存在していたが、**設定してもcommunityが
一切ピアに届かない**状態だった。原因は3つ重なっていた。

## 見つかったバグ

### ① `_recalc_best_path()` がベストパス再計算のたびにcommunityを捨てていた

`loc_rib`（ベストパステーブル）を作り直す際、辞書に`communities`
キーが無く、route-mapで付けたcommunityがベストパス再計算のたびに
消えていた。

### ② `_propagate_bgp()` の差分更新が as-path/local-pref/med しか見ていなかった

対向に経路が**既に存在する**状態で route-map / send-community を
後から設定しても、変化検出の比較対象に`communities`が含まれて
いなかったため、既存レコードが更新されなかった（`changed`フラグも
立たず、伝播が止まる）。

```python
# 修正前: communitiesの比較/更新が無い
elif (cur.as_path != rr.as_path or cur.local_pref != rr.local_pref
      or cur.med != rr.med):
    cur.as_path = rr.as_path
    ...
```

### ③ route-map/send-community の設定変更が即座に反映されなかった

`set_neighbor_route_map()` / `set_neighbor_send_community()` は
セッション上のフラグを変えるだけで、**その場で再伝播しなかった**。
実機ならsoft-reconfigurationに相当する動きが必要な場面。次に何か
別の変化（新しいnetwork広告など）が起きるまで、設定した属性が
既存の確立済みセッションには反映されなかった。

## 動作確認（実機に近いCLIで再現）

```
catalyst(config)# route-map SET-COMM permit 10
catalyst(config-route-map)# set community 65001:100
catalyst(config)# router bgp 65001
catalyst(config-router)# neighbor 10.99.99.2 route-map SET-COMM out
catalyst(config-router)# neighbor 10.99.99.2 send-community
```

```
cat# show ip bgp 172.20.0.0/24
BGP routing table entry for 172.20.0.0/24, version 1
Paths: (1 available, best #1, table default)
  65001
    10.99.99.1 from 10.99.99.1 (10.1.0.188)
      Origin IGP, metric 0, localpref 100, valid, external, best
      Community: 65001:100

cat# show ip bgp community 65001:100
   Network          Next Hop            Metric LocPrf Weight Path
*> 172.20.0.0/24      10.99.99.1                0    100      0 65001 i
```

`send-community`を設定しない限り、communityは一切広告されない
（実機の既定動作）ことも確認済み。

## 新規に追加したコマンド

- `show ip bgp <prefix>[/<len>]` — 経路詳細（community等の属性を表示）。
  `show ip bgp`のテーブル形式にはcommunityが出ないため、確認できる
  のは実質このコマンドだけ
- `show ip bgp community <community>` — 指定communityを持つベストパス
  だけを表示

## `set community <value> additive`

`additive`を付けると既存communityを消さず追加、付けないと置き換える
（実機と同じ挙動）。

## わかっている制約

route-mapに`match community <list>`のような**マッチ条件**は実装して
いない。このリポジトリのroute-mapは元々`set`句のみを無条件適用する
設計で、prefix-listもBGP側では`neighbor <ip> prefix-list <name> in|out`
という別経路でフィルタしている。`match`句全般（community-list以外も
含む）の追加は、route-mapのシーケンス番号ごとのpermit/deny評価という
別の大きめの設計変更が必要なため、今回は見送った。

## テスト

`tests/test_protocols.py`の`TestBgpCommunity`に9件追加。
`_propagate_bgp()`の差分更新漏れ、多段中継でのcommunity引き継ぎ、
`send-community`無効化時の撤回、`additive`の有無での挙動差を検証。
