# Catalyst ↔ RouteInjector で OSPF 経路を注入する手順

外部ツール（RouteInjector = `tools/route_injector/network_route_injector.py`
の `OSPFNeighborFaker`）から、エミュレーター内の装置へ**本物のOSPF
パケット**でネイバーを張り、External-LSA で経路を注入する手順。

実装の詳細は `docs/real-wire-routing-protocols.md` を参照。
ここでは**次回そのまま再実行できる手順**をまとめる。

## 前提

- root で実行すること（raw socket と `/32` ループバックエイリアスのため）
- `libpcap` が必要（`apt-get install -y libpcap-dev libpcap0.8`。導入済み）
- アプリ起動: `python3 app.py`

## 1. 装置側の設定

**IPアドレスと同じサブネットになるよう注意する**（後述のハマりどころ）。

```
enable
configure terminal
interface GigabitEthernet1/0/1
 no shutdown
 ip address 10.9.9.1 255.255.255.0
exit
router ospf 1
 network 10.9.9.0 0.0.0.255 area 0
exit
exit
```

設定直後、サーバーログに実OSPFリスナーの動的起動が出る:

```
[OSPF] catalyst (10.9.9.1) 実リスナーを動的起動しました (router_id=..., area=0.0.0.0)
```

## 2. RouteInjector 側（ヘッドレス実行スクリプト）

注入元のIPをループバックに追加してから実行する。
**毎回未使用のIPを使うこと**（後述）。

```bash
ip addr add 10.9.9.77/32 dev lo scope host
```

```python
# /tmp/ospf_demo.py
import sys, types

# network_route_injector.py はGUIツールと同じファイルにあるため、
# tkinterのダミーを差し込んでヘッドレスにimportする
# （OSPFNeighborFakerクラス自体はtkinter非依存）
class _D(types.ModuleType):
    def __getattr__(self, n): return type(n, (object,), {})
for m in ('tkinter.ttk', 'tkinter.scrolledtext', 'tkinter.messagebox',
          'tkinter.font', 'tkinter.filedialog', 'tkinter'):
    mod = _D(m)
    if m == 'tkinter':
        for sub in ('ttk', 'messagebox', 'scrolledtext', 'filedialog'):
            setattr(mod, sub, sys.modules.get(f'tkinter.{sub}'))
    sys.modules[m] = mod

sys.path.insert(0, '/home/user/network-lab-emulator/tools/route_injector')
from network_route_injector import OSPFNeighborFaker
import time

f = OSPFNeighborFaker(iface='lo', my_ip='10.9.9.77', router_id='10.9.9.77',
                      area='0.0.0.0', mask='255.255.255.0',
                      hello_interval=2, dead_interval=8, rxmt_interval=3,
                      debug=True, on_log=print)
f.start()                                  # sniff / hello / rxmt の3スレッドを起動
if f.full_event.wait(timeout=60):
    print(">>> ネイバー確立(FULL)")
    f.originate_router_lsa(cost=10)        # 自分のRouter-LSA（これが無いと経路が入らない）
    time.sleep(1)
    for net in ('172.31.10.0', '172.31.20.0', '172.31.30.0'):
        f.inject_external_route(net, '255.255.255.0', metric=20, tag=0)
        time.sleep(0.5)
    time.sleep(45)                         # 確認する間ネイバーを維持する
else:
    print("FULL到達せず:", f.state)
f.stop()
```

```bash
nohup python3 /tmp/ospf_demo.py > /tmp/ospf_demo.log 2>&1 & disown
sleep 20
grep -E "FULL|inject" /tmp/ospf_demo.log
```

## 3. 確認結果（2026-09-03 実行）

### inject 前

```
catalyst# show ip route
Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP
       * - candidate default

S     10.9.9.0/24 [0/0] via 0.0.0.0,
S     192.168.10.0/24 [0/0] via 0.0.0.0,

catalyst# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
(No neighbors)
```

### inject 後

```
catalyst# show ip route
Codes: C - connected, S - static, R - RIP, O - OSPF, B - BGP
       * - candidate default

S     10.9.9.0/24 [0/0] via 0.0.0.0,
S     192.168.10.0/24 [0/0] via 0.0.0.0,
O     172.31.10.0/24 [110/20] via 10.9.9.77, lan0
O     172.31.20.0/24 [110/20] via 10.9.9.77, lan0
O     172.31.30.0/24 [110/20] via 10.9.9.77, lan0

catalyst# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.9.9.77       1     Full/DROTHER    00:00:13    10.9.9.77       lo
```

注入した3経路が `O ... [110/20]` として学習され、ネイバーも `Full` で
表示される。

## ハマりどころ（実際に踏んだもの）

### `f.start()` を必ず使う

`_send_hello()` を自前のループで回すと、**DBDescの再送スレッド
(`_rxmt_loop`) が起動しない**。OSPFのMaster/Slaveネゴシエーションは
再送で収束する設計なので、DBDescを一度でも取りこぼすと ExStart から
永久に進まなくなる。`start()` は sniff / hello / rxmt の3スレッドを
正しく起動し、`full_event.wait(timeout=...)` で待てる。
正しく使えば **1〜2秒でFullに到達する**。

### 注入元IPは毎回新しいものを使う

装置側の `DeviceOspfResponder` は**1インスタンス＝1ネイバー**の
状態（`is_master`、`dbd_phase` など）をプロセスが生きている限り保持する。
同じIPでテストを繰り返すと、前回のDDシーケンス番号と食い違って
ExStartのままスタックする。`10.9.9.77` → `10.9.9.78` のように変えるか、
アプリを再起動する。

### VIP/ネットワークのサブネットを合わせる

`ip address 10.9.9.1 255.255.255.252`（/30）のような狭いマスクだと、
注入元に使える範囲が `.1〜.2` しか無い。上の手順では `/24` にしている。

### `originate_router_lsa()` を忘れない

自分（ASBR）へのRouter-LSAが無いと、相手のSPF計算で自分への経路が
解決できず、External-LSAで注入した経路がルーティングテーブルに
反映されない。

## RIP / BGP の場合

同じ装置に対して `tools/route_injector_cli.py` でRIP/BGPも注入できる。

```bash
# RIP: 送信元も520番をbindするため、未使用IPを --bind-ip で明示する
ip addr add 127.0.0.5/32 dev lo scope host
python3 tools/route_injector_cli.py rip --dest 10.9.9.1 \
  --route 172.16.50.0/24:10.9.9.2:1 --version 2 --bind-ip 127.0.0.5

# BGP
python3 tools/route_injector_cli.py bgp --peer 10.9.9.1 \
  --local-as 65001 --remote-as 65099 --router-id 10.9.9.100 \
  --route 172.16.60.0/24 --next-hop 10.9.9.100 --hold-seconds 8
```

対象装置側で `router rip` / `router bgp <AS>` を設定しておくこと。
Catalyst / Cisco / Si-R / Apresia の4機種で確認済み
（`docs/real-wire-routing-protocols.md` の表を参照）。

## 自動テスト

```bash
# RIP/BGP: root不要・数秒
pytest tests/test_real_routing_integration.py -v

# OSPF: raw socketが要るので既定ではスキップ。有効化して実行する
NETLAB_OSPF_WIRE_TEST=1 pytest tests/test_real_routing_integration.py -v
```

## Si-R / Nexus での確認（2026-09-03）

Catalystと同じ手順を Si-R (`sir-a`) と Nexus (`nexus`) でも実施し、
4件の食い違いを修正した。

### 装置側の設定

```
# Si-R
configure
lan 0 ip address 10.20.20.1/24 1
ospf use on
ospf area 0.0.0.0
lan 0 ip ospf use on
save

# Nexus
configure terminal
interface GigabitEthernet0/0/1
 no shutdown
 ip address 10.30.30.1 255.255.255.0
exit
router ospf 1
 network 10.30.30.0 0.0.0.255 area 0
end
```

### 結果

```
sir-a# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.20.20.77     1     Full/DROTHER    00:00:30    10.20.20.77     lo

sir-a# show ip route
O        172.31.10.0/24 [110/20] via 10.20.20.77, lan0
O        172.31.20.0/24 [110/20] via 10.20.20.77, lan0

nexus# show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.30.30.77     1     Full/DROTHER    00:00:25    10.30.30.77     lo

nexus# show ip route
O        172.30.10.0/24 [110/20] via 10.30.30.77, GigabitEthernet0/0/1
O        172.30.20.0/24 [110/20] via 10.30.30.77, GigabitEthernet0/0/1
```

### 見つかった食い違い

1. **実リスナーが mgmt0 に張り付く**。`_pick_management_ip` は管理IPを
   優先するため、mgmt0を持つNexusではOSPFセグメントのHelloを一切
   受け取れなかった。`_pick_ospf_ip()` で `network` に載っている
   インターフェースを選ぶように変更。
2. **セグメントの違う装置がネイバーに出る**。全装置の実リスナーが `lo`
   を共有しているので 224.0.0.5 宛Helloが他セグメントにも届く。
   Si-R (10.20.20.0/24) が Nexus (10.30.30.0/24) をネイバー表示して
   いた。`DeviceOspfResponder._on_packet()` で同一セグメント判定を追加。
3. **経路は入るのにネイバーが ExStart のまま**。装置側Helloは10秒間隔、
   注入側は2秒間隔なので、注入側が先にFullへ抜けてこちらのDBDescを
   無視する。LSUpdを受け取った時点でFullへ進めるようにした。
4. **学習経路の出力IFが `lan0` 固定**。Si-R以外では実在しない
   インターフェース名が出ていた。ネクストホップから解決するように変更。

## Nexus は NX-OS 本来の書き方で設定する（2026-09-03）

NX-OS の `router ospf` 配下に `network` 文は無く、参加させる
インターフェースで直接指定する。IOS形式の `network` しか受け付けて
いなかったため、実機と違う書き方を強いていた。

```
configure terminal
feature ospf
router ospf 1
exit
interface GigabitEthernet0/0/1
 no shutdown
 ip address 10.30.30.1 255.255.255.0
 ip router ospf 1 area 0.0.0.0
end
```

`show running-config` も実機同様の形になる。

```
feature ospf
feature lacp

interface GigabitEthernet0/0/1
  ip address 10.30.30.1/24
  ip router ospf 1 area 0.0.0.0
  no shutdown

router ospf 1
```

`no ip router ospf <tag> area <area>` でそのインターフェースを
OSPFから外せる。IOS形式の `network` も従来どおり使えるので、
既存の手順はそのまま動く。

### あわせて直したもの

`show ip ospf interface` は装置に関係なく
`GigabitEthernet0/0/0 / 192.168.1.1/24` を決め打ちで出していた。
`network` に載っている実インターフェースだけを出すようにし、
1本も無ければ `% OSPF is not enabled on any interface.` を返す。
