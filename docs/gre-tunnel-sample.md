# GRE トンネルインターフェース ＋ トンネル上OSPF

エミュレータで**動作確認済み**の GRE トンネル構成サンプル。
物理的に非隣接な2台のルータを GRE トンネルで結び、その上で OSPF ルーティングを行う。

```
              (物理チェーン: R1 と R3 は直接繋がっていない)
[R1] --- G0/0/0 --- [R2] --- G0/0/1 --- [R3]
 10.71.12.1      10.71.12.2/10.71.23.2      10.71.23.3
 Lo0:192.168.71.1                          Lo0:192.168.73.1
     └──────── GRE Tunnel0 (172.31.0.0/30) ────────┘
             tunnel source/destination = 物理IP
             OSPF area 0 をトンネル上で稼働
```

対応コマンド（Cisco/Catalyst）:

| コマンド | 説明 |
|---|---|
| `interface Tunnel<n>` | トンネルインターフェース作成 |
| `ip address <ip> <mask>` | トンネルIP（オーバーレイのサブネット） |
| `tunnel source <ip\|interface>` | トンネル始点（自分の物理IP） |
| `tunnel destination <ip>` | トンネル終点（対向の物理IP） |
| `tunnel mode gre ip` | GRE モード（デフォルト） |

- 両端の source/destination が相互に一致し、かつ transport（source→destination）が
  到達可能なとき、トンネルが `up` になり**仮想的な直結隣接**が張られる。
- 物理的に多段（間にルータがある）でも、underlay で対向の物理IPに到達できれば成立。
- トンネルIPサブネット上で ping / OSPF / スタティック等が利用可能。

---

## 1. Underlay（物理到達性）

**R1**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.71.12.1 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.71.1 255.255.255.0
 no shutdown
ip route 10.71.23.0 255.255.255.0 10.71.12.2
```

**R2（中継）**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.71.12.2 255.255.255.0
 no shutdown
interface GigabitEthernet0/0/1
 ip address 10.71.23.2 255.255.255.0
 no shutdown
```

**R3**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.71.23.3 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.73.1 255.255.255.0
 no shutdown
ip route 10.71.12.0 255.255.255.0 10.71.23.2
```

## 2. GRE トンネル（R1 ↔ R3、R2 を越える）

**R1**
```
interface Tunnel0
 ip address 172.31.0.1 255.255.255.252
 tunnel source 10.71.12.1
 tunnel destination 10.71.23.3
 tunnel mode gre ip
```

**R3**
```
interface Tunnel0
 ip address 172.31.0.2 255.255.255.252
 tunnel source 10.71.23.3
 tunnel destination 10.71.12.1
 tunnel mode gre ip
```

## 3. トンネル上 OSPF

**R1**
```
router ospf 1
 network 172.31.0.0 0.0.0.3 area 0
 network 192.168.71.0 0.0.0.255 area 0
```

**R3**
```
router ospf 1
 network 172.31.0.0 0.0.0.3 area 0
 network 192.168.73.0 0.0.0.255 area 0
```

---

## 4. 動作確認

```
R1# show ip interface brief | include Tunnel
Tunnel0                172.31.0.1      YES NVRAM  up   up

R1# ping 172.31.0.2                 ← トンネルIP疎通(マルチホップ越し)
!!!!!  Success rate is 100 percent (5/5)

R1# show ip ospf neighbor           ← トンネル越しにFull隣接
Neighbor ID   Pri  State     Dead Time  Address       Interface
...           1    Full/BDR  00:00:03   ...           ...

R1# show ip route ospf              ← R3のLANをトンネル経由で学習
O   192.168.73.0/24 [110/20] via ...

R1# ping 192.168.73.1               ← トンネル経由OSPF経路でLAN間疎通
!!!!!  Success rate is 100 percent (5/5)
```

---

## 補足: GRE over IPsec

`docs/ipsec-ospf-sample.md` の IPsec 構成と組み合わせることで、GRE トンネルを
IPsec で保護する GRE over IPsec 相当の構成も表現できます（トンネルの transport IP を
IPsec で保護し、OSPF はトンネルインターフェース上で稼働）。
