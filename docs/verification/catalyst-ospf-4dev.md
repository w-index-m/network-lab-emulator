# Cisco Catalyst × OSPF — 4台構成の正常/障害試験

- 実行: 2026-11-16 04:56 / 所要 47 秒 / 専用環境(保存設定を消してエミュレータを新規起動、他シナリオの装置は無し)
- 結果: **14 / 14 項目 OK** ✅
- 再実行: `python3 tools/verification/scenario_report.py catalyst ospf --devices 4`

## 構成(台数から自動で割り当て)

```
LAN 10.61.150.0/24 ── CATALYST-OSPF-R1 ══ CATALYST-OSPF-R2 ══ CATALYST-OSPF-R3 ══ CATALYST-OSPF-R4 ── LAN 10.61.151.0/24
```

| 装置 | IF | アドレス |
|---|---|---|
| CATALYST-OSPF-R1 | GigabitEthernet1/0/2 | 10.61.121.1/24 |
| CATALYST-OSPF-R1 | GigabitEthernet1/0/3 | 10.61.150.1/24 |
| CATALYST-OSPF-R2 | GigabitEthernet1/0/1 | 10.61.121.2/24 |
| CATALYST-OSPF-R2 | GigabitEthernet1/0/2 | 10.61.122.1/24 |
| CATALYST-OSPF-R3 | GigabitEthernet1/0/1 | 10.61.122.2/24 |
| CATALYST-OSPF-R3 | GigabitEthernet1/0/2 | 10.61.123.1/24 |
| CATALYST-OSPF-R4 | GigabitEthernet1/0/1 | 10.61.123.2/24 |
| CATALYST-OSPF-R4 | GigabitEthernet1/0/3 | 10.61.151.1/24 |

| リンク | 装置A IF | 装置B IF | ネットワーク |
|---|---|---|---|
| CATALYST-OSPF-R1 ⇔ CATALYST-OSPF-R2 | GigabitEthernet1/0/2 | GigabitEthernet1/0/1 | 10.61.121.0/24 |
| CATALYST-OSPF-R2 ⇔ CATALYST-OSPF-R3 | GigabitEthernet1/0/2 | GigabitEthernet1/0/1 | 10.61.122.0/24 |
| CATALYST-OSPF-R3 ⇔ CATALYST-OSPF-R4 | GigabitEthernet1/0/2 | GigabitEthernet1/0/1 | 10.61.123.0/24 |

## 投入したコマンド

<details><summary>CATALYST-OSPF-R1(18 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/2
no switchport
ip address 10.61.121.1 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/3
no switchport
ip address 10.61.150.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router ospf 1
network 10.61.121.0 0.0.0.255 area 0
network 10.61.150.0 0.0.0.255 area 0
end
```
</details>

<details><summary>CATALYST-OSPF-R2(18 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/1
no switchport
ip address 10.61.121.2 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no switchport
ip address 10.61.122.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router ospf 1
network 10.61.121.0 0.0.0.255 area 0
network 10.61.122.0 0.0.0.255 area 0
end
```
</details>

<details><summary>CATALYST-OSPF-R3(42 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/1
no switchport
ip address 10.61.122.2 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/2
no switchport
ip address 10.61.123.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router ospf 1
network 10.61.122.0 0.0.0.255 area 0
network 10.61.123.0 0.0.0.255 area 0
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

<details><summary>CATALYST-OSPF-R4(18 行)</summary>

```
configure terminal
interface GigabitEthernet1/0/1
no switchport
ip address 10.61.123.2 255.255.255.0
no shutdown
end
configure terminal
interface GigabitEthernet1/0/3
no switchport
ip address 10.61.151.1 255.255.255.0
no shutdown
end
configure terminal
ip routing
router ospf 1
network 10.61.123.0 0.0.0.255 area 0
network 10.61.151.0 0.0.0.255 area 0
end
```
</details>

## 確認項目

| 段階 | 確認内容 | 結果 | 補足 |
|---|---|---|---|
| 正常 | CATALYST-OSPF-R1 が CATALYST-OSPF-R4 の LAN 10.61.151.0/24 を学習(O) | ✅ |  |
| 正常 | 全リンクが links API で state=ok(画面は緑の NORMAL) | ✅ |  |
| 正常 | 経路追加の INFO(情)が出る | ✅ |  |
| 正常 | 状態ボードのネットワーク観点が正常 | ✅ |  |
| 障害 | CATALYST-OSPF-R3-CATALYST-OSPF-R4 が LINK DOWN | ✅ |  |
| 障害 | 検知経路に STATE と SYSLOG | ✅ |  |
| 障害 | 状態ボードのネットワーク観点が異常 | ✅ |  |
| 障害 | link_down アラート | ✅ |  |
| 障害 | CATALYST-OSPF-R1 から LAN 10.61.151.0/24 の経路が消える | ✅ |  |
| 障害 | 経路削除の INFO(情)が出る | ✅ |  |
| 復旧 | 経路が戻る | ✅ |  |
| 復旧 | LINK DOWN が解除される | ✅ |  |
| フラップ | if_flap(注 WARN IF FLAP)が出る | ✅ |  |
| フラップ | 状態ボードの syslog 観点にフラップ件数 | ✅ |  |

## 画面(ROUTER LINKS)

**正常時**

![正常時](img/catalyst-ospf-4dev-normal.png)

**障害時**

![障害時](img/catalyst-ospf-4dev-fault.png)

## 段階ごとの記録

### 正常(04:56:53)

CATALYST-OSPF-R1 から CATALYST-OSPF-R4 の LAN まで経路が通っている状態

**状態ボード**: network=ok「リンク 3本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=ok「error以上 0 ・ warning 0 ・ 全 49件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| CATALYST-OSPF-R1 | CATALYST-OSPF-R2 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R2 | CATALYST-OSPF-R3 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R3 | CATALYST-OSPF-R4 | ok | - | STATE:up, SYSLOG:up |

`CATALYST-OSPF-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.61.121.0/24 is directly connected, GigabitEthernet1/0/2
L        10.61.121.1/32 is directly connected, GigabitEthernet1/0/2
C        10.61.150.0/24 is directly connected, GigabitEthernet1/0/3
L        10.61.150.1/32 is directly connected, GigabitEthernet1/0/3
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
O        10.61.122.0/24 [110/20] via 10.61.121.2, GigabitEthernet1/0/2
O        10.61.123.0/24 [110/30] via 10.61.121.2, GigabitEthernet1/0/2
O        10.61.151.0/24 [110/40] via 10.61.121.2, GigabitEthernet1/0/2
```

`CATALYST-OSPF-R2# show ip ospf neighbor`

```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.61.121.1     1     Full/DROther    00:00:33    10.61.121.1     GigabitEthernet1/0/1
10.61.123.1     1     Full/DR         00:00:34    10.61.122.2     GigabitEthernet1/0/2
```

**ログ**(/cyber-view が見ているもの、33 件)

```
04:56:45 CATALYST-OSPF-R1 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.0.12.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R1 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.61.121.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R2 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.0.125.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R2 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.61.122.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.0.81.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.61.123.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R4 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.0.98.1, Area 0.0.0.0
04:56:45 CATALYST-OSPF-R4 sev5 %OSPF-5-ADJCHG: Process 1 started. Router ID 10.61.123.2, Area 0.0.0.0
04:56:47 CATALYST-OSPF-R4 sev6 %OSPF-6-DRCHG: DR Change from 0.0.0.0 to 10.61.123.2 on GigabitEthernet0/0
04:56:47 CATALYST-OSPF-R4 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.1 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:47 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 2 routes
04:56:47 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 2 routes
04:56:47 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 2 routes
04:56:47 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:56:47 CATALYST-OSPF-R3 sev6 %OSPF-6-DRCHG: DR Change from 0.0.0.0 to 10.61.123.2 on GigabitEthernet0/0
04:56:47 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.122.1 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:47 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:56:47 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:56:48 CATALYST-OSPF-R2 sev6 %OSPF-6-DRCHG: DR Change from 0.0.0.0 to 10.61.123.1 on GigabitEthernet0/0
04:56:48 CATALYST-OSPF-R2 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.121.1 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:48 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:56:48 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:56:48 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:56:48 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.2 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:48 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:56:48 CATALYST-OSPF-R2 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.1 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:48 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 2 routes
04:56:48 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:56:49 CATALYST-OSPF-R1 sev6 %OSPF-6-DRCHG: DR Change from 0.0.0.0 to 10.61.122.1 on GigabitEthernet0/0
04:56:49 CATALYST-OSPF-R1 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.122.1 on GigabitEthernet from LOADING to FULL, Loading Done
04:56:49 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:56:49 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:56:49 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
```

**アラート**(0 件)


**経路の変化(情 INFO)**(12 件)

- CATALYST-OSPF-R1 add O 10.61.122.0/24 via 10.61.121.2
- CATALYST-OSPF-R1 add O 10.61.123.0/24 via 10.61.121.2
- CATALYST-OSPF-R1 add O 10.61.151.0/24 via 10.61.121.2
- CATALYST-OSPF-R2 add O 10.61.123.0/24 via 10.61.122.2
- CATALYST-OSPF-R2 add O 10.61.150.0/24 via 10.61.121.1 [GigabitEthernet1/0/1]
- CATALYST-OSPF-R2 add O 10.61.151.0/24 via 10.61.122.2
- CATALYST-OSPF-R3 add O 10.61.121.0/24 via 10.61.122.1 [GigabitEthernet1/0/1]
- CATALYST-OSPF-R3 add O 10.61.150.0/24 via 10.61.122.1 [GigabitEthernet1/0/1]
- CATALYST-OSPF-R3 add O 10.61.151.0/24 via 10.61.123.2 [GigabitEthernet1/0/2]
- CATALYST-OSPF-R4 add O 10.61.121.0/24 via 10.61.123.1 [GigabitEthernet1/0/1]
- CATALYST-OSPF-R4 add O 10.61.122.0/24 via 10.61.123.1 [GigabitEthernet1/0/1]
- CATALYST-OSPF-R4 add O 10.61.150.0/24 via 10.61.123.1 [GigabitEthernet1/0/1]

### 障害(04:57:13)

CATALYST-OSPF-R3 の GigabitEthernet1/0/2 を落とした状態

**状態ボード**: network=crit「リンク断 1本」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 1 ・ warning 0 ・ 全 57件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| CATALYST-OSPF-R1 | CATALYST-OSPF-R2 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R2 | CATALYST-OSPF-R3 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R3 | CATALYST-OSPF-R4 | down | はい | STATE:down, SYSLOG:down |

`CATALYST-OSPF-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.61.121.0/24 is directly connected, GigabitEthernet1/0/2
L        10.61.121.1/32 is directly connected, GigabitEthernet1/0/2
C        10.61.150.0/24 is directly connected, GigabitEthernet1/0/3
L        10.61.150.1/32 is directly connected, GigabitEthernet1/0/3
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
O        10.61.122.0/24 [110/20] via 10.61.121.2, GigabitEthernet1/0/2
```

`CATALYST-OSPF-R3# show ip ospf neighbor`

```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.61.122.1     1     Full/DROther    00:00:35    10.61.122.1     GigabitEthernet1/0/1
```

**ログ**(/cyber-view が見ているもの、8 件)

```
04:57:02 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:57:02 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:57:02 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.2 interface down, changing state from FULL to DOWN
04:57:02 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:02 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:02 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:02 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:02 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
```

**アラート**(2 件)

- [medium/SYSLOG] 装置が error 以上のログを出力 — CATALYST-OSPF-R3: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
- [medium/NET] リンク断 — CATALYST-OSPF-R3: GigabitEthernet1/0/2 が down (catalyst-ospf-r3 - catalyst-ospf-r4)

**経路の変化(情 INFO)**(6 件)

- CATALYST-OSPF-R1 del O 10.61.123.0/24 via 10.61.121.2
- CATALYST-OSPF-R1 del O 10.61.151.0/24 via 10.61.121.2
- CATALYST-OSPF-R2 del O 10.61.123.0/24 via 10.61.122.2
- CATALYST-OSPF-R2 del O 10.61.151.0/24 via 10.61.122.2
- CATALYST-OSPF-R3 del C 10.61.123.0/24 [GigabitEthernet1/0/2]
- CATALYST-OSPF-R3 del O 10.61.151.0/24 via 10.61.123.2 [GigabitEthernet1/0/2]

### 復旧(04:57:21)

戻した状態

**状態ボード**: network=ok「リンク 3本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 1 ・ warning 0 ・ 全 68件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| CATALYST-OSPF-R1 | CATALYST-OSPF-R2 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R2 | CATALYST-OSPF-R3 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R3 | CATALYST-OSPF-R4 | ok | - | STATE:up, SYSLOG:up |

`CATALYST-OSPF-R1# show ip route`

```
Codes: L - local, C - connected, S - static, R - RIP, B - BGP,
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area,
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2,
       E1 - OSPF external type 1, E2 - OSPF external type 2,
       i - IS-IS, * - candidate default, U - per-user static route

Gateway of last resort is not set

C        10.61.121.0/24 is directly connected, GigabitEthernet1/0/2
L        10.61.121.1/32 is directly connected, GigabitEthernet1/0/2
C        10.61.150.0/24 is directly connected, GigabitEthernet1/0/3
L        10.61.150.1/32 is directly connected, GigabitEthernet1/0/3
C        192.168.10.0/24 is directly connected, Vlan10
L        192.168.10.1/32 is directly connected, Vlan10
O        10.61.122.0/24 [110/20] via 10.61.121.2, GigabitEthernet1/0/2
O        10.61.123.0/24 [110/30] via 10.61.121.2, GigabitEthernet1/0/2
O        10.61.151.0/24 [110/40] via 10.61.121.2, GigabitEthernet1/0/2
```

`CATALYST-OSPF-R3# show ip ospf neighbor`

```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.61.122.1     1     Full/DROther    00:00:37    10.61.122.1     GigabitEthernet1/0/1
10.61.123.2     1     Full/DR         00:00:36    10.61.123.2     GigabitEthernet1/0/2
```

**ログ**(/cyber-view が見ているもの、11 件)

```
04:57:14 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:57:14 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
04:57:14 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:14 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:14 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:14 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:19 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.2 on GigabitEthernet from LOADING to FULL, Loading Done
04:57:19 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:19 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:19 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:19 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
```

**アラート**(0 件)


**経路の変化(情 INFO)**(6 件)

- CATALYST-OSPF-R1 add O 10.61.123.0/24 via 10.61.121.2
- CATALYST-OSPF-R2 add O 10.61.123.0/24 via 10.61.122.2
- CATALYST-OSPF-R3 add C 10.61.123.0/24 [GigabitEthernet1/0/2]
- CATALYST-OSPF-R1 add O 10.61.151.0/24 via 10.61.121.2
- CATALYST-OSPF-R2 add O 10.61.151.0/24 via 10.61.122.2
- CATALYST-OSPF-R3 add O 10.61.151.0/24 via 10.61.123.2 [GigabitEthernet1/0/2]

### フラップ(04:57:28)

落とす/戻すを2回繰り返した直後

**状態ボード**: network=ok「リンク 3本 すべて up」 / traffic=ok「最大使用率 0.0% ・ 通信なし」 / security=ok「アラートなし」 / syslog=crit「error以上 3 ・ warning 0 ・ 全 85件 ・ フラップ 5件」

**リンク**

| A | B | 状態 | LINK DOWN | 検知経路 |
|---|---|---|---|---|
| CATALYST-OSPF-R1 | CATALYST-OSPF-R2 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R2 | CATALYST-OSPF-R3 | ok | - | STATE:up, SYSLOG:up |
| CATALYST-OSPF-R3 | CATALYST-OSPF-R4 | ok | - | STATE:up, SYSLOG:up |

`CATALYST-OSPF-R3# show ip ospf neighbor`

```
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.61.122.1     1     Full/DROther    00:00:31    10.61.122.1     GigabitEthernet1/0/1
```

**ログ**(/cyber-view が見ているもの、17 件)

```
04:57:21 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:57:21 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:57:21 CATALYST-OSPF-R3 sev5 %OSPF-5-ADJCHG: Process 1, Nbr 10.61.123.2 interface down, changing state from FULL to DOWN
04:57:21 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:21 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:21 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:21 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 3 routes
04:57:21 CATALYST-OSPF-R4 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 5 routes
04:57:21 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:57:21 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
04:57:21 CATALYST-OSPF-R1 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:21 CATALYST-OSPF-R2 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:21 CATALYST-OSPF-R3 sev6 %OSPF-6-LOGADJ: Process 1: SPF calculation complete, 4 routes
04:57:21 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to down
04:57:21 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to down
04:57:21 CATALYST-OSPF-R3 sev3 %LINK-3-UPDOWN: Interface GigabitEthernet1/0/2, changed state to up
04:57:21 CATALYST-OSPF-R3 sev5 %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet1/0/2, changed state to up
```

**アラート**(7 件)

- [warning/SYSLOG] インタフェースのフラップ — CATALYST-OSPF-R3: GigabitEthernet1/0/2: 60秒に 3 回 状態変化
- [warning/SYSLOG] OSPFネイバーのフラップ — CATALYST-OSPF-R3: 10.61.123.2: 120秒に 3 回 状態変化
- [medium/NET] リンク断 — CATALYST-OSPF-R3: GigabitEthernet1/0/2 が down (catalyst-ospf-r3 - catalyst-ospf-r4)
- [medium/NET] リンク断 — CATALYST-OSPF-R3: GigabitEthernet1/0/2 が down (catalyst-ospf-r3 - catalyst-ospf-r4)
- [warning/NET] 経路のフラップ — CATALYST-OSPF-R1: 10.61.151.0/24: 120秒に 4 回 追加/削除/変更
- [warning/NET] 経路のフラップ — CATALYST-OSPF-R2: 10.61.151.0/24: 120秒に 4 回 追加/削除/変更
- [warning/NET] 経路のフラップ — CATALYST-OSPF-R3: 10.61.151.0/24: 120秒に 4 回 追加/削除/変更

**経路の変化(情 INFO)**(3 件)

- CATALYST-OSPF-R1 del O 10.61.151.0/24 via 10.61.121.2
- CATALYST-OSPF-R2 del O 10.61.151.0/24 via 10.61.122.2
- CATALYST-OSPF-R3 del O 10.61.151.0/24 via 10.61.123.2 [GigabitEthernet1/0/2]

