# CDP/LLDP と実トポロジー(`vnet.links`)の不整合修正

## 症状

`show cdp neighbors` には隣接装置が表示されているのに、その相手と
OSPF/RIP等のルーティングプロトコルのパケットが**一切流れない**。
ネイバーが上がらない原因が設定ミスなのかリンク不良なのか判別できず、
切り分けが極めて困難だった。

実際に踏んだ例:

```
catalyst# show cdp neighbors
Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID
Core-SW          Gig 1/0/24        150        S           SR-S324TR1 ether 10
GW-Router        Gig 1/0/1         120        R           ISR4321   Gig 0/0/0
```

CDP上は `GW-Router`（cisco）が隣接に見えるが、
`saved_config.json` の `links` には `[{'a': 'sir', 'b': 'cat'}]` しか
無く、`vnet.links['catalyst']` に `cisco` が入っていない。
その結果 `vnet.broadcast_to_neighbors()` のループが cisco に到達せず、
OSPFのHelloが1つも届かないため `show ip ospf neighbor` は
`(No neighbors)` のままだった。

## 根本原因

**CDP/LLDPの表示データと、実際のパケット転送に使うトポロジーグラフが
別物だった。**

- 実際の隣接判定・パケット転送: `vnet.links`（`engine/protocols.py`）
- CDP/LLDPの表示: `DeviceState.cdp_neighbors` / `.lldp_neighbors`
  （`engine/rules.py`）

`DeviceState.__init__` は**装置の種類やリンクの有無に関係なく**、
全装置に固定のサンプルネイバー（`Core-SW` と `GW-Router`）を
持たせていた。さらに、これを実トポロジーから作り直す
`_rebuild_all_neighbors()` は `/api/topology/sync` 経由でしか呼ばれず、
**アプリ起動時の `_load_config()` では呼ばれていなかった**。

つまり起動直後は、どの装置も「実在しない隣接装置」を表示していた。

## 修正内容

1. **`engine/rules.py`**: `DeviceState.__init__` のサンプルネイバーを
   削除し、`cdp_neighbors` / `lldp_neighbors` を空で初期化する。
   ネイバー情報は実トポロジーからのみ構築されるようにした。

2. **`app.py` `_load_config()`**: 最後に `_rebuild_all_neighbors()` を
   呼び、起動時点で CDP/LLDP が `vnet.links` と一致するようにした。

3. **`app.py` `_update_neighbors()`**: 表示するインターフェース名を
   「その装置の最初のインターフェース」(`_first_if`) ではなく、
   実際にリンクが張られているインターフェース
   (`vnet.interface_links[dev][peer]`) から取るようにした。
   これが無いと、複数インターフェースを持つ装置で
   「隣接はしているが表示されるポート名が実際のリンクと違う」という
   別の食い違いが残る。

4. **`engine/rules.py` `_show_cdp()`**: 実インターフェース名を使うと
   固定幅18文字を超えて `GigabitEthernet1/0/1150` のように
   holdtime と繋がって表示されてしまうため、実機IOSと同様の短縮表示
   （`GigabitEthernet1/0/1` → `Gig 1/0/1`）と、幅超過時も必ず空白を
   入れるパディングを実装した。

## 修正後の確認（2026-09-03）

`saved_config.json` の links は `sir↔cat` と `catalyst↔cisco` の2本。

```
catalyst# show cdp neighbors
Device ID        Local Intrfce     Holdtme    Capability  Platform  Port ID
GW-Router        Gig 1/0/1         150        R           CISCO     Gig 0/0/0

Total cdp entries displayed : 1
```

- catalyst → `GW-Router`(cisco) のみ。実リンクと一致 ✅
- cisco → `Dist-SW`(catalyst) のみ。逆方向も一致 ✅
- srs → `Total cdp entries displayed : 0`。リンクが無いので正しく0件
  （修正前は実在しない `Core-SW`/`GW-Router` が表示されていた）✅
- ポート名も実際のリンクインターフェース
  (`Gig 1/0/1` ↔ `Gig 0/0/0`) が表示される ✅

## テスト

`tests/test_cdp_topology_consistency.py`（4件、ソケット不使用で高速）

```bash
pytest tests/test_cdp_topology_consistency.py -v
```

- リンク未接続の装置が CDP/LLDP ネイバーを持たないこと
- `show cdp neighbors` が 0 件と表示され、サンプル装置名が出ないこと
- 長いインターフェース名でも列がくっつかないこと
- インターフェース名短縮ロジック

## 同じ罠が無いか調べた結果（他プロトコルの棚卸し）

「表示用の固定データ」と「実際に動いているエンジン」が食い違う箇所が
他にも無いか、全表示コマンドを確認した。

| 機能 | 実エンジン | 未設定時の表示 | 判定 |
|---|---|---|---|
| CDP / LLDP | `vnet.links` から構築 | 0件 | **修正済み**（本ドキュメント） |
| EIGRP | **無し**（`router eigrp` 未実装） | 以前は幽霊ネイバー `10.0.0.2` | **修正済み**（下記） |
| HSRP | 無し | `% HSRP is not configured...` | OK（既に対策済み） |
| RIP / OSPF / BGP | あり | 設定に追従 | OK |
| STP / VRRP / LACP / VLAN / vPC / NAT / ARP / CEF / RIB | あり | 設定に追従 | OK |

### EIGRP の幽霊ネイバー（修正済み）

`DeviceState.__init__` が固定のEIGRPネイバー
`{"ip":"10.0.0.2","iface":"Gi0/0/0",...}` を持っていたため、EIGRPを
一切設定していない装置でも `show ip eigrp neighbors` に隣接が確立して
いるかのように表示されていた（CDPと全く同じ構造の問題）。

EIGRPは設定コマンド(`router eigrp`)もプロトコルエンジンも未実装なので、
`enabled: False` を持たせ、未設定時は実機同様に
`% EIGRP is not configured on this device.` を返すようにした。
`show ip eigrp topology` も同様。

（Nexusの `show ip eigrp neighbors` は元から `(no EIGRP neighbors)` と
正しく応答していた。装置種別によって実装が分かれていた。）

## 関連する注意点

トポロジーを変更した場合（`POST /api/link` など）は
`_update_neighbors()` が呼ばれて CDP/LLDP も追従する。
手で `saved_config.json` の `links` を編集した場合は、
**アプリを再起動すれば** `_load_config()` → `_rebuild_all_neighbors()`
の順で整合が取られる。

なお `saved_config.json` を直接編集する場合は必ず先に `app.py` を
完全停止すること（シャットダウン時に `_save_config()` がメモリ上の
状態を書き戻すため、稼働中の編集は上書きで消える）。
