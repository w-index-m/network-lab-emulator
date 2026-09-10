# OSPF障害切替 + フローティングスタティック 検証記録

2台のCatalystを**主回線(OSPF)**と**予備回線(フローティングスタティック)**で
冗長化し、主回線のshutdownでバックアップへ切り替わることを確認した記録。

この検証で **実機と挙動が食い違う不具合が6件** 見つかったため、
その原因と修正内容もあわせて残す（同種の障害試験を組むときの注意点）。

- 実装: `engine/real_ospf_agent.py` / `engine/protocols.py` / `app.py`
- テスト: `tests/test_ospf_failover.py`
- 再現スクリプト: 本ドキュメント末尾

## 構成

```
        Gi1/0/1  10.90.1.0/24  ← 主経路（OSPF area 0）
FS1 ═══════════════════════════ FS2
        Gi1/0/2  10.90.2.0/24  ← 予備経路（OSPFに載せない）

FS1: Loopback0 172.31.1.1/32    FS2: Loopback0 172.31.2.2/32
```

FS1側の設定（要点だけ）:

```
interface Loopback0
 ip address 172.31.1.1 255.255.255.255
interface GigabitEthernet1/0/1
 no switchport
 ip address 10.90.1.1 255.255.255.0
 no shutdown
interface GigabitEthernet1/0/2
 no switchport
 ip address 10.90.2.1 255.255.255.0
 no shutdown
!
router ospf 1
 router-id 172.31.1.1
 network 10.90.1.0 0.0.0.255 area 0
 network 172.31.1.1 0.0.0.0 area 0
!
! フローティングスタティック: AD 210 > OSPF 110 なので平常時は浮上しない
ip route 172.31.2.2 255.255.255.255 10.90.2.2 210
```

**予備回線 10.90.2.0/24 を `network` に入れないのが肝。**
入れてしまうと予備側でもOSPF隣接が張られ、切替の検証にならない。

## 検証結果（修正後の実際の出力）

### ① 平常時 — OSPF(AD110)が優先

```
FS1# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
172.31.2.2      1     Full/DROTHER    00:00:35    10.90.1.2       GigabitEthernet1/0/1

FS1# show ip route
O        172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1

FS1# show ip route 172.31.2.2
Routing entry for 172.31.2.2/32
  Known via "ospf 1", distance 110, metric 20, type intra area
  Last update from 10.90.1.2 on GigabitEthernet1/0/1
  Routing Descriptor Blocks:
  * 10.90.1.2, from 10.90.1.2, via GigabitEthernet1/0/1
      Route metric is 20, traffic share count is 1
```

フローティング側は待機状態:

```
FS1# show ip route static
Destination/Mask     Next-Hop          AD    Status      Type
172.31.2.2/32        10.90.2.2         210   active      floating-backup
```

### ② 障害発生 — `interface Gi1/0/1` / `shutdown`

```
FS1# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
(No neighbors)

FS1# show ip route
S        172.31.2.2/32 [210/0] via 10.90.2.2, GigabitEthernet1/0/2

FS1# show ip route 172.31.2.2
Routing entry for 172.31.2.2/32
  Known via static, distance 210, metric 0
  Routing Descriptor Blocks:
  * 10.90.2.2, via GigabitEthernet1/0/2
      Route metric is 0, traffic share count is 1
```

隣接が消え、OSPF経路が撤回され、AD210のスタティックが浮上している。
主回線の直結経路(C/L 10.90.1.0/24)も同時に消えることも確認。

### ③ 復旧 — `no shutdown`

```
FS1# show ip ospf neighbor
172.31.2.2      1     Full/DROTHER    00:00:20    10.90.1.2       GigabitEthernet1/0/1

FS1# show ip route
O        172.31.2.2/32 [110/20] via 10.90.1.2, GigabitEthernet1/0/1
```

AD110に戻る。

## この検証で見つかった不具合と修正（重要）

障害試験をやるまで表面化しなかったものばかり。**静的な収束状態が
正しくても、再収束が正しいとは限らない**という典型例。

| # | 症状 | 原因 | 修正 |
|---|---|---|---|
| A | shutdownしても隣接がFullのまま | `_flap_interface_down` がシミュレーション側 `ospf_engine` にしか通知せず、実際に隣接を張っている実OSPFリスナーは動き続けていた | `app.py` で該当IFのIPを持つリスナーを `stop_ospf_agent()` で停止。`no shutdown` で `ensure_ospf_agent()` により再開 |
| B | 対向が無言でも隣接が落ちない | 流用元の `OSPFNeighborFaker` は経路注入ツールで、`dead_interval` をHelloに載せるだけで**受信側のDeadタイマーを持たない** | `DeviceOspfResponder` に `_dead_loop` を追加。Hello受信時刻を記録し、`dead_interval` 超過で `expire_neighbor()` |
| C | 停止したはずのリスナーが復活する | scapyの `sniff` は `stop()` 後も受信済みパケットを処理し続け、`DOWN→INIT→…` と再遷移していた | `_on_packet` 冒頭で `stop_event` を見て即return。`stop_ospf_agent` は**撤回より先に停止フラグを立てる** |
| D | OSPFに入れていない予備回線で隣接が張られる | シミュレーション側OSPFが `network` 文を無視し、`broadcast_to_neighbors` で全リンクにHelloを流していた | `ospf_enabled_ifaces()` / `_non_ospf_peers()` を追加し、送信時 `exclude`・受信時ドロップ |
| E | 主回線shutdown後もOSPF経路が残る | RIBのshutdownフィルタが `iface` を見るが、動的経路は next_hop がrouter-idで **iface が空**のまま素通りしていた | フィルタ内で `resolve_learned_next_hop()` → `_iface_for_nexthop()` して出口IFを解決してから判定 |
| F | `O 10.90.1.0/24 via 172.31.1.1, Loopback0` という実機に無い経路 | 自分のLSA由来で「自分の直結NWへ自分経由」の経路が生まれ、直結経路がshutdownで消えた瞬間に表面化 | 次ホップが自分のrouter-idのOSPF経路はRIBに入れない |

あわせて直した表示系:

- `show ip ospf neighbor` の Interface 列が `lo`（全装置が `lo` を共有して
  待ち受けているため）→ 自IPからIF名を逆引きして `GigabitEthernet1/0/1` に
- `show ip route <A.B.C.D>` が引数を無視して全テーブルを返していた
  → `Routing entry for ...` の詳細ブロックを返す。
  `show ip route {connected|static|ospf|rip|bgp|eigrp}` の絞り込みも追加
- `no router ospf <n>` がどのハンドラにも一致せず**完全に無視**されていた
  （`show ip protocols` にプロセスが残り続けた）→ プロセス停止＋経路撤回
- running-config の `network` 行のワイルドカードが常に `0.0.0.255` 固定
  → 実際のプレフィックス長から復元（`network <lo> 0.0.0.0` が壊れていた）

## 同種の試験を組むときの注意

1. **OSPFには2つの実装がある。** `engine/protocols.py` の
   `OspfEngine`（vnet上のシミュレーション）と、
   `engine/real_ospf_agent.py`（scapyの実パケット、`lo`上で全装置が共存）。
   `show ip ospf neighbor` に出るのは後者が同期した内容なので、
   前者だけを操作しても表示は変わらない。
2. **並列リンクは `vnet.interface_links` では表現できない。**
   `{peer -> iface}` と1本しか持てないため、同一ペア間に2本張ると
   後勝ちになる。全部を持っているのは `vnet.link_ifaces`。
3. **装置IDとIPの重複に注意。** `saved_config.json` に前回の装置が
   残るので、試験前に使うIDとIPレンジが空いているか確認する
   （`curl -s localhost:8000/api/status`）。
4. `/api/cli` のレスポンスキーは **`output`**（`response` ではない）。
   `/api/link` のパラメータは **`iface_a` / `iface_b`**。
5. 認証を切って起動するには `NETLAB_AUTH_DISABLE=1`。

## 再現スクリプト

```python
import json, time, urllib.request
BASE = "http://127.0.0.1:8000"

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def cli(dev, cmd):
    return post("/api/cli", {"device_id": dev, "command": cmd}).get("output", "")

for d in ("fs1", "fs2"):
    post("/api/device", {"id": d, "type": "catalyst", "hostname": d.upper()})
for ifn in ("GigabitEthernet1/0/1", "GigabitEthernet1/0/2"):
    post("/api/link", {"a": "fs1", "b": "fs2", "iface_a": ifn, "iface_b": ifn})

for cmd in """configure terminal
interface Loopback0|ip address 172.31.1.1 255.255.255.255|exit
interface GigabitEthernet1/0/1|no switchport|ip address 10.90.1.1 255.255.255.0|no shutdown|exit
interface GigabitEthernet1/0/2|no switchport|ip address 10.90.2.1 255.255.255.0|no shutdown|exit
router ospf 1|router-id 172.31.1.1|network 10.90.1.0 0.0.0.255 area 0|network 172.31.1.1 0.0.0.0 area 0|exit
ip route 172.31.2.2 255.255.255.255 10.90.2.2 210|end""".replace("\n", "|").split("|"):
    cli("fs1", cmd)
# fs2 は 10.90.1.2 / 10.90.2.2 / 172.31.2.2 で同様に投入

time.sleep(45)                       # 隣接確立を待つ
print(cli("fs1", "show ip route"))
cli("fs1", "configure terminal"); cli("fs1", "interface GigabitEthernet1/0/1")
cli("fs1", "shutdown"); cli("fs1", "end")
time.sleep(55)                       # Dead(40秒)＋余裕
print(cli("fs1", "show ip route"))   # ← S [210/0] に切り替わる
```

待ち時間は Hello 10秒 / Dead 40秒が既定のため、**障害後は最低50秒**見ること。

## 関連ドキュメント

- `docs/sir-catalyst-rip-ospf-bgp-stp-mpls-regression.md` — 他プロトコルの検証記録
- `docs/netconf-catalyst.md` — NETCONF実装
