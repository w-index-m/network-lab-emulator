# IPv6アドレッシング/ルーティング (v1)

## 背景

ユーザーから「ネットワークラボの改善点ありますか？」と聞かれ、既知の
課題の一つとして「IPv6ルーティングが完全に未実装 — `engine/rules.py`の
"ipv6"言及37件は全部見た目だけの固定応答で、実ルーティングが無い」を
提案し、実装を依頼された。

調べたところ、`ipv6 address <addr>/<prefix>`(interface config)と
`ipv6 unicast-routing`は既に`engine/rules.py`で
`state.interfaces[if]['ipv6']`/`['ipv6_prefix']`へ実際に反映されており、
`show running-config`/`show ipv6 interface brief`にも出ていた——つまり
アドレッシングのCLI/表示層は既にあった。無かったのは「そこから先」:

1. `ipv6 route <dest>/<len> <next-hop>` スタティック経路コマンド自体が
   存在しない
2. `show ipv6 route` が無い
3. ping/tracerouteがIPv6アドレス(コロンを含む宛先)を一切認識しない
   (正規表現がIPv4のドット区切り数字限定だった)

## スコープ

IPv4の`IcmpEngine`/`RibEngine`が持つACL/NAT/VRF/ICMP Redirect等の機能は
スコープ外(v6プラス/MAP-E実装時と同じ、現実的なカット)。実装したのは:

- `engine/protocols.py`に新設`Ipv6Engine`(`ipv6_engine`シングルトン)
  - `register_device()`: 装置のIPv6アドレスから直結経路を自動導出
  - `add_static_route()`/`remove_static_route()`: `ipv6 route`用
  - `trace_path()`: 実vnetトポロジー(`vnet.get_neighbors()`)のBFSによる
    マルチホップ到達性判定。直結チェック→スタティック経路の再帰的な
    次ホップ解決→隣接関係の実在確認、という順で辿る
  - `ping()`/`traceroute()`: IPv4の`IcmpEngine`と同じ戻り値の形
    (`{'reachable','reason','ttl','rtts','count','hops'}`)にして、
    既存の`_format_ping`/`_format_traceroute`をそのまま再利用できる
    ようにした
- `app.py`:
  - `_register_icmp()`がIPv6インタフェースも`ipv6_engine`へ登録
  - `ipv6 route`/`no ipv6 route`のCLIハンドラ
  - `show ipv6 route`(Codes: C/S形式)
  - `ping`/`traceroute`にIPv6宛先(コロンを含む、`ping ipv6 <addr>`形式
    も許容)を認識する分岐を追加
  - 既存の「設定コマンド実行後にicmp_engineへ再登録する」トリガー正規表現
    に`ipv6\s+addr(?:ess)?`/`ipv6\s+route`を追加(無いと設定した
    IPv6アドレスがいつまでも`ipv6_engine`に反映されないバグがあった—
    実装直後にテストで踏んで修正)

## ライブ検証

実際に動いているインスタンス(`NETLAB_AUTH_DISABLE=1`)に対し、2台の
Cisco装置をリンクし、同一サブネットのIPv6アドレスを設定:

```
R1: interface GigabitEthernet0/0/0 / ipv6 address 2001:db8:ffff::1/64
R2: interface GigabitEthernet0/0/0 / ipv6 address 2001:db8:ffff::2/64
```

`show ipv6 interface brief`(R1):
```
GigabitEthernet0/0/0    [up/up]
    2001:db8:ffff::1
```

`show ipv6 route`(R1、直結経路が実際に導出されている):
```
IPv6 Routing Table - default - 1 entries
Codes: C - Connected, L - Local, S - Static
C   2001:db8:ffff::/64 [0/0]
     via GigabitEthernet0/0/0, directly connected
```

R1からR2への実ping(到達):
```
Type escape sequence to abort.
Sending 5, 100-byte ICMP Echos to 2001:db8:ffff::2, timeout is 2 seconds:
!!!!!
Success rate is 100 percent (5/5), round-trip min/avg/max = 1/1/2 ms
```

traceroute(ホップ先のホスト名が正しくR2になることを確認——最初の実装は
同一サブネット直結到達の場合に送信元自身のホスト名を表示してしまう
バグがあり、このライブ検証で見つけて修正した):
```
traceroute to 2001:db8:ffff::2 (2001:db8:ffff::2), 30 hops max, 40 byte packets
 1  live-v6-r2 (2001:db8:ffff::2)  2.902 ms  2.384 ms  0.726 ms
```

存在しない宛先への到達不可(フェイクの成功を作っていないことの確認):
```
Type escape sequence to abort.
Sending 5, 100-byte ICMP Echos to 2001:db8:dead::9, timeout is 2 seconds:
.....
Success rate is 0 percent (0/5)
```

マルチホップ(R1 -- MID -- R2、スタティック経路でMID経由にR2側ネットへ
到達)、およびスタティック経路を入れない場合は到達しないことも
`tests/test_ipv6_routing.py`の`TestIpv6MultiHopReachability`で固定。

## テスト

`tests/test_ipv6_routing.py`(12 tests): アドレス設定の表示反映、直結
経路の自動導出、スタティック経路の追加/削除反映、直結ピアへのping/
traceroute成功、未知の宛先への失敗、IPv4側のping処理に影響していない
ことの固定、マルチホップ到達性(スタティック経路あり/なし)。

## 既知の制約(スコープ外)

- OSPFv3/BGP IPv6 AFI等の動的ルーティングプロトコルは対象外(直結+
  スタティックのみ)
- ACL/NAT66/VRF/ICMP Redirect/近隣探索(ND)の詳細な状態機械は対象外
- `ipv6 unicast-routing`の有無による到達性のゲートは行っていない
  (設定の反映自体は表示されるが、v1では到達性判定に影響しない——
  pragmaticな割り切り)
