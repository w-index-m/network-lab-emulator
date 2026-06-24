# IPsec VPN 設定サンプル集

ネットワークラボエミュレータ上で動作確認済みの IPsec VPN 設定サンプルです。  
ブラウザ上の各機器 CLI にそのままコピー&ペーストして使用できます。

---

## ネットワーク構成図

```
【Si-R ↔ Si-R】
192.168.1.0/24 --- [SiR-Tokyo: WAN 203.0.113.1] ===IPsec=== [SiR-Osaka: WAN 203.0.113.5] --- 192.168.2.0/24

【Si-R ↔ Cisco IOS】
192.168.1.0/24 --- [SiR-HQ: WAN 203.0.113.1] ===IPsec=== [IOS-Branch: WAN 203.0.113.2] --- 10.10.0.0/24

【Cisco IOS ↔ Cisco IOS】
10.1.0.0/24 --- [IOS-Tokyo: WAN 203.0.113.1] ===IPsec=== [IOS-Osaka: WAN 203.0.113.2] --- 10.2.0.0/24
```

---

## パターン 1: Si-R ↔ Si-R

### 共通パラメータ

| 項目 | 値 |
|------|-----|
| Pre-Shared Key | `P@ssw0rd2024` |
| 暗号化アルゴリズム | AES-256 |
| ハッシュアルゴリズム | SHA-256 |
| DHグループ | 14 (2048bit) |
| IKEモード | Main モード |
| IPsecプロトコル | ESP |
| PFS | 有効 |
| IKE SA ライフタイム | 86400秒 (24時間) |
| IPsec SA ライフタイム | 28800秒 (8時間) |

---

### SiR-Tokyo（東京側）

デバイス種別: **Si-R**  
WAN IP: `203.0.113.1/30`  
LAN IP: `192.168.1.1/24`

```
remote 1 ap 0 name VPN-to-Osaka
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.1
remote 1 ap 0 tunnel remote 203.0.113.5
remote 1 ap 0 ipsec ike preshared-key P@ssw0rd2024
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ip route 192.168.2.0/24 203.0.113.5
ipsec use on
ike use on
```

#### show running-config（確認用）

```
hostname SiR-Tokyo
!
password admin set ****
!
lan 0 ip address 192.168.1.1/24 1
lan 0 ip dhcp service server
lan 0 ip dhcp pool address 192.168.1.100 192.168.1.200
lan 0 ip dhcp server dns 8.8.8.8 8.8.4.4
lan 0 ip napt use use
!
wan 1 use use
wan 1 bind ether 0
wan 1 ip address 203.0.113.1/30
wan 1 ip route default metric 100
!
remote 1 ap 0 name VPN-to-Osaka
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.1
remote 1 ap 0 tunnel remote 203.0.113.5
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ap 0 ipsec ike preshared-key ****
remote 1 ip route 192.168.2.0/24 203.0.113.5
!
ipsec use on
ike use on
!
```

---

### SiR-Osaka（大阪側）

デバイス種別: **Si-R**  
WAN IP: `203.0.113.5/30`  
LAN IP: `192.168.2.1/24`

```
remote 1 ap 0 name VPN-to-Tokyo
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.5
remote 1 ap 0 tunnel remote 203.0.113.1
remote 1 ap 0 ipsec ike preshared-key P@ssw0rd2024
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ip route 192.168.1.0/24 203.0.113.1
ipsec use on
ike use on
```

#### show running-config（確認用）

```
hostname SiR-Osaka
!
password admin set ****
!
lan 0 ip address 192.168.2.1/24 1
lan 0 ip dhcp service server
lan 0 ip dhcp pool address 192.168.2.100 192.168.2.200
lan 0 ip dhcp server dns 8.8.8.8 8.8.4.4
lan 0 ip napt use use
!
wan 1 use use
wan 1 bind ether 0
wan 1 ip address 203.0.113.5/30
wan 1 ip route default metric 100
!
remote 1 ap 0 name VPN-to-Tokyo
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.5
remote 1 ap 0 tunnel remote 203.0.113.1
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec pfs use on
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ap 0 ipsec ike preshared-key ****
remote 1 ip route 192.168.1.0/24 203.0.113.1
!
ipsec use on
ike use on
!
```

#### 疎通確認コマンド（SiR-Tokyo から実行）

```
show ipsec sa
show ike sa
show ipsec statistics
show ike statistics
```

---

## パターン 2: Si-R ↔ Cisco IOS

### 共通パラメータ

| 項目 | 値 |
|------|-----|
| Pre-Shared Key | `VPN@Secure123` |
| 暗号化アルゴリズム | AES-256 |
| ハッシュアルゴリズム | SHA-256 |
| DHグループ | 14 (2048bit) |
| IKEモード | Main モード |
| IPsecプロトコル | ESP |
| Transform-set | esp-aes-256 esp-sha256-hmac |
| IKE SA ライフタイム | 86400秒 (24時間) |
| IPsec SA ライフタイム | 28800秒 (8時間) |

---

### SiR-HQ（本社 Si-R 側）

デバイス種別: **Si-R**  
WAN IP: `203.0.113.1/30`  
LAN IP: `192.168.1.1/24`

```
remote 1 ap 0 name VPN-to-CiscoIOS
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.1
remote 1 ap 0 tunnel remote 203.0.113.2
remote 1 ap 0 ipsec ike preshared-key VPN@Secure123
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ip route 10.10.0.0/24 203.0.113.2
ipsec use on
ike use on
```

#### show running-config（確認用）

```
hostname SiR-HQ
!
password admin set ****
!
lan 0 ip address 192.168.1.1/24 1
lan 0 ip dhcp service server
lan 0 ip dhcp pool address 192.168.1.100 192.168.1.200
lan 0 ip dhcp server dns 8.8.8.8 8.8.4.4
lan 0 ip napt use use
!
wan 1 use use
wan 1 bind ether 0
wan 1 ip address 203.0.113.1/30
wan 1 ip route default metric 100
!
remote 1 ap 0 name VPN-to-CiscoIOS
remote 1 ap 0 datalink type ipsec
remote 1 ap 0 ipsec type ike
remote 1 ap 0 tunnel local 203.0.113.1
remote 1 ap 0 tunnel remote 203.0.113.2
remote 1 ap 0 ipsec protocol esp
remote 1 ap 0 ipsec encrypt aes256 sha256
remote 1 ap 0 ipsec ike mode main
remote 1 ap 0 ipsec ike dh 14
remote 1 ap 0 ipsec ike lifetime 86400
remote 1 ap 0 ipsec sa lifetime 28800
remote 1 ap 0 ipsec ike preshared-key ****
remote 1 ip route 10.10.0.0/24 203.0.113.2
!
ipsec use on
ike use on
!
```

---

### IOS-Branch（拠点 Cisco IOS 側）

デバイス種別: **Cisco IOS**  
WAN IP: `203.0.113.2/30` (GigabitEthernet0/0/0)  
LAN IP: `10.10.0.1/24` (GigabitEthernet0/0/1)

```
configure terminal
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
crypto isakmp key VPN@Secure123 address 203.0.113.1
!
crypto ipsec transform-set TS-AES256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
ip access-list extended VPN-ACL
 permit ip 10.10.0.0 0.0.0.255 192.168.1.0 0.0.0.255
!
crypto map CMAP 10 ipsec-isakmp
 match address VPN-ACL
 set peer 203.0.113.1
 set transform-set TS-AES256
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.2 255.255.255.252
 crypto map CMAP
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.10.0.1 255.255.255.0
 no shutdown
!
crypto isakmp enable GigabitEthernet0/0/0
end
```

#### show running-config（確認用）

```
hostname IOS-Branch
!
version 17.9
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.2 255.255.255.252
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.10.0.1 255.255.255.0
 no shutdown
!
ip routing
!
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
!
crypto isakmp key **** address 203.0.113.1
!
crypto ipsec transform-set ts-aes256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
crypto map cmap 10 ipsec-isakmp
 match address VPN-ACL
 set peer 203.0.113.1
 set transform-set ts-aes256
!
crypto isakmp enable gigabitethernet0/0/0
!
end
```

#### 疎通確認コマンド（IOS-Branch から実行）

```
show crypto isakmp sa
show crypto ipsec sa
show crypto isakmp policy
show crypto map
```

---

## パターン 3: Cisco IOS ↔ Cisco IOS

### 共通パラメータ

| 項目 | 値 |
|------|-----|
| Pre-Shared Key | `Cisco@VPN2024` |
| 暗号化アルゴリズム | AES-256 |
| ハッシュアルゴリズム | SHA-256 |
| DHグループ | 14 (2048bit) |
| IKEモード | Main モード |
| IPsecプロトコル | ESP |
| Transform-set | esp-aes-256 esp-sha256-hmac |
| IKE SA ライフタイム | 86400秒 (24時間) |
| IPsec SA ライフタイム | 28800秒 (8時間) |

---

### IOS-Tokyo（東京側）

デバイス種別: **Cisco IOS**  
WAN IP: `203.0.113.1/30` (GigabitEthernet0/0/0)  
LAN IP: `10.1.0.1/24` (GigabitEthernet0/0/1)

```
configure terminal
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
crypto isakmp key Cisco@VPN2024 address 203.0.113.2
!
crypto ipsec transform-set TS-ESP256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
ip access-list extended VPN-ACL-TKY
 permit ip 10.1.0.0 0.0.0.255 10.2.0.0 0.0.0.255
!
crypto map IOSMAP 10 ipsec-isakmp
 match address VPN-ACL-TKY
 set peer 203.0.113.2
 set transform-set TS-ESP256
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.1 255.255.255.252
 crypto map IOSMAP
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.1.0.1 255.255.255.0
 no shutdown
!
crypto isakmp enable GigabitEthernet0/0/0
end
```

#### show running-config（確認用）

```
hostname IOS-Tokyo
!
version 17.9
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.1 255.255.255.252
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.1.0.1 255.255.255.0
 no shutdown
!
ip routing
!
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
!
crypto isakmp key **** address 203.0.113.2
!
crypto ipsec transform-set ts-esp256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
crypto map iosmap 10 ipsec-isakmp
 match address VPN-ACL-TKY
 set peer 203.0.113.2
 set transform-set ts-esp256
!
crypto map iosmap interface gigabitethernet0/0/0
crypto isakmp enable gigabitethernet0/0/0
!
end
```

---

### IOS-Osaka（大阪側）

デバイス種別: **Cisco IOS**  
WAN IP: `203.0.113.2/30` (GigabitEthernet0/0/0)  
LAN IP: `10.2.0.1/24` (GigabitEthernet0/0/1)

```
configure terminal
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
crypto isakmp key Cisco@VPN2024 address 203.0.113.1
!
crypto ipsec transform-set TS-ESP256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
ip access-list extended VPN-ACL-OSK
 permit ip 10.2.0.0 0.0.0.255 10.1.0.0 0.0.0.255
!
crypto map IOSMAP 10 ipsec-isakmp
 match address VPN-ACL-OSK
 set peer 203.0.113.1
 set transform-set TS-ESP256
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.2 255.255.255.252
 crypto map IOSMAP
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.2.0.1 255.255.255.0
 no shutdown
!
crypto isakmp enable GigabitEthernet0/0/0
end
```

#### show running-config（確認用）

```
hostname IOS-Osaka
!
version 17.9
!
interface GigabitEthernet0/0/0
 ip address 203.0.113.2 255.255.255.252
 no shutdown
!
interface GigabitEthernet0/0/1
 ip address 10.2.0.1 255.255.255.0
 no shutdown
!
ip routing
!
crypto isakmp policy 10
 authentication pre-share
 encryption aes 256
 hash sha256
 group 14
 lifetime 86400
!
crypto isakmp key **** address 203.0.113.1
!
crypto ipsec transform-set ts-esp256 esp-aes-256 esp-sha256-hmac
 mode tunnel
!
crypto map iosmap 10 ipsec-isakmp
 match address VPN-ACL-OSK
 set peer 203.0.113.1
 set transform-set ts-esp256
!
crypto map iosmap interface gigabitethernet0/0/0
crypto isakmp enable gigabitethernet0/0/0
!
end
```

#### 疎通確認コマンド（両ルーターから実行）

```
show crypto isakmp sa
show crypto ipsec sa
show crypto isakmp policy
show crypto map
```

---

## トラブルシューティング

### IKE ネゴシエーションが失敗する場合

| 確認項目 | Si-R | Cisco IOS |
|----------|------|-----------|
| PSK 一致確認 | `show ipsec sa` | `show crypto isakmp sa` |
| IKE 有効化 | `ike use on` | `crypto isakmp enable <if>` |
| IPsec 有効化 | `ipsec use on` | `crypto map <name> interface <if>` |
| DHグループ一致 | `remote N ap M ipsec ike dh 14` | `group 14` |
| 暗号アルゴリズム | `ipsec encrypt aes256 sha256` | `encryption aes 256` / `hash sha256` |

### よくあるミス

1. **PSK の不一致** — 両端で同じ文字列を設定すること（大文字小文字区別あり）
2. **DHグループ不一致** — Si-R `dh 14` ↔ Cisco `group 14` を合わせること
3. **暗号化アルゴリズム不一致** — Si-R `aes256` ↔ Cisco `aes 256` は同じ意味
4. **IKE/IPsec 有効化忘れ** — Si-R は `ike use on` と `ipsec use on` が必須
5. **WAN IP の確認** — `tunnel local` / `tunnel remote` とデバイスの実際のインタフェース IP が一致していること

---

## ネゴシエーション成功時の show コマンド出力例

### Si-R: `show ike sa`

```
IKE SA
  Tunnel  Local IP         Remote IP        State    Lifetime
  1       203.0.113.1      203.0.113.5      MATURE   86400s
```

### Si-R: `show ipsec sa`

```
IPsec SA
  Tunnel  Remote IP        Status        SPI(in)      SPI(out)    Lifetime
  1       203.0.113.5      established   0xbea68b0d   0x8bea68b0  28800s
```

### Cisco IOS: `show crypto isakmp sa`

```
IPv4 Crypto ISAKMP SA
dst             src             state          conn-id status
203.0.113.1     203.0.113.2     QM_IDLE           1001 ACTIVE
```

### Cisco IOS: `show crypto ipsec sa`

```
interface: GigabitEthernet0/0/0
    Crypto map tag: CMAP, local addr 203.0.113.2

   protected vrf: (none)
   local  ident (addr/mask/prot/port): (10.10.0.0/255.255.255.0/0/0)
   remote ident (addr/mask/prot/port): (192.168.1.0/255.255.255.0/0/0)
   current_peer 203.0.113.1 port 500
    PERMIT, flags={origin_is_acl,}
   #pkts encaps: 100, #pkts encrypt: 100, #pkts digest: 100
   #pkts decaps: 100, #pkts decrypt: 100, #pkts verify: 100

     inbound esp sas:
      spi: 0x8BEA68B0 (2,347,524,272)
        transform: esp-aes-256 esp-sha256-hmac
        Status: ACTIVE

     outbound esp sas:
      spi: 0xBEA68B0D (3,199,399,693)
        transform: esp-aes-256 esp-sha256-hmac
        Status: ACTIVE
```
