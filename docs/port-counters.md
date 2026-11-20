# 各機器のポートのカウンタ(SNMP IF-MIB / show interfaces / gNMI)

すべての機器のポートで、SNMP の IF-MIB(ifTable・ifXTable)が実機と同じ列をそろえて返します。
CLI の `show interfaces`・`show interfaces counters`・`show interfaces counters errors` も、
gNMI の `/interfaces/interface/state/counters` も、同じカウンタ(`engine.protocols.dp_engine`)を読むので、
どれで見ても値が一致します。

## 何が数えられるか

| 種類 | どこから増えるか | IF-MIB の列 |
|---|---|---|
| ユニキャスト | ping(`ping X repeat N size S`、PC の `ping -c N -s S`) | ifInUcastPkts(.11)/ ifHCInUcastPkts(31…7)ほか |
| マルチキャスト | STP BPDU・OSPF Hello・CDP・LLDP(背景で流れ続ける。下の表) | ifHCInMulticastPkts(31…8)/ ifInMulticastPkts(31…2)ほか |
| ブロードキャスト | L2 ループのブロードキャストストーム | ifHCInBroadcastPkts(31…9)/ ifInBroadcastPkts(31…3)ほか |
| 破棄 | storm-control broadcast level で捨てた分 | ifInDiscards(.13) |
| エラー | `dp_engine.drop(..., reason='crc'|'giant'|'runt'|'error')` | ifInErrors(.14)、show の CRC・giants・runts |

- ifInNUcastPkts(.12)/ ifOutNUcastPkts(.18)は「マルチキャスト + ブロードキャスト」。
- 捨てたフレームは実機と同じく octets・packets input には入れません。
- `show interfaces` の `Received N broadcasts (M multicasts)` の N は、実機どおりマルチキャストを含みます。

### 背景の制御トラフィック(`engine/control_traffic.py`)

実機の IF カウンタは、何もしなくても少しずつ増えます。以前のエミュレータは ping のときしか動かず、
SNMP で見ると「何も流れていないリンク」に見えていました。実機の既定の間隔で、送る側の out と相手側の in に
同じ数を積みます。どちらかのポートが shutdown なら積みません。

| フレーム | 間隔 | 大きさ | 条件 |
|---|---|---|---|
| STP BPDU | 2 秒 | 64 B | STP が有効で、そのポートが Designated |
| OSPF Hello | hello-interval(既定 10 秒) | 98 B | OSPF が有効で、相手が OSPF の隣接 |
| CDP | 60 秒 | 400 B | cisco / catalyst / nexus / wlc |
| LLDP | 30 秒 | 300 B | `lldp run` 済み、または既定で有効な機種(arista / juniper) |

1 秒ごとに積みます。小数ぶんは持ち越すので、平均の数は間隔どおりです。
止めるには `NETLAB_CTRL_TRAFFIC=0`(テストは conftest で止めて、ping の分だけを数えています)。

## IF-MIB の列

ifTable(1.3.6.1.2.1.2.2.1)

| 列 | 名前 | 値 |
|---|---|---|
| .3 | ifType | 6 ethernetCsmacd / 24 ループバック / 53 SVI(Vlan) / 131 トンネル / 161 Port-channel |
| .4 | ifMtu | 1500(ループバックは 1514) |
| .5 | ifSpeed | IF の種類どおり(Gi=1G、Te=10G、NX-OS の Ethernet=10G、Fa=100M …) |
| .6 | ifPhysAddress | 装置と IF ごとに固定の MAC(ループバックは無し) |
| .7 / .8 | ifAdminStatus / ifOperStatus | shutdown なら down。L2 ポートはケーブルが無ければ oper down |
| .9 | ifLastChange | 最後に up/down した時の sysUpTime(自分のポートとリンクの相手のポート) |
| .10〜.20 | octets・ucast・NUcast・discards・errors・unknown protos | 32 ビット(2^32 で折り返す) |

ifXTable(1.3.6.1.2.1.31.1.1.1)

| 列 | 名前 |
|---|---|
| .1 | ifName(短縮名 Gi1/0/1 等) |
| .2〜.5 | ifIn/OutMulticastPkts・ifIn/OutBroadcastPkts(32 ビット) |
| .6〜.13 | ifHCIn/OutOctets・Ucast・Multicast・Broadcast(64 ビット) |
| .14 | ifLinkUpDownTrapEnable(L3 は有効、L2 アクセスポートは無効) |
| .15 | ifHighSpeed(Mbps) |
| .16 / .17 | ifPromiscuousMode(false)/ ifConnectorPresent(物理ポートは true) |
| .18 | ifAlias(description) |
| .19 | ifCounterDiscontinuityTime(0) |

cisco / catalyst / nexus / arista / juniper / yamaha / fortigate で、装置のすべてのポートが ifTable に出ることを
テストで確かめています(`tests/test_port_counters_all_devices.py`)。

## gNMI

`/interfaces/interface[name=X]/state/counters` は、in/out の octets・pkts・unicast・multicast・broadcast・
errors・discards、in-fcs-errors、in-unknown-protos を返します。`state/last-change` は最後に up/down した時刻です
(UNIX 時刻のナノ秒)。uint64 は RFC 7951 どおり文字列で返します。

## ライブ確認(2026-11-20)

本物のサーバー(`uvicorn app:app`)に Catalyst 2 台(`spanning-tree mode rapid-pvst`)と PC 1 台をつなぎ、
本物の net-snmp(`snmpwalk` / `snmpget`)で読みました。

アップリンク Gi1/0/1。何もせず 12 秒待つと、BPDU が 2 秒ごとに 6 個(64 B × 6 = 384 B)増えました。

```
== Gi1/0/1 (STP uplink to L-SW2) t0
31.1.1.1.6   320          ifHCInOctets
31.1.1.1.8   5            ifHCInMulticastPkts
== Gi1/0/1 after 12s idle
2.2.1.4      1500         ifMtu
2.2.1.5      1000000000   ifSpeed
2.2.1.8      1            ifOperStatus up
31.1.1.1.6   704
31.1.1.1.7   0            ifHCInUcastPkts(ping はしていない)
31.1.1.1.8   11
31.1.1.1.15  1000         ifHighSpeed
2.2.1.13     0            ifInDiscards
2.2.1.14     0            ifInErrors
```

PC につながった Gi1/0/4。`ping -c 10 -s 1000` の後は、ユニキャストだけが 10 個(1028 B × 10)増えました。

```
31.1.1.1.6   10280
31.1.1.1.7   10
31.1.1.1.8   0
```

gNMI でも同じ値が読めることは、`gnmic get` / `gnmic subscribe --sample-interval 1s` で確認しました
(1 秒ごとに届き、ping の分だけ in-octets が増える)。

### このとき見つけた、別の既存の問題(未修正)

Catalyst 2 台を同じ優先度(32769)で `spanning-tree mode rapid-pvst` にすると、両方とも
「This bridge is the root」のままで、ルートが 1 台に決まりませんでした(MAC が小さい方がルートになるはず)。
片方の優先度を下げた 3 台の三角形(`tests/test_stp_late_links.py`)では Blocking まで決まります。
カウンタとは別の問題なので、今回は直していません。
