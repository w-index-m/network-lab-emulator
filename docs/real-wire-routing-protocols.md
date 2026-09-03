# 実ワイヤプロトコル RIP/BGP/OSPF リスナー

`engine/real_rip_agent.py` / `engine/real_bgp_agent.py` / `engine/real_ospf_agent.py`

## これは何か

`tools/route_injector_cli.py`（RIP/BGP）や
`tools/route_injector/network_route_injector.py` の `OSPFNeighborFaker`
（OSPF）といった、**本物のワイヤプロトコルを話す外部ツール**から、この
エミュレーターの各装置に対して実際にRIP/BGP/OSPFのパケットを送り、
ネイバー確立・経路交換ができるようにする実装。

## 背景（根本原因）

このエミュレーターの `engine/protocols.py` にある `RipEngine` /
`BgpEngine` / `OspfEngine` は、**内部Pythonオブジェクト間のシミュレー
ション**でしかなく、実際のUDP/TCP/raw IPソケットを一切listenしていな
かった。そのため、CLIで `router ospf` 等を設定しても、それは
`vnet.links` で直接つながっている**エミュレーター内の他装置**との
シミュレーションにしか反映されず、外部から本物のOSPF/RIP/BGPパケット
を送っても何も応答しなかった（`Connection refused`、無応答、経路が
反映されない、など）。実際に3プロトコルとも外部ツールから接続を試み、
すべてNGであることを確認した上で、以下の実リスナーを新規実装した。

## 実装方針（3プロトコル共通）

`engine/snmp_udp_agent.py`（実SNMPエージェント）で確立済みのパターンを
踏襲：

1. 各装置の management IP を `ip addr add <IP>/32 dev lo scope host` で
   ループバックにエイリアス登録する（同一プロセス内で複数装置を別IP
   として区別するため）。
2. そのIP宛の実ソケット（UDP/TCP/raw）で待ち受ける。
3. 受信した本物のパケットをパースし、`engine/protocols.py` の内部エン
   ジン（`RipEngine` / `BgpEngine` / `OspfEngine`）が使うデータ構造へ
   そのまま書き込む。こうすることで `show ip route` 等の既存表示ロジ
   ックをそのまま再利用できる。

### RIP (`engine/real_rip_agent.py`)

- UDP/520 で各装置ごとに待ち受け（`start_all_rip_agents`、
  `app.py` の `lifespan` で起動時に全装置分開始）。
- 受信したRIPv1/v2 Responseパケットをパースし、
  `RipEngine.receive(device_id, msg)` にそのまま渡す（内部の
  Bellman-Ford更新、`distribute-list in`、ポイズンリバース等の既存
  ロジックをそのまま再利用）。
- 送信元は外部ホストで内部の `device_id` を持たないため、
  `msg['src_id']` にはピアIPアドレス文字列をそのまま使う。

### BGP (`engine/real_bgp_agent.py`)

- TCP/179 で各装置ごとにパッシブオープン（`start_all_bgp_agents`）。
- OPEN受信→OPEN+KEEPALIVE応答→KEEPALIVE受信でEstablished、という
  最小限のステートマシンを実装（`route_injector_cli.py` の
  `BGPSpeaker` と対になる受信側）。
- UPDATE受信時はNLRI/AS_PATH/NEXT_HOP/MED/LOCAL_PREFをパースし、
  `bgp_engine.nodes[device_id]['loc_rib']` に直接追記する。

  **注意（ハマった点）**: `BgpEngine` の内部実装
  （`engine/protocols.py` 2281行目）は `loc_rib` を
  **`BgpRoute` データクラスではなく `dict` のリスト**として扱ってい
  る（`show ip route` 側は `r['prefix']` のようにキーアクセスする）。
  最初 `BgpRoute` インスタンスを追加してしまい
  `TypeError: 'BgpRoute' object is not subscriptable` で
  `show ip route` がクラッシュした。既存コードのデータ構造に合わせて
  dict で追加するよう修正済み。

### OSPF (`engine/real_ospf_agent.py`)

- `tools/route_injector/network_route_injector.py` の
  `OSPFNeighborFaker`（scapyベース、P2PのHello→2-Way→ExStart→
  Exchange→Full のフルステートマシン。tkinter非依存）を
  `DeviceOspfResponder` としてそのまま装置側の「実OSPFプロセス」に
  再利用している。
  - `OSPFNeighborFaker` はGUIツール(`network_route_injector.py`)の
    一部としてTkinterと同じファイルにあるため、ヘッドレスに読み込む
    ために `sys.modules` へダミーの `tkinter` モジュール群を注入して
    からimportしている（GUIコード自体は使わないため実害なし）。
  - raw IP protocol 89 を `iface='lo'` で `scapy.sniff()` により
    listen（libpcapが必要。`apt-get install libpcap-dev libpcap0.8`
    済み）。
  - External-LSA (`OSPF_External_LSA`) を受信すると
    `ospf_engine.nodes[device_id]['_learned_external']` に書き込み、
    `ospf_engine._recalc_routes(device_id)` を呼んでSPF計算に反映。
- OSPFはRIP/BGPと異なり「有効な装置がある場合のみ」動く設計（実装が
  1インスタンス=1ネイバーのP2Pモデルのため、常時全装置分立ち上げる
  意味が薄い）。そのため:
  - アプリ起動時（`lifespan`）に、その時点で `router ospf` が有効な
    装置があれば `start_all_ospf_agents` で起動。
  - **さらに** `app.py` 内の3箇所の `ospf_engine.start(...)` 呼び出し
    直後（CLIで `router ospf` / `network ... area ...` を設定した
    瞬間）に `ensure_ospf_agent(device_id, device_sessions, ospf_engine)`
    を呼び、動的に実リスナーを追従起動するようにした（アプリ起動後
    にCLIでOSPFを有効化した装置にも対応するため）。

## 動作確認（2026-09-03、Catalyst 1台 + `route_injector_cli.py` / `OSPFNeighborFaker`）

以下は実際にこの環境で確認した手順。**IPアドレスやポートは今回の検証
時点のものであり、装置構成やトポロジーが変わっても、同じ手順（CLIで
`router rip`/`router ospf`/`router bgp` を設定し、外部ツールから同じ
オプションで実パケットを送る）を踏めば再現できる**。

### 事前準備

```bash
# rootで実行（/32ループバックエイリアス追加・raw socket使用のため）
apt-get install -y libpcap-dev libpcap0.8   # OSPFのsniff()に必要（済み）

# アプリ起動（実RIP/BGP/OSPFリスナーがlifespanで自動起動する）
python3 app.py
```

### 装置側の設定（例: catalyst）

```
enable
configure terminal
router rip
 version 2
 network 10.9.9.0
exit
router ospf 1
 router-id 10.9.9.1
 network 10.9.9.0 0.0.0.255 area 0
exit
router bgp 65099
exit
exit
```

`router ospf` 設定直後、サーバーログに以下が出力され、実OSPFリスナー
が動的起動したことを確認できる:

```
[OSPF] catalyst (10.9.9.1) 実リスナーを動的起動しました (router_id=..., area=0.0.0.0)
```

### RIP（`route_injector_cli.py`）

```bash
python3 tools/route_injector_cli.py rip --dest 10.9.9.1 \
  --route 172.16.50.0/24:10.9.9.2:1 --version 2 --bind-ip <未使用のローカルIP>
```

`show ip route rip` で確認:

```
R     172.16.50.0/24 [120/2] via <bind-ip>, lan0
```

**ハマった点**: `route_injector_cli.py` はRIPの送信元ソケットを
`bind((bind_ip or "0.0.0.0", 520))` のように**送信側も520番ポートで
bind**する（RFC1058の慣習）。デフォルトの `--bind-ip 0.0.0.0` のままだ
と、実リスナー側が各装置のIPに対して個別に520番をbind済みのため
`OSError: [Errno 98] Address already in use` になる。ループバックに
別のIP（未使用のもの）をエイリアス登録し `--bind-ip` で明示的に指定
すれば回避できる:

```bash
ip addr add 127.0.0.5/32 dev lo scope host
python3 tools/route_injector_cli.py rip --dest 10.9.9.1 \
  --route 172.16.50.0/24:10.9.9.2:1 --version 2 --bind-ip 127.0.0.5
```

### BGP（`route_injector_cli.py`）

```bash
python3 tools/route_injector_cli.py bgp --peer 10.9.9.1 \
  --local-as 65001 --remote-as 65099 --router-id 10.9.9.100 \
  --route 172.16.60.0/24 --next-hop 10.9.9.100 --hold-seconds 8
```

ログで `Established` に到達し、UPDATE送信後 `show ip route` で確認:

```
B     172.16.60.0/24 [20/0] via 10.9.9.100, lan0
```

### OSPF（`OSPFNeighborFaker`、ヘッドレス実行）

`tools/route_injector/network_route_injector.py` はTkinter GUIツール
の一部だが、`OSPFNeighborFaker` クラス自体はTkinter非依存のため、
`sys.modules` にダミーのtkinterモジュール群を注入してヘッドレスに
importして直接使う（`engine/real_ospf_agent.py` と同じ手法）。

```python
faker = OSPFNeighborFaker(
    iface='lo', my_ip='10.9.9.50', router_id='10.9.9.50', area='0.0.0.0',
    mask='255.255.255.0', hello_interval=2, dead_interval=8, debug=True,
)
# sniff()をバックグラウンドスレッドで開始しつつHelloを送り続ける
...
if faker.state == faker.STATE_FULL:
    faker.originate_router_lsa(cost=10)
    faker.inject_external_route('192.168.200.0', '255.255.255.0', metric=20)
```

`Full` 到達後、`show ip route` で確認:

```
O     192.168.200.0/24 [110/20] via 10.9.9.50, lan0
```

**ハマった点（重要）**:

- OSPFの `my_ip` に使うIPは**必ず未使用のものを新規に用意する**こと。
  同じIPで複数回テストを実行すると、`DeviceOspfResponder`（サーバー
  側）はプロセスが生きている限り**1インスタンス=1ネイバー**の状態を
  保持し続けるため（`is_master` や `dbd_phase` 等がインスタンス変数）、
  古いテストプロセスとのDBDesc シーケンス番号が食い違ったまま新しい
  テストプロセスと通信しようとして ExStart で永久にスタックする
  （実機でも隣接ルータが再起動した直後、Dead Timerが切れるまで同様の
  ことが起こりうる）。
- DBDesc のMaster/Slaveネゴシエーションは、双方の `rxmt_interval`
  （デフォルト5秒）に依存した再送によって収束するため、**Hello交換
  開始からFull到達まで数十秒かかることがある**（今回の検証では最短
  約5秒、最長で約70秒かかるケースがあった）。短いタイムアウトで
  「NG」と判断しないよう注意。
- `show ip ospf neighbor` はこの実リスナー経由のネイバーを表示しない
  （`ospf_engine.nodes[...]['neighbors']` を更新していないため）。
  ただし学習した経路は正しく `_learned_external` 経由で
  `show ip route` に反映される。これは既知の制約（下記）。

## 既知の制約

- **OSPFはP2Pの1ネイバーのみ対応**。`OSPFNeighborFaker` の設計上、
  1つの `DeviceOspfResponder` インスタンスは同時に1つのネイバーしか
  扱えない（ブロードキャストセグメント上での複数ネイバー・DR/BDR
  選出には非対応）。RIP/BGPは複数ピアに対応できる設計（UDP/TCPの
  接続ごとに独立処理）。
- `show ip ospf neighbor` に実リスナー経由のネイバーは表示されない
  （経路自体は正しく学習・表示される）。
- 各装置の management IP が重複していると（`saved_config.json` に
  複数装置が同じIPを持つケースがある）、その装置分のリスナーは
  起動に失敗する（ログに `起動失敗: could not bind to local_addr`
  として出力される）。
- RIP/OSPF/BGPのプロセス有効化状態（`router rip` 等の設定）は
  `saved_config.json` に永続化されないため、アプリ再起動後は
  CLIで再設定する必要がある（`vnet.links` などのトポロジー情報とは
  異なる）。

### 装置非依存であることの確認（Si-R = `sir-a`）

上と同じ手順を `catalyst` の代わりに `sir-a`（Si-R, hostname
Router-A）に対して実行し、RIPが同様に動作することを確認した:

```
router rip
 version 2
 network 192.168.1.0
```

```bash
python3 tools/route_injector_cli.py rip --dest 192.168.1.1 \
  --route 172.16.70.0/24:192.168.1.2:1 --version 2 --bind-ip 127.0.0.5
```

`show ip route rip`:

```
R     172.16.70.0/24 [120/2] via 127.0.0.5, lan0
```

→ 実リスナーはCatalyst固有の実装ではなく、`device_sessions` の
全装置に対して汎用的に動作することを確認した。

### 既知のブロッカー: `apresia` 装置の management IP 重複

`saved_config.json` には同じ management IP を持つ装置が複数存在する
（例: `apresia`（hostname sw1, 192.168.10.1）と `srs`（hostname
Core-SW, 192.168.10.1）が重複、`sir`（hostname SiR-Router）と
`sir-a`（192.168.1.1）が重複）。これは今回の実リスナー実装以前から
存在する、ラボのfixture構成上の重複であり（実SNMPエージェントの
起動ログでも同じ重複により起動失敗が発生している）、今回の変更が
原因ではない。

この重複により、`apresia` 装置は実RIP/BGP/OSPFリスナーの起動に失敗
する（先に起動した `srs` が同じIP:ポートを占有するため）:

```
[RIP] apresia (192.168.10.1:520) 起動失敗: could not bind to local_addr ('192.168.10.1', 520)
```

Apresiaに対する実ワイヤプロトコル試験を行うには、まずこのIP重複を
解消する必要がある（`apresia` に別のmanagement IPを割り当てるか、
`srs` 側を変更する）。今回のセッションでは未対応。

## 未着手（今後の検討事項）

以下は今回のセッションで要望が挙がったが、**まだ着手していない、
規模の大きいスコープ**:

- Catalyst/Si-R/Apresia それぞれ3台を「central Ethernet」（共有
  ブロードキャストセグメント）に接続し、複数ネイバー・DR/BDR選出を
  含むOSPF/RIP/BGP試験を行う（現状の実装は前述の通りOSPFがP2P限定の
  ため、ブロードキャストセグメントでの複数ネイバー対応には設計変更
  が必要）。
- Si-R / Apresia 単体に対する、上記と同じ実ワイヤプロトコル試験
  （Catalystで確認した手順と同じ方法で、`_pick_management_ip` が
  各装置のmanagement IPを解決できることを前提に動くはずだが、実際に
  Si-R/Apresiaで動作確認はまだ行っていない）。
