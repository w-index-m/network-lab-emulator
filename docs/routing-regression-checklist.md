# ルーティング回帰試験チェックリスト（2台構成: RIP/OSPF/BGP/EIGRP）

Si-R/Catalyst/Cisco/Nexus 等の2台構成で RIP・OSPF・BGP・EIGRP のネイバー確立・
経路交換・疎通を確認した際の試験項目と、発見済みの問題点をまとめる。
新しいサブネットを使う場合は `saved_config.json` の既存IPと重複しないこと
（重複するとクロストークして偽ネイバーが出る。後述）。

- 起動: `NETLAB_AUTH_DISABLE=1 NETLAB_FAST_TIMERS=1 uvicorn app:app --port 8099`
- 最終確認: 2026-09-05

## 試験項目（各プロトコル×各ベンダー組み合わせ）

- [ ] ネイバー/隣接が確立する（Full、Established等）
- [ ] 対向の直接接続以外のネットワーク（Loopback等）が経路交換される
- [ ] `show ip route <proto>` の next-hop がIPアドレスで表示される（デバイスIDでない）
- [ ] ping で相互到達できる
- [ ] `router-id` 等の明示設定がネイバー表示に反映される
- [ ] interface shutdown / no shutdown でネイバーが正しく切断・再確立する（Dead Timer通り）

## 既知の問題（2026-09-05 確認）

### 1. OSPF: `router-id` コマンドが未実装（要修正）
`router ospf <process>` サブモード配下の `router-id X.X.X.X` がどこにもパースされず、
`state.ospf['router_id']` にも `ospf_engine.nodes[...]['router_id']` にも反映されない。
結果、`show ip ospf neighbor` に管理者が設定したIDでなく自動生成された無関係な
Router IDが表示される。

再現手順:
```
router ospf 1
router-id 102.102.102.102
network 100.64.12.0 0.0.0.3 area 0
```
→ `show ip ospf neighbor` のNeighbor ID欄が `102.102.102.102` にならない。

### 2. RIP/BGP: `show ip route rip` / `show ip route bgp` のnext-hop表示がデバイスID
実機なら `via 10.1.12.2, 00:00:12, GigabitEthernet0/0` となるべき箇所が
`via r2,` のようにデバイスID表示のまま（タイマー・インターフェースも欠落）。

### 3. （設計上の注意・仕様）実OSPF/RIPリスナーはサブネット一致のみでネイバー判別
`engine/real_ospf_agent.py` はraw IPプロトコル(89)のリスナーを全装置共有の `lo` 上で
待ち受け、パケット送信元IPが自分と同一サブネットかどうかだけでネイバーを判別する
（`vnet` のリンクトポロジーとは独立）。そのため、無関係な2つの試験シナリオが
同じサブネット（例: `10.1.12.0/30`）を使い回すと、リンクしていない装置同士が
ネイバーとして見えてしまう。バグではないが、試験構築時は必ずユニークな
サブネットを使うこと。

## 確認済みで問題なし

- cisco ↔ catalyst RIPv2: ネイバー確立・双方向経路交換・ping疎通 OK
- cisco ↔ catalyst eBGP: Established・経路交換・ping疎通 OK
- cisco ↔ catalyst OSPF: サブネットを分離すればFull到達 OK（router-id表示を除く）

## 実装済み（この回で追加）

### EIGRP（Cisco / Catalyst / Nexus）
`engine/protocols.py` の `EigrpEngine`。回帰試験は `tests/test_eigrp.py`。

- `router eigrp <asn>` / `network <ip> [wildcard]` / `no router eigrp <asn>`
- `eigrp router-id` / `variance` / `metric weights` / `passive-interface`
- Hello 5秒・Hold 15秒でのネイバー確立と失効
- AS番号不一致・Kパラメータ不一致でネイバーが上がらないこと
  （`metric weights` の変更は実機同様に既存ネイバーをリセットする）
- クラシックメトリック `256 × (10^7/帯域kbps + 遅延/10usec)`
- FD/RD を持つトポロジテーブルとフィージブルサクセサ判定
- `show ip eigrp neighbors` / `topology` / `interfaces`
- `show ip route` に AD 90 の `D`、再配信由来は AD 170 の `D EX`
- Nexus は `feature eigrp` が先に必要

Si-R / SR-S / APRESIA は実機がEIGRP非対応のため対象外。

### Si-R のリンク断/復旧（ether use）
Si-R には Cisco の `shutdown` が無く、実機のコマンドは
`ether <group> <port> use <on|off>`（コマンドリファレンス 4.1.2）。
回帰試験は `tests/test_sir_ether_use.py`。

- `ether <g> <p> use on|off`（`1,3-4` のような複数指定にも対応）
- ether → VLAN → lan の2段（`ether vlan untag <vid>` / `lan <n> vlan <vid>`）を
  たどって、落ちる lan インタフェースを決める
- `ether <g> <p> snmp trap linkdown|linkup <enable|disable>` によるトラップ抑止
- `show ether` / `show ether brief` / `show ether statistics` / `clear ether statistics`
  を実機の出力形式に修正（以前は固定文字列を返しており、`use off` の結果が
  表示に反映されなかった）

あわせて、Si-R/SR-S で `ether` が略称展開により `etherchannel` へ化けるバグを修正。
これが原因で `show ether brief` などが正規表現にマッチしていなかった。

### サブインタフェース（802.1Q）
回帰試験は `tests/test_subinterface_dot1q.py`。

- `encapsulation dot1Q <vlan> [native]` / `no encapsulation`
  （物理インタフェース上では実機同様に拒否）
- `show running-config` に ip address より前で出力
- `show vlans`（IOSルータの802.1Qサブインタフェース一覧）
- 両端のタグ番号が食い違う場合は同一セグメントとみなさない
  （送信側・受信側の両方で判定。片側だけタグ付きの構成は物理側の設定
  依存のため判定しない）

## 未着手

- Si-R を含む組み合わせ（Si-R↔Catalyst, Si-R↔Cisco）でのRIP/OSPF/BGP試験は未実施。
- Si-R の `show snmp` 出力形式は実機で未確認（現状はベストエフォート）。
- ruff の F841（未使用変数）34件はCIのブロック対象から除外中。
