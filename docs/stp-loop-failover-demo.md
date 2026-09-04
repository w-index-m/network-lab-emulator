# Catalyst 3台ループ構成での STP トポロジーチェンジ検証

Catalyst 3台（`catalyst` / `cat` / `catalyst-test`）だけでトライアングル
ループを組み、Rapid-PVST+ でループを防いだ状態から1リンクに疑似ケーブル
障害（`shutdown`）を起こして、STP が再収束する様子を作業前後の
`show spanning-tree` で確認した記録。

> `cat` には元々 `sir` とのリンクがあり、最初の検証ではそれが
> 3台目の"ポート"としてSTPの出力に混ざってしまっていた（`show
> spanning-tree` に本来無関係なリンクが3本目として出る）。
> **今回は `sir`⟷`cat` 間のリンクを検証中だけ切り離し、
> 純粋にCatalyst 3台だけのループで取り直した。**

## 構成

```
                catalyst(Dist-SW)
        Gi1/0/10 ┃              ┃ Gi1/0/12
                 ┃              ┃
   cat(Catalyst-SW) ━━━━━━━━━━ catalyst-test(CAT-MODIFIED)
              Gi1/0/11        Gi1/0/11
```

| リンク | 片側 | もう片側 |
|---|---|---|
| catalyst ⟷ cat | Gi1/0/10 | Gi1/0/10 |
| cat ⟷ catalyst-test | Gi1/0/11 | Gi1/0/11 |
| catalyst-test ⟷ catalyst | Gi1/0/12 | Gi1/0/12 |

3台とも `spanning-tree mode rapid-pvst` で Rapid-PVST+ を起動している。
（`sir` は検証の間だけ `cat` から切り離し、終了後に元通り接続した。）

## 手順

```
! 3台をトライアングルに接続後、各インターフェースを起動
catalyst(config)# interface GigabitEthernet1/0/10
catalyst(config-if)# no shutdown
catalyst(config-if)# exit
catalyst(config-if)# interface GigabitEthernet1/0/12
catalyst(config-if)# no shutdown
catalyst(config-if)# exit
catalyst(config)# spanning-tree mode rapid-pvst

cat(config)# interface GigabitEthernet1/0/10
cat(config-if)# no shutdown
cat(config-if)# exit
cat(config-if)# interface GigabitEthernet1/0/11
cat(config-if)# no shutdown
cat(config-if)# exit
cat(config)# spanning-tree mode rapid-pvst

catalyst-test(config)# interface GigabitEthernet1/0/11
catalyst-test(config-if)# no shutdown
catalyst-test(config-if)# exit
catalyst-test(config-if)# interface GigabitEthernet1/0/12
catalyst-test(config-if)# no shutdown
catalyst-test(config-if)# exit
catalyst-test(config)# spanning-tree mode rapid-pvst
```

## 作業前: `show spanning-tree`（ループがブロックされている状態）

`cat` がルートブリッジとなり、`catalyst` の `catalyst-test` 方向
（Gi1/0/12）が **Alternate/Blocking** となって、ループが1か所で
正しく遮断されている。

```
===== catalyst =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             Cost        19
             Hello Time  2 sec  Max Age 20 sec  Forward Delay 15 sec

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0ef4.43c1
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  0
             Time since topology change never

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 1             Root  FWD 19        128.1    P2p   ← Gi1/0/10 (→cat)
ether 2             Altn  BLK 19        128.1    P2p   ← Gi1/0/12 (→catalyst-test)

===== cat =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             This bridge is the root

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0e11.0d04
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  0
             Time since topology change never

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 1             Desgn FWD 19        128.1    P2p   ← Gi1/0/10 (→catalyst)
ether 2             Desgn FWD 19        128.1    P2p   ← Gi1/0/11 (→catalyst-test)

===== catalyst-test =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             Cost        19
             Hello Time  2 sec  Max Age 20 sec  Forward Delay 15 sec

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0e1d.7ee2
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  0
             Time since topology change never

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 1             Root  FWD 19        128.1    P2p   ← Gi1/0/11 (→cat)
ether 2             Desgn FWD 19        128.1    P2p   ← Gi1/0/12 (→catalyst)
```

## 疑似ケーブル障害の発生

`catalyst` の Root ポート（`cat` 方向 = Gi1/0/10、ルートブリッジへの
直結リンク）を `shutdown` して、リンク断を模擬する。

```
catalyst(config)# interface GigabitEthernet1/0/10
catalyst(config-if)# shutdown
```

## 作業後: `show spanning-tree`（再収束後）

- `catalyst` の Root ポートが無くなり（インターフェースdownで除去）、
  それまで **Blocking だったポート（Gi1/0/12）が Root/Forwarding に
  昇格**した
- `catalyst` の Root Path Cost が `19` → `38` に変化
  （`catalyst-test` 経由の迂回路になったため）
- 3台とも **Topology Change count が増加**し、再収束が起きたことが
  記録に残っている

```
===== catalyst =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             Cost        38
             Hello Time  2 sec  Max Age 20 sec  Forward Delay 15 sec

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0ef4.43c1
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  1
             Time since topology change 00:03

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 2             Root  FWD 19        128.1    P2p   ← Gi1/0/12、BLK→FWDに昇格

===== cat =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             This bridge is the root

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0e11.0d04
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  1
             Time since topology change 00:03

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 2             Desgn FWD 19        128.1    P2p   ← Gi1/0/11 (→catalyst-test)

===== catalyst-test =====
VLAN0001
  Spanning tree enabled protocol rstp
  Root ID    Priority    32769
             Address     000e.0e11.0d04
             Cost        19
             Hello Time  2 sec  Max Age 20 sec  Forward Delay 15 sec

  Bridge ID  Priority    32769  (priority 32768 sys-id-ext 1)
             Address     000e.0e1d.7ee2
             Hello Time  1 sec  Max Age 20 sec  Forward Delay 15 sec
             Topology Change count  2
             Time since topology change 00:03

Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 1             Root  FWD 19        128.1    P2p   ← Gi1/0/11 (→cat)
ether 2             Desgn FWD 19        128.1    P2p   ← Gi1/0/12、catalystの迂回経路
```

（`cat` からは切断されたリンクが見えないだけで、自分自身がRootの
ため元々全ポート Designated/Forwarding のまま変化なし。TC count が
1増えているのは `catalyst` からのTCN受信によるもの。）

## 復旧後の確認（`no shutdown`）

```
catalyst(config)# interface GigabitEthernet1/0/10
catalyst(config-if)# no shutdown
```

```
Interface           Role Sts Cost      Prio.Nbr Type
--------------------------------------------------------------------
ether 2             Altn  BLK 19        128.1    P2p
ether 1             Root  FWD 19        128.1    P2p
```

障害前と同じ Root/Alternate の役割に復元され、Root Path Cost も
`19` に戻ることを確認した。

## まとめ

| 項目 | 作業前 | 障害後 | 復旧後 |
|---|---|---|---|
| `catalyst` の Root Port | Gi1/0/10 (cost 19) | Gi1/0/12 経由 (cost 38、迂回) | Gi1/0/10 (cost 19、復元) |
| Gi1/0/12（catalyst側） | Alternate/Blocking | **Root/Forwarding に昇格** | Alternate/Blocking に復帰 |
| Topology Change count（catalyst / cat / catalyst-test） | 0 / 0 / 0 | 1 / 1 / 2 | 変化なし（すでに再収束済み） |
| 通信 | 3台とも到達可能（1本blockで冗長） | **リンク1本切れても冗長経路で到達可能を維持** | 通常構成に復帰 |

ループ構成 + Rapid-PVST+ により、1本のケーブル障害が発生しても
瞬時に予備経路へ切り替わり、通信断（ブラックホール）にならないことを
確認できた。

## この検証中に見つけたバグ（修正済み）

障害復旧（`no shutdown`）を試したところ、**リンクを戻したはずの
ポートが `show spanning-tree` から1本消える**という不具合を見つけた。

原因は `StpEngine.port_up()` のポート名採番だった。障害時に
`port_down()` がポートを辞書から削除して欠番を作るため、復旧時に
`len(ports) + 1` で採番すると**既存の別ポート名と衝突し、無関係な
接続先のポート情報を上書きして消してしまっていた**。3台ループで
1本落として戻すと、物理的には3本とも繋がっているのに STP 上は
2本しか見えなくなる——今回の一連のセッションで見てきた
「表示と実態の食い違い」と同じ系統の不具合だった。

既存の空き番号を再利用しないよう `port_up()` の採番方法を修正し、
本ドキュメントの「復旧後の確認」で正しく2ポートとも復元されることを
確認した。

## 分かっている制約（表示上の注意点）

`show spanning-tree` のインターフェース列は内部的に `ether 1` /
`ether 2` という通し番号で表示され、**実際の `GigabitEthernet1/0/xx`
とは連動していない**。物理インターフェースとの対応は本ドキュメントの
「構成」表、または `show cdp neighbors` の Local Intrfce 列を
併用して確認する必要がある。
