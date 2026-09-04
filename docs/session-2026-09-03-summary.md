# 作業ログ 2026-09-03 — 実ワイヤプロトコル対応と「表示と実態の食い違い」の一掃

この日の作業をまとめたもの。個別の手順書は各ドキュメントを参照。

| ドキュメント | 内容 |
|---|---|
| `docs/real-wire-routing-protocols.md` | 実RIP/BGP/OSPFリスナーの設計と全ベンダー検証結果 |
| `docs/ospf-route-injection-howto.md` | RouteInjectorからOSPF経路を注入する再現手順 |
| `docs/cdp-topology-consistency.md` | CDP/LLDPと実トポロジーの不整合修正 |
| `docs/vrrp-hsrp.md` | VRRP/HSRPの試験と修正（4バグ） |
| `docs/nexus-vpc-howto.md` | Nexus vPCの構築手順とステータス確認 |

## 1. 実ワイヤプロトコル RIP/BGP/OSPF リスナーの新規実装

### 背景（根本原因）

`engine/protocols.py` の `RipEngine` / `BgpEngine` / `OspfEngine` は
**内部Pythonオブジェクト間のシミュレーション**でしかなく、実際の
UDP/TCP/raw IPソケットを一切listenしていなかった。CLIで `router ospf`
等を設定しても、`vnet.links` で直結された**エミュレーター内の装置同士**
でしか機能せず、外部ツールから本物のパケットを送っても無応答だった。

3プロトコルとも外部ツールから接続を試み、すべてNGであることを確認した
上で実リスナーを実装した。

### 実装

`engine/snmp_udp_agent.py`（実SNMPエージェント）のパターンを踏襲し、
各装置のmanagement IPを `/32` ループバックエイリアスとして追加して
そのIPで待ち受ける。

| ファイル | プロトコル | 内部エンジンへの橋渡し |
|---|---|---|
| `engine/real_rip_agent.py` | UDP/520 | `RipEngine.receive()` にそのまま渡す |
| `engine/real_bgp_agent.py` | TCP/179 パッシブ | `sessions` / `rib_in` / `loc_rib` へ反映 |
| `engine/real_ospf_agent.py` | raw IP proto 89 | `_learned_external` → `_recalc_routes()` |

OSPFは `tools/route_injector/network_route_injector.py` の
`OSPFNeighborFaker`（scapyベースのフルステートマシン）を装置側の
「実OSPFプロセス」としてそのまま再利用している。

### 検証結果（全ベンダー × 3プロトコル）

| 装置 | RIP | BGP | OSPF |
|---|---|---|---|
| Catalyst | OK | OK | OK |
| Cisco | OK | OK | OK |
| Si-R | OK | OK | OK |
| Apresia | OK | OK | OK |

## 2. 「表示と実態の食い違い」の一掃

このセッションで繰り返し現れた最大のテーマ。**CLIの表示は正常なのに
実際には何も動いていない**ため、切り分けが極めて困難になる類のバグ。

| 対象 | 症状 | 状態 |
|---|---|---|
| CDP/LLDP | 実在しない隣接装置を表示（`vnet.links` と無関係の固定データ） | 修正 |
| EIGRP | 未設定なのに幽霊ネイバー `10.0.0.2` を表示 | 修正 |
| VRRP/HSRP | Helloが受信側に渡らず両系Master/Active（スプリットブレイン） | 修正 |
| OSPF | 経路は学習されるのに `show ip ospf neighbor` が `(No neighbors)` | 修正 |
| BGP | 経路は入るのに `show ip bgp summary` にネイバー行が出ない | 修正 |
| BGP | `show ip bgp neighbors` 自体が未実装（`% Invalid input`） | 実装 |
| RIP | — | 元から正常 |

### CDP/LLDP（`docs/cdp-topology-consistency.md`）

`DeviceState.__init__` が全装置に固定のサンプルネイバー
（`Core-SW` / `GW-Router`）を持たせており、さらに実トポロジーから
作り直す `_rebuild_all_neighbors()` が起動時に呼ばれていなかった。
CDP上は隣接に見えるのにOSPFのHelloが1つも届かない、という状態だった。

### VRRP/HSRP（`docs/vrrp-hsrp.md`）

テストが1件も無く、4つのバグが埋まっていた。特に
`VirtualNetwork.send_to()` のディスパッチ表に `vrrp_advert` /
`hsrp_hello` が無く、Helloを撒いても受信側エンジンに渡らず捨てられて
いた。また `broadcast_to_neighbors()` が送信側のdownしか見ておらず、
自分のIFをshutdownした装置が対向のHelloを受信し続けていた
（`_edge_up()` を使うよう修正）。

## 3. その他の修正

### RIPv1 のクラスフルマスク

RIPv1にはマスク欄が無い（常に0）のに `prefix = 0` を返しており、
**すべてのRIPv1経路が `/0`（デフォルトルート同然）として学習**されて
いた。RFC 1058 どおりアドレスクラスから導出するよう修正。

| ネットワーク | クラス | 修正後 |
|---|---|---|
| `10.1.0.0` | A | `/8` |
| `172.16.80.0` | B | `/16` |
| `192.168.5.0` | C | `/24` |

### syslog / SNMP trap

link down時の送信自体は**元から動作していた**（実RFC3164 syslogと
実v2c trapがワイヤに出ることを受信サーバを立てて確認）。ただし
検証中に2つの制限が判明したので修正した。

- `snmp-server host` に `udp-port` 構文が無く、常に162番固定だった
  （root権限なしにtrap受信の検証ができない）
- `logging host` / `snmp-server host` は同じホストの再設定を無視して
  いたため、一度誤ったポートで登録すると訂正できなかった

### OSPFマルチエリアテストのフレーキー解消

`test_multiple_backbone_routers_all_learn_other_areas` が固定の
`asyncio.sleep(5)` で4台の収束を待っており、**5回に1回程度ランダムに
失敗**していた（私の変更前から存在）。収束をポーリングする方式に変更し、
8回連続パス・実行時間も15秒→9秒に短縮。

## 4. Nexus vPC（`docs/nexus-vpc-howto.md`）

テストが1件も無かったので、2台構成での構築・ロール選出・ステータス確認・
障害シミュレーションを実施し、`tests/test_vpc.py`（7件）を追加した。
既存実装は正しく動作しており、バグは見つからなかった。

- role priority が**小さい方が primary**（HSRP等と逆）
- 1台だけでは `pending` のまま上がらない（正しい挙動）
- `vpc peer failure` / `vpc peer-link failure` で障害を再現できる

## 5. 追加したテスト

| ファイル | 件数 | 内容 |
|---|---|---|
| `tests/test_real_routing_listeners.py` | 9 | RIP/BGPパケットのパース、RIPv1クラスフル |
| `tests/test_real_routing_integration.py` | 4 | 実ソケットでの結合テスト（OSPFはroot必要でskip可） |
| `tests/test_cdp_topology_consistency.py` | 5 | CDP整合性、EIGRP幽霊ネイバー |
| `tests/test_vrrp_hsrp.py` | 7 | VRRP/HSRP選出・対向表示・フェイルオーバー |
| `tests/test_vpc.py` | 7 | vPCロール選出・メンバー・障害 |
| `tests/test_link_trap_syslog.py` | +2 | trapポート指定、ホスト再設定の上書き |

全体テスト: **316 passed, 29 skipped**（修正前から失敗0、フレーキーも解消）

## 6. この環境での運用上の注意

- **`app.py` の再起動でプロトコル設定は消える**。`saved_config.json` に
  永続化されるのは装置とリンク（トポロジー）だけで、`router ospf` 等の
  プロトコル設定はメモリ上のみ。試験のたびにCLIで再投入が必要。
- **`saved_config.json` を直接編集するときは必ず `app.py` を完全停止
  してから**。シャットダウン時に `_save_config()` がメモリ上の状態を
  書き戻すため、稼働中の編集は上書きで消える。
- **OSPFの試験は毎回新しい送信元IPを使う**。装置側のOSPFレスポンダは
  1インスタンス＝1ネイバーの状態を保持するため、同じIPで繰り返すと
  DDシーケンス番号が食い違ってExStartでスタックする。
- **`% Invalid command` が出ても設定は入っていることがある**。表示を
  出しているルールベースCLI（`engine/rules.py`）が構文を知らないだけで、
  エンジンへの反映は `app.py` 側のハンドラが先に処理している場合がある
  （Apresiaの `router rip`、Nexusの `vpc domain` など）。`show` コマンドで
  実際の状態を確認すること。
