# Cisco ↔ Si-R IPsec VPN ＋ トンネル上OSPF ルーティング

エミュレータで**動作確認済み**の、Cisco IOS ルータと Si-R ルータ間の
サイト間 IPsec VPN と、その上で OSPF による動的ルーティングを行う構成サンプル。

> **最終検証: 2026-08-19**（実サーバで再確認）。
> `show ipsec sa` = MATURE / `show crypto isakmp sa` = QM_IDLE・ACTIVE /
> OSPF相互学習（IOSが192.168.1.0、SiRが10.10.0.0）/ `ping 192.168.1.1` = 100%。

```
192.168.1.0/24 --- [SiR-HQ]                          [IOS-Branch] --- 10.10.0.0/24
   (LAN)      lan0:192.168.1.1                     Gi0/0/1:10.10.0.1  (LAN)
                     wan1:203.0.113.1 ===IPsec=== Gi0/0/0:203.0.113.2
                                 (ESP / AES-256 / SHA-256 / DH14)
                         ＋ OSPF area 0 で LAN 経路を動的交換
```

| 項目 | 値 |
|------|-----|
| Pre-Shared Key | `VPN@Secure123` |
| 暗号 / ハッシュ / DH | AES-256 / SHA-256 / group14 |
| IKEモード / プロトコル | main / ESP |
| ルーティング | OSPF area 0（LAN + WAN を広告） |

> **設定順序の注意**: Cisco 側を先に設定し、**Si-R 側を後**に設定してください。
> Si-R の `ike use on`（最後のコマンド）が IKE ネゴシエーションの開始点になり、
> 対向 Cisco が設定済みならその場でトンネルが確立します（一発成立）。
> 逆順の場合は、両側設定後に Si-R で `ike use on` をもう一度実行すれば確立します。

---

## 1. IOS-Branch（Cisco IOS）— 先に設定

```
enable
configure terminal
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
crypto isakmp key VPN@Secure123 address 203.0.113.1
crypto ipsec transform-set TS-AES256 esp-aes-256 esp-sha256-hmac
ip access-list extended VPN-ACL
 permit ip 10.10.0.0 0.0.0.255 192.168.1.0 0.0.0.255
crypto map CMAP 10 ipsec-isakmp
 match address VPN-ACL
 set peer 203.0.113.1
 set transform-set TS-AES256
interface GigabitEthernet0/0/0
 ip address 203.0.113.2 255.255.255.252
 crypto map CMAP
 no shutdown
interface GigabitEthernet0/0/1
 ip address 10.10.0.1 255.255.255.0
 no shutdown
crypto isakmp enable GigabitEthernet0/0/0
!
router ospf 1
 network 203.0.113.0 0.0.0.3 area 0
 network 10.10.0.0 0.0.0.255 area 0
```

## 2. SiR-HQ（Si-R）— 後に設定

```
lan 0 ip address 192.168.1.1/24 1
wan 1 ip address 203.0.113.1/30
remote 1 ap 0 name VPN
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.1
remote 1 ap 0 tunnel remote 203.0.113.2
remote 1 ap 0 ipsec ike preshared-key VPN@Secure123
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec protocol esp
ipsec use on
ike use on
!
ospf use on
ospf area 0
lan 0 ip ospf use on
wan 1 ip ospf use on
```

---

## 3. 動作確認

### IPsec トンネル確立

```
SiR-HQ# show ipsec sa
  Remote       Local        Protocol  SPI(In)     SPI(Out)    State
  203.0.113.2  203.0.113.1  ESP       0x40ac945e  0x0ac945ee  MATURE
```
```
IOS-Branch# show crypto isakmp sa
dst             src             state      conn-id status
203.0.113.1     203.0.113.2     QM_IDLE    8144    ACTIVE
```

### トンネル上 OSPF（LAN 経路を動的交換）

```
IOS-Branch# show ip ospf neighbor
Neighbor ID   Pri  State      Dead Time  Address      Interface
10.0.132.1    1    Full/BDR   00:00:03   10.0.123.1   GigabitEthernet0/0/0

IOS-Branch# show ip route ospf
O   192.168.1.0/24 [110/20] via ...      ← SiR-HQ の LAN を OSPF 学習

SiR-HQ# show ip route
O   10.10.0.0/24  [110/20] via ...       ← IOS-Branch の LAN を OSPF 学習
```
> 静的ルート（`remote 1 ip route ...` / `ip route ...`）を書かなくても、
> OSPF で相互の LAN 経路を自動的に学習します。

### LAN 間疎通（IPsec で保護）

```
IOS-Branch# ping 192.168.1.1
!!!!!
Success rate is 100 percent (5/5)
```

---

## 補足

- 本エミュレータは Cisco の crypto map 方式による IPsec と Si-R の `remote ap` 方式を
  相互接続（IKE Phase1/Phase2 のパラメータ一致を判定）します。
- OSPF は IPsec ピア間（WAN セグメント）で隣接を確立し、LAN 経路を交換します。
  実機で GRE トンネルインターフェース上に OSPF を載せる構成（GRE over IPsec）とは
  異なり、本エミュレータではピア間の OSPF 隣接として再現しています。
- IKE パラメータ不一致（PSK・暗号・DH 等）の場合はトンネルが `LARVAL`/`Waiting` の
  ままとなり確立しません（`show ipsec sa` / `show crypto isakmp sa` で確認）。
