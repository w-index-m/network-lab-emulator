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

### IPsec/IKE タイマー（Si-R / Cisco ルータ）
回帰試験は `tests/test_ipsec_dpd_timers.py`。

Si-R（コマンドリファレンス 10.2.62, 10.2.82〜10.2.84, 10.2.101で仕様確認）:
- `remote ap ike retry <time> <count>`（ネゴシエーション再送、既定10s/3回）
- `remote ap ike dpd use <on|off>`（DPD利用可否、既定off）
- `remote ap ike dpd idle <time>`（無通信監視時間、既定10s、5〜600s）
- `remote ap ike dpd retry <time> <count>`（DPD再送、既定1s/3回、1〜60s・1〜10回）
  再送時間×(再送回数+1) < 無通信監視時間 のマニュアル注記どおり相互検証する
- `remote ap sessionwatch interval <normal> <error> <timeout> [<retry>]`
- `ether use off`（リンクダウン）でDPDが有効なトンネルだけがdetecting状態に遷移し、
  無通信監視時間+再送時間×再送回数の経過後にdownと判定される。DPD無効なら
  実機同様に能動検知しない（次のネゴシエーションかSA有効期限切れまで見かけ上
  establishedのまま）
- 検知窓が満了する前にリンクが復旧すればトンネルは切断されない

Cisco:
- `crypto isakmp keepalive <interval> <retry>`（実IOSの範囲10-3600s/2-60sで検証）を
  実際のDPD検知窓として使う
- 検知窓をicmp_engineの共有属性からトンネルごとの値に変更
  （以前はkeepalive設定が異なる複数装置が同じグローバル値を取り合うクロストークがあった）
- `crypto map <name> interface <if>` と、実機で最も一般的な
  「interface配下で `crypto map <name>` とだけ書く」構文の両方でDPD登録が働くことを確認
  （後者は以前は一切登録されずshow crypto ipsec sa/DPDが常に空になっていた）
- crypto map名の大文字小文字がデータ構造間で食い違い、`crypto_map_interface`の
  適用先マップが見つからなくなるバグを修正
- WAN側インタフェースのIP特定が部分一致だったため、"GigabitEthernet0/0"が
  "GigabitEthernet0/0/0"の部分文字列として誤って一致するバグを修正（完全一致を優先）
- `show running-config`（app.pyの実際のCisco生成器）に crypto isakmp policy/key/
  keepalive/transform-set/crypto map の出力が丸ごと欠けていたため追加
  （`engine/rules.py`側には同等の出力コードが以前からあったが、実際のAPI経路では
  呼ばれない生成器で、設定しても running-config に一切反映されなかった）

### Si-R 手動鍵設定 IPsec（remote ap ipsec type manual）
回帰試験は `tests/test_ipsec_manual_key.py`。

IKE（自動鍵交換）とは別の、SPI・プロトコル・鍵を直接指定する方式
（コマンドリファレンス 10.2.27〜10.2.36）。ネゴシエーションを介さず、
両側の send/receive 設定が噛み合った時点でSAが張られる。

- `remote ap ipsec type manual` / `ipsec send|receive spi <hex>`（100〜ffffffff）/
  `protocol <none|esp|ah>` / `range <src>/<mask> <dst>/<mask>` /
  `encrypt <algo> [<hex|text> <key>]` / `auth <algo> [<hex|text> <key>]`
- SA作成可否はマニュアル10.2.29の表どおり判定する:
  `protocol=ah` は auth必須（encryptの有無は無関係）、
  `protocol=esp` は encrypt必須（authの有無は無関係）
- 「暗号化しないトンネル」はここで組める:
  - `protocol=ah` + `auth=hmac-sha256` 等（認証のみ、暗号化なし）
  - `protocol=esp` + `encrypt=null`（ESP-NULL。フレーミングはあるが機密性なし）
- `encrypt`/`auth` が `none`/`null` の場合は鍵を指定できない（マニュアル注記どおり拒否）
- SPI・プロトコル・鍵材料のいずれかが両側で食い違えばSAは張られず`Waiting`のまま
- IKE用トンネル（`ipsec type`未指定）とは判定経路が完全に分離しており、
  同じトンネル辞書を共有していても混ざらないことを確認

## 未着手

- Si-R を含む組み合わせ（Si-R↔Catalyst, Si-R↔Cisco）でのRIP/OSPF/BGP試験は未実施。
- Si-R の `show snmp` 出力形式は実機で未確認（現状はベストエフォート）。
- ike retry（ネゴシエーション自体の再送）は設定値の保持とrunning-config反映まで。
  negotiate_ipsec()は同期的に一発で成否を決めるため、実際に初回再送時間×再送回数
  だけ待って失敗を確定させる、という時間経過そのものはエミュレートしていない。
- 手動鍵設定のDPD/sessionwatchとの連動は未確認（IKE用のsir_link_down等は
  ipsec_tunnelsのstatusを直接見るため手動鍵トンネルにも一応効くはずだが、
  専用のテストはまだ無い）。
- ruff の F841（未使用変数）34件はCIのブロック対象から除外中。
