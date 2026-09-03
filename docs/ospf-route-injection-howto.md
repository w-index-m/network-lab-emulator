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
