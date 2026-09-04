# Nexus 93180 で vPC を組む手順とステータス確認

`engine/protocols.py` の `VpcEngine`

## 構成

```
        ┌──────────────┐   peer-link (port-channel1)   ┌──────────────┐
        │   N9K-1      │═══════════════════════════════│   N9K-2      │
        │ (nexus)      │   Ethernet1/1 ↔ Ethernet1/1   │ (nexus-2)    │
        │ role pri 100 │                               │ role pri 200 │
        │ mgmt0        │  peer-keepalive (mgmt VRF)    │ mgmt0        │
        │ 192.168.100.1│···············································│192.168.100.2│
        └──────────────┘                               └──────────────┘
               ║                                              ║
          vPC 10 / vPC 20 (port-channel10 / port-channel20)
```

vPC は2台のスイッチが必要なので、既定のラボには1台しかない Nexus を
もう1台追加してから組む。

## 1. 2台目のNexusを作成してリンクする

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")

# 2台目を作成
curl -s -X POST http://localhost:8000/api/device \
  -H "Content-Type: application/json" -H "X-Session-Token: $TOKEN" \
  -d '{"id":"nexus-2","type":"nexus","hostname":"N9K-2"}'

# peer-link 用に接続（vnet.links に載せないとプロトコルが流れない）
curl -s -X POST http://localhost:8000/api/link \
  -H "Content-Type: application/json" -H "X-Session-Token: $TOKEN" \
  -d '{"a":"nexus","b":"nexus-2","iface_a":"Ethernet1/1","iface_b":"Ethernet1/1"}'
```

## 2. 両機のvPC設定

### N9K-1（primary にしたい側 = role priority が小さい方）

```
configure terminal
hostname N9K-1
interface mgmt0
 ip address 192.168.100.1/24
exit
feature vpc
feature lacp
vpc domain 10
 role priority 100
 peer-keepalive destination 192.168.100.2 source 192.168.100.1
 peer-gateway
exit
interface port-channel1
 vpc peer-link
exit
```

### N9K-2（secondary）

```
configure terminal
hostname N9K-2
interface mgmt0
 ip address 192.168.100.2/24
exit
feature vpc
feature lacp
vpc domain 10
 role priority 200
 peer-keepalive destination 192.168.100.1 source 192.168.100.2
 peer-gateway
exit
interface port-channel1
 vpc peer-link
exit
```

### vPCメンバーポート（両機に同じ vPC 番号で設定する）

```
configure terminal
interface port-channel10
 switchport
 switchport mode trunk
 vpc 10
exit
interface port-channel20
 switchport
 switchport mode trunk
 vpc 20
exit
```

> **注意**: `vpc domain 10` などの入力に対して
> `% Invalid command at '^' marker.` が表示されるが、**設定自体は反映
> されている**。表示を出しているルールベースのCLIシミュレータ
> (`engine/rules.py`) がこの構文を知らないだけで、vPCエンジンへの
> 反映は `app.py` 側のハンドラが先に処理している。`show vpc` で
> 実際に入っていることを確認できる（Apresiaの `router rip` でも
> 同じ挙動になる）。

## 3. ステータス確認（実行結果 2026-09-03）

### `show vpc`

```
N9K-1# show vpc
vPC domain id                     : 10
Peer status                        : alive
vPC keep-alive status              : alive
Configuration consistency status   : success
Per-vlan consistency status        : success
Type-2 consistency status          : success
vPC role                           : primary
Number of vPCs configured          : 2
Peer Gateway                       : Enabled
Peer-switch                        : Disabled

vPC Peer-link status
---------------------------------------------------------------------
id    Port   Status Active vlans
--    ----   ------ --------------------------------------------------
1     port-channel1 up     -

vPC status
----------------------------------------------------------------------------
Id    Port          Status Consistency Reason                Active vlans
--    ------------- ------ ----------- ------                ------------
10    port-channel10up     success     -                     -
20    port-channel20up     success     -                     -
```

確認すべき点:

| 項目 | 期待値 |
|---|---|
| `Peer status` | `alive` |
| `vPC keep-alive status` | `alive` |
| `Configuration consistency status` | `success` |
| `vPC role` | primary / secondary が両機で割れている |
| `Number of vPCs configured` | 設定した vPC 数 |
| Peer-link / 各vPC の Status | `up` |

### `show vpc role`

role priority が**小さい方が primary**（実機準拠）。

```
N9K-1# show vpc role
Configured role                    : primary
Current role                       : primary
Role priority                      : 100
System-mac address                 : 00:23:04:65:0a:00
Peer system-mac address            : 00:23:04:d1:0a:00

N9K-2# show vpc role
Configured role                    : secondary
Current role                       : secondary
Role priority                      : 200
```

### `show vpc peer-keepalive`

```
N9K-1# show vpc peer-keepalive
vPC keep-alive status              : alive
--Destination                      : 192.168.100.2
--Source                           : 192.168.100.1
--Vrf                              : management
--Keepalive interval               : 1000 msec
--Keepalive timeout                : 5 sec
--Keepalive hold timeout           : 15 sec
```

### `show vpc brief`

```
N9K-2# show vpc brief
vPC domain id                     : 10
Peer status                        : alive
vPC role                           : secondary
vPC Peer-Link                      : port-channel1 (up)
Number of vPCs                     : 2
  vPC 10  : port-channel10 (up)
  vPC 20  : port-channel20 (up)
```

### `show vpc consistency-parameters global`

```
Name                        Type  Local Value            Peer Value
--------------------------- ----- ---------------------- --------------------
STP mode                    1     Rapid-PVST+            Rapid-PVST+
STP MST region name         1     ""                     ""
STP port type, edge         1     Normal, Disabled       Normal, Disabled
STP MST region revision     1     0
```

## 4. 障害シミュレーション

エミュレーター固有の隠しコマンドで障害を再現できる。

```
configure terminal
vpc peer failure        ! ピアスイッチのダウン
vpc peer-link failure   ! peer-link のダウン
```

`vpc peer failure` 実行後:

```
N9K-1# show vpc brief
Peer status                        : dead        ← alive から変化
vPC role                           : primary
vPC Peer-Link                      : port-channel1 (up)
```

続けて `vpc peer-link failure` 実行後:

```
Peer status                        : dead
vPC Peer-Link                      : port-channel1 (down)   ← down に変化
```

## アプリ再起動後の再構築

**vPCの設定はアプリを再起動すると消える。** `saved_config.json` に
永続化されるのは装置とリンク（トポロジー）だけで、`vpc domain` などの
プロトコル設定はメモリ上にしか無い。

そのため再起動後は:

- **手順1（装置作成とリンク）は不要**。`nexus-2` と
  `nexus↔nexus-2` のリンクは保存済みなのでそのまま残っている。
- **手順2（両機のvPC設定）だけをもう一度流す**。全コマンドをまとめて
  投入して5秒ほど待てば `alive` / `primary` / `secondary` まで上がる。

実際にアプリを停止→起動して、手順2のみの再投入で復旧することを
確認済み（2026-09-03）。

## Peer-Keepalive の宛先ミスは検出される

`peer-keepalive destination` を打ち間違えると、実機同様に上がらない。

```
N9K-1(config-vpc-domain)# peer-keepalive destination 192.168.100.9 source 192.168.100.1
                                                                ^ 本来は .2

N9K-1# show vpc peer-keepalive
vPC keep-alive status              : pending
--Destination                      : 192.168.100.9

N9K-1# show vpc brief
Peer status                        : pending
vPC role                           : none
vPC Peer-Link                      : port-channel1 (down)
  vPC 10  : port-channel10 (down)
```

Keepaliveは `vnet.send_to()` で相手に配送され、受け取った側の
`VpcEngine.receive_keepalive()` が呼ばれて初めて `alive` になる。
送信しただけでは `alive` にならず、受信が `keepalive timeout` の間
途絶えれば `dead` に落ちる。

> 以前は送信側が自分で `alive` を代入しており、しかも
> `vnet.send_to()` に `vpc_keepalive` のディスパッチが無かったので
> パケットは誰にも届いていなかった。宛先IPを間違えても `alive` と
> 表示されるため、vPCで最も多い障害パターンの練習ができなかった。
> （2026-09-03 修正）

## ハマりどころ

- **2台目の装置を作るのを忘れない**。1台だけだと keepalive の相手が
  居らず `Peer status : pending` のまま上がらない。
- **`destination` と `source` は両機で襷掛けに合わせる**。片方の
  `destination` がもう片方の `source` と一致しないとペアにならない
  （VRFも一致が必要）。
- **`/api/link` でリンクを張る**こと。`show cdp neighbors` 等の表示だけ
  合っていても `vnet.links` に載っていないとキープアライブが流れない
  （`docs/cdp-topology-consistency.md` 参照）。
- **mgmt0 のIPを両機に設定する**。peer-keepalive は management VRF 上で
  やり取りする想定のため、`peer-keepalive destination`／`source` に
  指定したIPが実際に装置に付いている必要がある。
- **role priority は小さい方が primary**。HSRP等の「大きい方が優先」と
  逆なので注意。
- vPC番号は**両機で同じ番号**を使うこと。
