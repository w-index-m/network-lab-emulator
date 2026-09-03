# VRRP / HSRP ゲートウェイ冗長化 — 動作試験と修正

`engine/protocols.py` の `VrrpEngine`（VRRPとHSRPの両方を実装）

## 試験前の状態

`VrrpEngine` は選出・Helloループ・Deadタイマー・フェイルオーバー・
表示整形まで一通り実装されていたが、**テストが1件も無く**、実際に
2台構成で動かすと機能していなかった。

## 発見したバグ（4件、すべて修正済み）

### 1. Hello/Advertが受信側エンジンに渡らない（スプリットブレイン）

`VirtualNetwork.send_to()` はメッセージtypeごとに受信エンジンへ
ディスパッチするが、その表に **`vrrp_advert` と `hsrp_hello` が
入っていなかった**。

```python
if msg_type == 'rip_packet':      await rip_engine.receive(...)
elif msg_type == 'ospf_hello':    await ospf_engine.receive_hello(...)
elif msg_type == 'stp_bpdu':      await stp_engine.receive_bpdu(...)
# vrrp_advert / hsrp_hello がここに無い → 表示用ログ扱いで捨てられる
```

`broadcast_to_neighbors()` 側の「同一セグメントのみ届く」リストには
両方が入っていたため、パケットは送信されるが**誰も受け取らない**状態
だった。結果、両系ともAdvert/Helloを聞けず、**両方がMaster/Active**を
名乗るスプリットブレインになっていた。

```
（修正前）
catalyst# show standby → State is Active   ← priority 110
cisco#    show standby → State is Active   ← priority 100 なのにActive
```

→ `send_to()` に両typeのディスパッチを追加。

### 2. 対向の情報を保持しておらず表示が常に unknown

`VrrpGroup` / `HsrpGroup` に相手を記録するフィールドが無く、
`format_show_standby()` は `Standby router is unknown` を
**ハードコード**していた。

→ `peer_id` / `peer_ip` / `peer_priority` を追加し、Advert/Hello受信時
に記録して表示するようにした。

### 3. 対向IPの解決が別セグメントのIPを返す

`_get_device_ip()` は装置が持つIPのうち単にソート順で最大のものを
返すため、複数セグメントに足を持つ装置では冗長化を組んでいる
セグメントと無関係のIP（ループバック等）が表示されていた。

```
（修正前）Standby router is 192.168.1.1  ← ciscoのloopback。実際は 10.9.9.2
```

→ VIPと同じサブネットに属するIPを選ぶ `_get_device_ip_for_vip()` を
追加。VRRPの同priority時のIP比較（RFC 5798はVRRPインターフェースの
主IPで比較する）にも使うようにした。

**注意**: VIPは必ず実インターフェースと同じサブネット内に設定すること。
`10.9.9.1/30` の対向で VIP に `10.9.9.254` を指定すると、`/30` は
`.0〜.3` しか含まないためVIPがサブネット外になり、対向IPの解決に
失敗する（試験時に実際にこれで引っかかった）。

### 4. インターフェースをshutdownしてもInitに落ちない

`shutdown` 時、OSPF・RIP・STP・BGP・LACPにはエンジン通知が飛んでいたが
**VRRP/HSRPには通知が無かった**。そのため切れた側がMaster/Activeを
名乗り続け、対向がDeadタイマーで昇格した結果、また両系Master/Active
になっていた。

→ `VrrpEngine.interface_down()` / `interface_up()` を実装し、
`app.py` の shutdown / no shutdown ハンドラから呼ぶようにした。

### 4b. downしたIFで対向のHelloを受信し続ける（4の修正が効かない原因）

上記4を実装しても状態が戻ってしまった。原因は
`broadcast_to_neighbors()` が**送信側のdownしか見ていなかった**こと。
自分のIFをshutdownした装置でも対向からのHelloは届き続けるため、
Initに落とした直後に受信→再選出でActiveに戻っていた。

`_edge_up()`（リンク両端のdownを見る既存関数）が既にあったので、
これを `broadcast_to_neighbors()` でも使うようにした。実機でも
リンク片端がshutならその区間は疎通しないので、この方が正しい。

## 動作確認（2026-09-03、catalyst ↔ cisco 直結）

前提: 両者を同じサブネットに置く（VIPを含められるよう `/24`）。

```
! catalyst (Active/Master 側)
interface GigabitEthernet1/0/1
 ip address 10.9.9.1 255.255.255.0
 standby 1 ip 10.9.9.254
 standby 1 priority 110
 standby 1 preempt
 vrrp 2 ip 10.9.9.253
 vrrp 2 priority 120

! cisco (Standby/Backup 側)
interface GigabitEthernet0/0/0
 ip address 10.9.9.2 255.255.255.0
 standby 1 ip 10.9.9.254
 vrrp 2 ip 10.9.9.253
```

### HSRP

```
catalyst# show standby
GigabitEthernet1/0/1 - Group 1
  State is Active
  Priority 110 (configured 110)
  Active router is local
  Standby router is 10.9.9.2, priority 100

cisco# show standby
  State is Standby
  Active router is 10.9.9.1, priority 110
  Standby router is local
```

フェイルオーバー（catalyst の IF を shutdown → no shutdown）:

| 手順 | catalyst | cisco |
|---|---|---|
| 正常時 | Active | Standby |
| catalyst の IF を shutdown | **Init** | **Active** |
| no shutdown（preempt） | **Active** | **Standby** |

### VRRP

```
cisco# show vrrp
GigabitEthernet0/0/0 - Group 2
  State is Backup
  Virtual IP address is 10.9.9.253
  Virtual MAC address is 00:00:5e:00:01:02
  Priority is 100
  Master Router is 10.9.9.1, priority is 120
```

| 手順 | catalyst | cisco |
|---|---|---|
| 正常時 | Master | Backup |
| catalyst の IF を shutdown | **Init** | **Master** |
| no shutdown（preempt） | **Master** | **Backup** |

## テスト

`tests/test_vrrp_hsrp.py`（7件）。上記4バグそれぞれに対応する回帰
テストを含む。

```bash
pytest tests/test_vrrp_hsrp.py -v
```

Helloの周期とDeadタイマーの実時間待ちがあるため、実行に40秒程度かかる。
