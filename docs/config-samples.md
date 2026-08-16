# 設定サンプル集

エミュレータで**動作確認済み**の構成をコピペ可能な形でまとめた設定サンプル集。
各サンプルは `tests/scenario_regression.py` で自動回帰されています。

- 起動: `NETLAB_AUTH_DISABLE=1 NETLAB_FAST_TIMERS=1 uvicorn app:app --port 8099`
- GUIで機器/リンクを作成 → 各機器のCLIに以下をコピペ
- 確認コマンドは各サンプル末尾に記載
- 最終更新: 2026-08-05

## 目次
1. [静的ルーティング（2ルータ）](#1-静的ルーティング2ルータ)
2. [OSPF（3ルータ・チェーン）](#2-ospf3ルータチェーン)
3. [RIP（3ルータ・広域イーサ中継）](#3-rip3ルータ広域イーサ中継)
4. [RIP + distribute-list（経路フィルタ）](#4-rip--distribute-list経路フィルタ)
5. [STP（3スイッチ・トライアングル）](#5-stp3スイッチトライアングル)
   - [5b. STP（3台 SR-S・トライアングル）](#5b-stp3台-sr-sトライアングル)
6. [EtherChannel / LACP（2スイッチ）](#6-etherchannel--lacp2スイッチ)
7. [inter-VLAN ルーティング（L3コア+L2アクセス2台）](#7-inter-vlan-ルーティングl3コアl2アクセス2台)
8. [BGP eBGP（擬似AWS VGW）](#8-bgp-ebgp擬似aws-vgw)
9. [BGP 3-ASトランジット](#9-bgp-3-asトランジット)
10. [BGP AWS Direct Connect + VPNバックアップ](#10-bgp-aws-direct-connect--vpnバックアップ)

---

## 1. 静的ルーティング（2ルータ）

```
[rt1] Lo0:192.168.1.1 --- G0/0/0:10.0.0.1 === 10.0.0.2:G0/0/0 --- Lo0:192.168.2.1 [rt2]
```

**rt1 (Cisco)**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.0.0.1 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.1.1 255.255.255.0
 no shutdown
ip route 192.168.2.0 255.255.255.0 10.0.0.2
```

**rt2 (Cisco)**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.0.0.2 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.2.1 255.255.255.0
 no shutdown
ip route 192.168.1.0 255.255.255.0 10.0.0.1
```

確認: `show ip route`（`S 192.168.2.0/24 [1/0] via 10.0.0.2`）、`ping 192.168.2.1`

---

## 2. OSPF（3ルータ・チェーン）

```
[r1] --- [r2] --- [r3]   すべて area 0、LANはLoopback
```

**r1**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.1.12.1 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.1.1 255.255.255.0
 no shutdown
router ospf 1
 network 10.1.12.0 0.0.0.255 area 0
 network 192.168.1.0 0.0.0.255 area 0
```

**r2**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.1.12.2 255.255.255.0
 no shutdown
interface GigabitEthernet0/0/0
 ip address 10.1.23.2 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.2.1 255.255.255.0
 no shutdown
router ospf 1
 network 10.1.12.0 0.0.0.255 area 0
 network 10.1.23.0 0.0.0.255 area 0
 network 192.168.2.0 0.0.0.255 area 0
```

**r3**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.1.23.3 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.3.1 255.255.255.0
 no shutdown
router ospf 1
 network 10.1.23.0 0.0.0.255 area 0
 network 192.168.3.0 0.0.0.255 area 0
```

確認: `show ip ospf neighbor`、`show ip route ospf`（r1がr3のLAN `O 192.168.3.0/24 [110/30]` を多段学習）、`ping 192.168.3.1`

---

## 3. RIP（3ルータ・広域イーサ中継）

```
[rp1] --- [rp2] --- [rp3]   RIPv2、真ん中rp2が中継
```
> 注: LANは独立クラスC（192.168.11/12/13.0）にすること。同一クラスB内の不連続サブネットは auto-summary で集約される（実機同様）。

**rp1**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.2.12.1 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.11.1 255.255.255.0
 no shutdown
router rip
 version 2
 network 10.0.0.0
 network 192.168.11.0
```

**rp2**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.2.12.2 255.255.255.0
 no shutdown
interface GigabitEthernet0/0/0
 ip address 10.2.23.2 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.12.1 255.255.255.0
 no shutdown
router rip
 version 2
 network 10.0.0.0
 network 192.168.12.0
```

**rp3**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.2.23.3 255.255.255.0
 no shutdown
interface Loopback0
 ip address 192.168.13.1 255.255.255.0
 no shutdown
router rip
 version 2
 network 10.0.0.0
 network 192.168.13.0
```

確認: `show ip route rip`（rp1が `R 192.168.13.0/24 [120/3]` を2ホップ学習）、`ping 192.168.13.1`

---

## 4. RIP + distribute-list（経路フィルタ）

上のRIP構成の中継ルータ(rp2)で、rp3のLAN広告を抑制する例。

**prefix-list方式**
```
configure terminal
ip prefix-list BLK deny 192.168.13.0/24
ip prefix-list BLK permit 0.0.0.0/0 le 32
router rip
 distribute-list BLK out
```

**標準ACL方式**
```
configure terminal
access-list 20 deny 192.168.13.0 0.0.0.255
access-list 20 permit any
router rip
 distribute-list 20 out
```

確認: rp1で `show ip route rip` に `192.168.13.0` が現れない（他経路は正常）。

---

## 5. STP（3スイッチ・トライアングル）

```
[sw1]---[sw2]
   \     /
    [sw3]      3本のリンクでループ構成
```

**sw1 (Catalyst, 最小優先度＝ルート)**
```
enable
configure terminal
spanning-tree vlan 1 priority 4096
spanning-tree mode rapid-pvst
```

**sw2**
```
enable
configure terminal
spanning-tree vlan 1 priority 8192
spanning-tree mode rapid-pvst
```

**sw3**
```
enable
configure terminal
spanning-tree vlan 1 priority 32768
spanning-tree mode rapid-pvst
```

確認: `show spanning-tree`（sw1が root、sw3の1ポートが `Altn/BLK` でループ遮断）。
ルート障害試験: sw1の全リンクを `shutdown` → sw2が新ルートに再選出、`no shutdown` で復帰。

### 5b. STP（3台 SR-S・トライアングル）

SR-S でも Catalyst と同じく3台トライアングルでSTPが成立する（動作確認済み）。

```
[s1]---[s2]
   \   /
    [s3]     s1-s2 / s2-s3 / s1-s3 の3リンクでループ
```

**s1 (SR-S, 最小優先度＝ルート)**
```
enable
configure terminal
spanning-tree mode rstp
spanning-tree vlan 1 priority 4096
```

**s2**
```
enable
configure terminal
spanning-tree mode rstp
spanning-tree vlan 1 priority 8192
```

**s3**
```
enable
configure terminal
spanning-tree mode rstp
spanning-tree vlan 1 priority 32768
```

確認結果（`show spanning-tree`）:
- **s1** = ルート（`This bridge is the root`）、2ポートとも `Desgn/FWD`
- **s2** = `Root/FWD` + `Desgn/FWD`
- **s3**（最大優先度）= `Root/FWD` + **`Altn/BLK`**（ループ遮断）

再収束試験:
- s1 の全リンクを `shutdown` → **s2 が新ルートに昇格**
- s1 を `no shutdown` → s1 がルートに復帰

---

## 6. EtherChannel / LACP（2スイッチ）

```
[ec1] ==2本== [ec2]   Gi1/0/1, Gi1/0/2 をバンドル
```

**ec1 / ec2 共通**
```
enable
configure terminal
interface range gi1/0/1 - 2
 channel-group 1 mode active
```

L3化してping確認する場合（各機に）:
```
interface vlan 1
 ip address 10.9.9.1 255.255.255.0   (ec2は .2)
 no shutdown
```

確認: `show etherchannel summary`（`Po1(SU)` + `Gi1/0/1(P) Gi1/0/2(P)`）。
メンバー障害: `interface gi1/0/1` → `shutdown` で `(D)`、残1本ならPoは`SU`維持。`no shutdown`で`(P)`復帰。

---

## 7. inter-VLAN ルーティング（L3コア+L2アクセス2台）

```
[pc1 VLAN30]---[l2a]===trunk===[l3core]===trunk===[l2b]---[pc2 VLAN40]
                          SVI30:172.20.10.1   SVI40:172.20.20.1
```
PCは GUI/`/api/device` で IP/ゲートウェイを設定:
- pc1: 172.20.10.100 / GW 172.20.10.1
- pc2: 172.20.20.100 / GW 172.20.20.1

**l3core (Catalyst L3)**
```
enable
configure terminal
ip routing
interface vlan 30
 ip address 172.20.10.1 255.255.255.0
 no shutdown
interface vlan 40
 ip address 172.20.20.1 255.255.255.0
 no shutdown
interface GigabitEthernet1/0/1
 switchport mode trunk
interface GigabitEthernet1/0/2
 switchport mode trunk
```

**l2a (アクセス VLAN30)**
```
enable
configure terminal
interface GigabitEthernet1/0/24
 switchport mode trunk
interface GigabitEthernet1/0/1
 switchport access vlan 30
```

**l2b (アクセス VLAN40)**
```
enable
configure terminal
interface GigabitEthernet1/0/24
 switchport mode trunk
interface GigabitEthernet1/0/1
 switchport access vlan 40
```

確認: `show ip interface brief`（Vlan30/Vlan40 up）、pc1で `ping 172.20.20.100`（VLAN間疎通・双方向）。

---

## 8. BGP eBGP（擬似AWS VGW）

```
[cgw AS65000] --- [aws VGW AS64512]
 Lo:192.168.100.0/24        Lo:10.100.0.0/16(VPC)
```

**cgw (顧客)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 169.254.1.1 255.255.255.252
 no shutdown
interface Loopback0
 ip address 192.168.100.1 255.255.255.0
 no shutdown
router bgp 65000
 neighbor 169.254.1.2 remote-as 64512
 network 192.168.100.0 mask 255.255.255.0
```

**aws (VGW相当)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 169.254.1.2 255.255.255.252
 no shutdown
interface Loopback0
 ip address 10.100.0.1 255.255.0.0
 no shutdown
router bgp 64512
 neighbor 169.254.1.1 remote-as 65000
 network 10.100.0.0 mask 255.255.0.0
```

確認: `show ip bgp`（cgwがVPC `10.100.0.0/16` を AS-path `64512` で学習）、`show ip bgp summary`

---

## 9. BGP 3-ASトランジット

```
[b1 AS65001] --- [b2 AS65002] --- [b3 AS65003]   b2がトランジットAS
```

**b1**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.12.0.1 255.255.255.0
 no shutdown
interface Loopback0
 ip address 172.16.1.1 255.255.255.0
 no shutdown
router bgp 65001
 neighbor 10.12.0.2 remote-as 65002
 network 172.16.1.0 mask 255.255.255.0
```

**b2 (トランジット)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 10.12.0.2 255.255.255.0
 no shutdown
interface GigabitEthernet0/0/0
 ip address 10.23.0.2 255.255.255.0
 no shutdown
router bgp 65002
 neighbor 10.12.0.1 remote-as 65001
 neighbor 10.23.0.3 remote-as 65003
 network 172.16.2.0 mask 255.255.255.0
```

**b3**
```
enable
configure terminal
interface GigabitEthernet0/0/0
 ip address 10.23.0.3 255.255.255.0
 no shutdown
interface Loopback0
 ip address 172.16.3.1 255.255.255.0
 no shutdown
router bgp 65003
 neighbor 10.23.0.2 remote-as 65002
 network 172.16.3.0 mask 255.255.255.0
```

確認: `show ip bgp`（b1が `172.16.3.0/24` を AS-path `65002 65003` でトランジット学習）。

---

## 10. BGP AWS Direct Connect + VPNバックアップ

```
             ┌── DX(AS64512) local-pref200 ──┐  (主)
[cgw AS65000]                                  [AWS]
             └── VPN(AS64512) prepend×3 ──────┘  (予備)
```
DXをMD5+BFD付きで優先、VPNをAS-path prependでバックアップにする冗長構成。

**cgw (顧客)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 169.254.10.1 255.255.255.252
 no shutdown
interface GigabitEthernet0/0/2
 ip address 169.254.20.1 255.255.255.252
 no shutdown
interface Loopback0
 ip address 192.168.200.1 255.255.255.0
 no shutdown
route-map DX-IN permit 10
 set local-preference 200
router bgp 65000
 neighbor 169.254.10.2 remote-as 64512
 neighbor 169.254.10.2 password KEY1
 neighbor 169.254.10.2 fall-over bfd
 neighbor 169.254.10.2 route-map DX-IN in
 neighbor 169.254.20.2 remote-as 64512
 neighbor 169.254.20.2 password KEY2
 network 192.168.200.0 mask 255.255.255.0
```

**awsdx (Direct Connect)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 169.254.10.2 255.255.255.252
 no shutdown
interface Loopback0
 ip address 10.200.0.1 255.255.0.0
 no shutdown
router bgp 64512
 neighbor 169.254.10.1 remote-as 65000
 neighbor 169.254.10.1 password KEY1
 neighbor 169.254.10.1 fall-over bfd
 network 10.200.0.0 mask 255.255.0.0
```

**awsvpn (VPNバックアップ)**
```
enable
configure terminal
interface GigabitEthernet0/0/1
 ip address 169.254.20.2 255.255.255.252
 no shutdown
interface Loopback0
 ip address 10.200.0.2 255.255.0.0
 no shutdown
route-map VPN-OUT permit 10
 set as-path prepend 64512 64512
router bgp 64512
 neighbor 169.254.20.1 remote-as 65000
 neighbor 169.254.20.1 password KEY2
 neighbor 169.254.20.1 route-map VPN-OUT out
 network 10.200.0.0 mask 255.255.0.0
```

確認:
- `show ip bgp`（DXが `*>` ベスト local-pref200、VPNが `*` prepend `64512 64512 64512`）
- `show bfd neighbors`（DXのBFDがUp）
- フェイルオーバー: cgwのGi0/0/1を`shutdown` → VPNへ切替、`no shutdown`で復帰
- MD5不一致: 片側のpasswordを変更 → セッションが `Active` で確立不可
