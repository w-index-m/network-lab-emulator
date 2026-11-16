# Fujitsu SR-S × RIP — 3台構成の正常/障害試験

- 実行: 2026-11-16 04:50 / 所要 58 秒 / 専用環境(保存設定を消してエミュレータを新規起動、他シナリオの装置は無し)
- 結果: **14 / 14 項目 OK** ✅
- 再実行: `python3 tools/verification/scenario_report.py srs rip --devices 3`

## 構成(台数から自動で割り当て)

```
LAN 10.62.110.0/24 ── SRS-RIP-R1 ══ SRS-RIP-R2 ══ SRS-RIP-R3 ── LAN 10.62.111.0/24
```

| 装置 | IF | アドレス |
|---|---|---|
| SRS-RIP-R1 | GigabitEthernet1/0/2 | 10.62.81.1/24 |
| SRS-RIP-R1 | GigabitEthernet1/0/3 | 10.62.110.1/24 |
| SRS-RIP-R2 | GigabitEthernet1/0/1 | 10.62.81.2/24 |
| SRS-RIP-R2 | GigabitEthernet1/0/2 | 10.62.82.1/24 |
| SRS-RIP-R3 | GigabitEthernet1/0/1 | 10.62.82.2/24 |
| SRS-RIP-R3 | GigabitEthernet1/0/3 | 10.62.111.1/24 |

| リンク | 装置A IF | 装置B IF | ネットワーク |
|---|---|---|---|
| SRS-RIP-R1 ⇔ SRS-RIP-R2 | GigabitEthernet1/0/2 | GigabitEthernet1/0/1 | 10.62.81.0/24 |
| SRS-RIP-R2 ⇔ SRS-RIP-R3 | GigabitEthernet1/0/2 | GigabitEthernet1/0/1 | 10.62.82.0/24 |

## 投入したコマンド

<details><summary>SRS-RIP-R1(19 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/2
no switchport
ip address 10.62.81.1 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/3
no switchport
ip address 10.62.110.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router rip
version 2
network 10.0.0.0
no auto-summary
end
```
</details>

<details><summary>SRS-RIP-R2(43 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/1
no switchport
ip address 10.62.81.2 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no switchport
ip address 10.62.82.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router rip
version 2
network 10.0.0.0
no auto-summary
end
configure terminal
interface GigabitEthernet1/0/2
shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no shutdown
end
configure terminal
interface GigabitEthernet1/0/2
shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no shutdown
end
configure terminal
interface GigabitEthernet1/0/2
shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no shutdown
end
```
</details>

<details><summary>SRS-RIP-R3(19 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/1
no switchport
ip address 10.62.82.2 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/3
no switchport
ip address 10.62.111.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router rip
version 2
network 10.0.0.0
no auto-summary
end
```
</details>

## 確認項目

| 段階 | 確認内容 | 結果 | 補足 |
|---|---|---|---|
| 正常 | SRS-RIP-R1 が SRS-RIP-R3 の LAN 10.62.111.0/24 を学習(R) | ✅ |  |
| 正常 | 全リンクが links API で state=ok(画面は緑の NORMAL) | ✅ |  |
| 正常 | 経路追加の INFO(情)が出る | ✅ |  |
| 正常 | 状態ボードのネットワーク観点が正常 | ✅ |  |
| 障害 | SRS-RIP-R2-SRS-RIP-R3 が LINK DOWN | ✅ |  |
| 障害 | 検知経路に STATE と SYSLOG | ✅ |  |
| 障害 | 状態ボードのネットワーク観点が異常 | ✅ |  |
| 障害 | link_down アラート | ✅ |  |
| 障害 | SRS-RIP-R1 から LAN 10.62.111.0/24 の経路が消える | ✅ |  |
| 障害 | 経路削除の INFO(情)が出る | ✅ |  |
| 復旧 | 経路が戻る | ✅ |  |
| 復旧 | LINK DOWN が解除される | ✅ |  |
| フラップ | if_flap(注 WARN IF FLAP)が出る | ✅ |  |
| フラップ | 状態ボードの syslog 観点にフラップ件数 | ✅ |  |

## 画面(ROUTER LINKS)

**正常時**

![正常時](img/srs-rip-normal.png)

**障害時**

![障害時](img/srs-rip-fault.png)

## 段階ごとの記録

### 正常(04:50:42)

SRS-RIP-R1 から SRS-RIP-R3 の LAN まで経路が通っている状態

**状態ボード**: network=ok「リンク 2本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=ok「error以上 0 ・ warning 0 ・ 全 27件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| SRS-RIP-R1 | SRS-RIP-R2 | ok | - | STATE:up, SYSLOG:up |
| SRS-RIP-R2 | SRS-RIP-R3 | ok | - | STATE:up, SYSLOG:up |

`SRS-RIP-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.110.0/24 is directly connected, GigabitEthernet1/0/3
L        10.62.110.1/32 is directly connected, GigabitEthernet1/0/3
C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.81.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.111.0/24 [120/2] via 10.62.81.2, GigabitEthernet1/0/2
R        10.62.82.0/24 [120/1] via 10.62.81.2, GigabitEthernet1/0/2
```

`SRS-RIP-R2# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/1
L        10.62.81.2/32 is directly connected, GigabitEthernet1/0/1
C        10.62.82.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.82.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.110.0/24 [120/1] via 10.62.81.1, GigabitEthernet1/0/1
R        10.62.111.0/24 [120/1] via 10.62.82.2, GigabitEthernet1/0/2
```

**ログ**(/cyber-view が見ているもの、9 件)

```
04:50:28 SRS-RIP-R1 sev5 %RIPV2-5-RDBUPDATE: Sending update to 224.0.0.9 via all interfaces - v2 10.62.81.0/24, 10.62.110.0/24
04:50:29 SRS-RIP-R2 sev5 %RIPV2-5-RDBUPDATE: Sending update to 224.0.0.9 via all interfaces - v2 10.62.81.0/24, 10.62.82.0/24
04:50:29 SRS-RIP-R2 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.110.0/24 [120/1] via SRS-RIP-R1
04:50:31 SRS-RIP-R1 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.82.0/24 [120/1] via SRS-RIP-R2
04:50:33 SRS-RIP-R3 sev5 %RIPV2-5-RDBUPDATE: Sending update to 224.0.0.9 via all interfaces - v2 10.62.82.0/24, 10.62.111.0/24
04:50:33 SRS-RIP-R3 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.81.0/24 [120/1] via SRS-RIP-R2
04:50:33 SRS-RIP-R3 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.110.0/24 [120/2] via SRS-RIP-R2
04:50:36 SRS-RIP-R2 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.111.0/24 [120/1] via SRS-RIP-R3
04:50:38 SRS-RIP-R1 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.111.0/24 [120/2] via SRS-RIP-R2
```

**アラート**(0 件)


**経路の変化(情 INFO)**(6 件)

- SRS-RIP-R2 add R 10.62.110.0/24 via 10.62.81.1
- SRS-RIP-R1 add R 10.62.82.0/24 via 10.62.81.2
- SRS-RIP-R3 add R 10.62.110.0/24 via 10.62.82.1
- SRS-RIP-R3 add R 10.62.81.0/24 via 10.62.82.1
- SRS-RIP-R1 add R 10.62.111.0/24 via 10.62.81.2
- SRS-RIP-R2 add R 10.62.111.0/24 via 10.62.82.2

### 障害(04:51:03)

SRS-RIP-R2 の GigabitEthernet1/0/2 を落とした状態

**状態ボード**: network=crit「リンク断 1本」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 1 ・ warning 1 ・ 全 30件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| SRS-RIP-R1 | SRS-RIP-R2 | ok | - | STATE:up, SYSLOG:up |
| SRS-RIP-R2 | SRS-RIP-R3 | down | はい | STATE:down, SYSLOG:down |

`SRS-RIP-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.110.0/24 is directly connected, GigabitEthernet1/0/3
L        10.62.110.1/32 is directly connected, GigabitEthernet1/0/3
C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.81.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.82.0/24 [120/1] via 10.62.81.2, GigabitEthernet1/0/2
```

`SRS-RIP-R2# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/1
L        10.62.81.2/32 is directly connected, GigabitEthernet1/0/1
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.110.0/24 [120/1] via 10.62.81.1, GigabitEthernet1/0/1
```

**ログ**(/cyber-view が見ているもの、3 件)

```
04:50:50 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:50:50 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:50:50 SRS-RIP-R2 sev4 2026/11/16 04:50:50 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Down
```

**アラート**(2 件)

- [medium/SYSLOG] 装置が error 以上のログを出力 — SRS-RIP-R2: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
- [medium/NET] リンク断 — SRS-RIP-R2: GigabitEthernet1/0/2 が down (srs-rip-r2 - srs-rip-r3)

**経路の変化(情 INFO)**(3 件)

- SRS-RIP-R1 del R 10.62.111.0/24 via 10.62.81.2
- SRS-RIP-R2 del R 10.62.111.0/24 via 10.62.82.2
- SRS-RIP-R2 del C 10.62.82.0/24 [GigabitEthernet1/0/2]

### 復旧(04:51:13)

戻した状態

**状態ボード**: network=ok「リンク 2本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 1 ・ warning 1 ・ 全 35件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| SRS-RIP-R1 | SRS-RIP-R2 | ok | - | STATE:up, SYSLOG:up |
| SRS-RIP-R2 | SRS-RIP-R3 | ok | - | STATE:up, SYSLOG:up |

`SRS-RIP-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.110.0/24 is directly connected, GigabitEthernet1/0/3
L        10.62.110.1/32 is directly connected, GigabitEthernet1/0/3
C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.81.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.111.0/24 [120/2] via 10.62.81.2, GigabitEthernet1/0/2
R        10.62.82.0/24 [120/1] via 10.62.81.2, GigabitEthernet1/0/2
```

`SRS-RIP-R2# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/1
L        10.62.81.2/32 is directly connected, GigabitEthernet1/0/1
C        10.62.82.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.82.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.110.0/24 [120/1] via 10.62.81.1, GigabitEthernet1/0/1
R        10.62.111.0/24 [120/1] via 10.62.82.2, GigabitEthernet1/0/2
```

**ログ**(/cyber-view が見ているもの、5 件)

```
04:51:03 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:51:03 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
04:51:03 SRS-RIP-R2 sev5 2026/11/16 04:51:03 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Up (1Gbps full-duplex)
04:51:07 SRS-RIP-R2 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.111.0/24 [120/1] via SRS-RIP-R3
04:51:09 SRS-RIP-R1 sev6 %RIPV2-6-RDBUPDATE: Updating full routing table - 10.62.111.0/24 [120/2] via SRS-RIP-R2
```

**アラート**(0 件)


**経路の変化(情 INFO)**(3 件)

- SRS-RIP-R2 add C 10.62.82.0/24 [GigabitEthernet1/0/2]
- SRS-RIP-R1 add R 10.62.111.0/24 via 10.62.81.2
- SRS-RIP-R2 add R 10.62.111.0/24 via 10.62.82.2

### フラップ(04:51:21)

落とす/戻すを2回繰り返した直後

**状態ボード**: network=ok「リンク 2本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 3 ・ warning 3 ・ 全 47件 ・ フラップ 4件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| SRS-RIP-R1 | SRS-RIP-R2 | ok | - | STATE:up, SYSLOG:up |
| SRS-RIP-R2 | SRS-RIP-R3 | ok | - | STATE:up, SYSLOG:up |

`SRS-RIP-R2# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.62.81.0/24 is directly connected, GigabitEthernet1/0/1
L        10.62.81.2/32 is directly connected, GigabitEthernet1/0/1
C        10.62.82.0/24 is directly connected, GigabitEthernet1/0/2
L        10.62.82.1/32 is directly connected, GigabitEthernet1/0/2
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
R        10.62.110.0/24 [120/1] via 10.62.81.1, GigabitEthernet1/0/1
```

**ログ**(/cyber-view が見ているもの、12 件)

```
04:51:13 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:51:13 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:51:13 SRS-RIP-R2 sev4 2026/11/16 04:51:13 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Down
04:51:15 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:51:15 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
04:51:15 SRS-RIP-R2 sev5 2026/11/16 04:51:15 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Up (1Gbps full-duplex)
04:51:15 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:51:15 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:51:15 SRS-RIP-R2 sev4 2026/11/16 04:51:15 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Down
04:51:15 SRS-RIP-R2 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:51:15 SRS-RIP-R2 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
04:51:15 SRS-RIP-R2 sev5 2026/11/16 04:51:15 SRS-RIP-R2 Si-R G120 : [LINK] GigabitEthernet1/0/2 Link Up (1Gbps full-duplex)
```

**アラート**(6 件)

- [warning/SYSLOG] インタフェースのフラップ — SRS-RIP-R2: GigabitEthernet1/0/2: 60秒に 3 回 状態変化
- [medium/NET] リンク断 — SRS-RIP-R2: GigabitEthernet1/0/2 が down (srs-rip-r2 - srs-rip-r3)
- [medium/NET] リンク断 — SRS-RIP-R2: GigabitEthernet1/0/2 が down (srs-rip-r2 - srs-rip-r3)
- [warning/NET] 経路のフラップ — SRS-RIP-R1: 10.62.111.0/24: 120秒に 4 回 追加/削除/変更
- [warning/NET] 経路のフラップ — SRS-RIP-R2: 10.62.111.0/24: 120秒に 4 回 追加/削除/変更
- [warning/NET] 経路のフラップ — SRS-RIP-R2: 10.62.82.0/24: 120秒に 4 回 追加/削除/変更

**経路の変化(情 INFO)**(4 件)

- SRS-RIP-R1 del R 10.62.111.0/24 via 10.62.81.2
- SRS-RIP-R2 del R 10.62.111.0/24 via 10.62.82.2
- SRS-RIP-R2 del C 10.62.82.0/24 [GigabitEthernet1/0/2]
- SRS-RIP-R2 add C 10.62.82.0/24 [GigabitEthernet1/0/2]

