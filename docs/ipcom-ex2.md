# IPCOM EX2（富士通/PFU系UTMアプライアンス）

`device_type: "ipcom"`

## これは何か

ユーザーからアップロードされたIPCOM EX2シリーズのマニュアル
（取扱説明書、コマンドリファレンス、アンチウィルス移行ガイド、事例集）
をもとに、IPCOM EX2の基本CLI操作をエミュレータに追加したもの。

「作成範囲をどこまでにするか」をユーザーに確認したところ**中規模**
（ログイン/hostname/show系の基本コマンドに加え、IPCOM特有の
`load`/`new`編集モードと`running-config`/`startup-config`への保存
フローも再現する）を選択された。マニュアルの中心テーマだった
アンチウィルス機能（`rule virus`、クラウドサンドボックス検査等）は
**スコープ外**（実装していない）。

## IPCOM EX2 のCLIは他機種と根本的に違う

これまで実装してきたCisco系（`ipcom>` → `enable` → `configure terminal`
→ `interface X`という単純な階層）とは異なり、IPCOM EX2は
「即時モード」と「編集モード」が分かれている、実機マニュアル
（P3NK-6002）準拠の独自体系:

```
ipcom>              操作者EXEC（ログイン直後）
  ↓ admin
ipcom#               管理者EXEC
  ↓ configure terminal   ※ admin昇格していないと % Authorization failed
ipcom(config)#       グローバル構成定義(即時) — 主にファイル選択用
  ↓ load running-config / load startup-config / new
ipcom(edit)#         グローバル構成定義(編集) — 実際の設定はここ
  ↓ interface lan0.0
ipcom(edit-if)#      インターフェース構成定義(編集)
```

実機は`edit`モードでの変更を`save running-config`/
`save startup-config`で明示的に反映するまでバッファに留めるが、
このエミュレータでは他機種同様コマンド投入時点で即座に`state`へ
反映する簡略化をしている（`save`/`commit`はACKメッセージのみ返す）。

## インターフェース命名

マニュアル準拠で`lan<N>.<VLAN-ID>`形式（例: `lan0.0`、`lan0.1`）。
デフォルトで`lan0.0`（192.168.1.1/24）、`lan0.1`
（192.168.100.1/24）の2つを持つ。

## 対応コマンド

- `admin` — 管理者EXECへ昇格
- `configure terminal` — グローバル構成定義(即時)へ（要admin昇格）
- `load running-config` / `load startup-config` / `new` — 編集モードへ
- `hostname <name>`
- `interface <name>` — 存在しなければ新規作成してedit-ifへ
  - `description <text>`
  - `ip address <ip>/<prefix>`
  - `ip-routing`
  - `auto-negotiation { on | off }`
  - `shutdown` / `no shutdown`
- `ip route <net>/<prefix> <gateway> [distance <n>]` / `no ip route ...`
- `commit` / `save running-config` / `save startup-config [force-update]`
- `show interface[s]` / `show running-config` / `show startup-config`
  / `show version`（どのモードからでも実行可、実機準拠）
- `exit` / `quit` / `end` / `logout` — モードを1段階戻す
  （admin exec からは操作者EXECへ降格）

## `ip route`/`show ip route`/`show running-config`は共有ディスパッチ層を再利用

このエミュレータのCLIディスパッチは2層構造になっている
（`app.py`冒頭のコメント参照）:

```
1. app.py の handle_protocol_show(...)    … show系（先に実行される）
2. app.py の handle_protocol_config(...)  … 設定系（先に実行される）
3. engine/rules.py の RuleEngine.process(...) … 上記で拾われなかった
   ものの受け皿（ベンダ別の既定応答）
```

`ip route <net>/<prefix> <gw>`はIPCOMのマニュアル構文が偶然にも
Si-Rの省略形構文（`ip route 0.0.0.0/0 192.168.1.1`）と一致するため、
`handle_protocol_config`が既に**共有RIBエンジン(`rib_engine`)**へ
反映する。`_ipcom_process`側で改めて`ip route`を処理せず受理する
だけにしたのは、実装当初に両方で別々に状態を持ってしまい
「`show ip route`に整形の異なる重複行が出る」不具合を実際に踏んだ
ため（下記の実機検証ログで再現・修正を確認済み）。

`show ip route`は`handle_protocol_show`がRIBエンジンにルート
（スタティックまたは動的）が1件でもあればそちらを優先する。
1件も無い場合だけ`_ipcom_process`側の簡易フォールバック
（直接接続の経路のみ表示）が動く。`show running-config`は
`_build_running_config`にIPCOM用の早期returnを追加し、
`RuleEngine._ipcom_show_config`（hostname+interface設定のみ、
ルートはRIBエンジンが真の情報源なので出力しない）に委譲する。

## 実際に動かして確認した結果

エミュレータを実際に起動し、IPCOM装置を作成して一連の操作を
CLIで実行した（`/api/cli`経由）。

```
$ curl -X POST .../api/device -d '{"id":"ipcom-fresh","hostname":"IPCOM-FRESH","type":"ipcom"}'
{"ok":true,"id":"ipcom-fresh"}

--- 未昇格でconfigure terminal(拒否される) ---
> configure terminal
% Authorization failed.
  (admin コマンドで管理者EXECに昇格してください)

--- admin昇格 ---
> admin
(mode: exec, ipcom_admin: true)

--- configure terminal → load running-config → hostname → interface編集 ---
ipcom# configure terminal
ipcom(config)# load running-config
ipcom(edit)# hostname IPCOM-NEW
ipcom(edit)# interface lan0.0
ipcom(edit-if)# description WAN
ipcom(edit-if)# ip address 203.0.113.5/30
ipcom(edit-if)# exit
ipcom(edit)# save startup-config
startup-config へ保存しました。
ipcom(edit)# exit
ipcom(config)# exit

--- show running-config ---
hostname IPCOM-NEW
interface lan0.0
  description WAN
  auto-negotiation on
  ip address 203.0.113.5/30
  ip-routing
  exit
interface lan0.1
  auto-negotiation on
  ip address 192.168.100.1/24
  ip-routing
  exit

--- show interface ---
lan0.0       203.0.113.5/30 up       WAN
lan0.1       192.168.100.1/24 up

--- show ip route（RIBエンジン経由、connected routesが正しく反映）---
Codes: L - local, C - connected, S - static, ...
C        192.168.100.0/24 is directly connected, lan0.1
L        192.168.100.1/32 is directly connected, lan0.1
C        203.0.113.4/30 is directly connected, lan0.0
L        203.0.113.5/32 is directly connected, lan0.0
```

権限昇格の拒否→admin昇格→config/edit/edit-ifのモード遷移→
hostname/interface設定→save→exitでのモード降順の連鎖→show系
（running-config/interface/ip route）まで、意図した通りに動作する
ことを確認した。

途中、`ip route`をRIBエンジンと`_ipcom_process`側の両方で二重管理
してしまい`show ip route`に整形の異なる重複行（`172.16.0.0/16/0`
のような壊れた表示）が出る不具合を実際に踏み、`_ipcom_process`側の
独自`state.static_routes`管理を削除してRIBエンジン一本化する
修正で解消したことも確認済み。

## RIP/OSPF/BGP ルーティング

ユーザーから「一般的なRFCの内容などで実装してほしい」という要望を
受けて追加。IPCOMのマニュアル（P3NK-6002）記載の`router rip`/
`router ospf`/`router bgp <asn>`構文が、既存のCisco/Si-R系装置で
使っている構文とほぼ一致していたため、**独自にRIP/OSPFを再実装
するのではなく、既存の共有プロトコルエンジン(`rip_engine`/
`ospf_engine`)にそのまま乗せる**形にした。

```
ipcom(edit)# router rip
ipcom(edit-router)# network <ip>/<prefix>
ipcom(edit-router)# exit

ipcom(edit)# router ospf
ipcom(edit-router)# network <ip> <wildcard-mask> area <area-id>
ipcom(edit-router)# exit

ipcom(edit)# router bgp <asn>
ipcom(edit-router)# neighbor <ip> remote-as <asn>
ipcom(edit-router)# exit
```

### なぜ「実装」がほとんど無いのか（app.pyの2層ディスパッチを再利用）

`app.py`のCLIディスパッチは`handle_protocol_show`/
`handle_protocol_config`が`rule_engine.process`（＝`_ipcom_process`）
より**先に**実行される2層構造になっている（`app.py`冒頭のコメント
参照）。RIP/OSPFの`network`/`redistribute`等のサブコマンドは、
`state._routing_mode`という側路属性だけを見て動いており、
`state.device_type`や`state.mode`の値を一切問わない。つまり
IPCOM用に`network`文をパースするコードを書く必要は無く、
`_ipcom_process`側は「`router rip`/`router ospf`/`router bgp`で
`config-router`モードへ遷移する」「`config-router`/`edit-if`モードの
コマンドは（既に反映済みなので）そのまま受理するだけ」の2点だけを
実装すれば、実際のRIP/OSPFネイバー形成・経路学習が動く。

### 実機検証で見つけた不具合: `router ospf`にプロセスID番号が無い

IPCOM実機のOSPF定義構文はCisco IOSと違い、`router ospf`に
プロセスID番号を取らない（マニュアルの例もすべて`router ospf`単体）。
一方`app.py`側のOSPF検出は`^router\s+ospf\s+(\d+)`（数字必須）を
要求しており、bare `router ospf`だとこの正規表現にマッチせず、
`handle_protocol_config`が素通りして`_routing_mode`が一切
設定されない、というバグを実際に`show ip ospf neighbor`が
`% OSPF is not configured on this device.`を返す形で発見した。
`_ipcom_process`側で`router ospf`（bare）を検出した際に
`_routing_mode`/`_ospf_process`等の属性を直接立てるフォールバックを
追加して解消した。

OSPFの`network`文自体はCisco IOSのワイルドカードマスク構文
（`network <ip> <wildcard> area <id>`）が共有エンジン側の唯一の
対応フォーマットのため、IPCOM実機のプレフィックス表記
（マニュアルには具体例が無いが、他のIPCOMコマンドの慣習からは
`network <ip>/<prefix> area <id>`が予想される）とは異なる。これは
「プロトコルの動きを正しく再現する」ことを優先した意図的な簡略化。

### 実際に動かして確認した結果

2台のIPCOM装置を作成しリンクさせ、RIPとOSPFそれぞれで実際に
ネイバーを形成させた。

**RIP:**
```
$ (IPCOM-R1) show ip rip neighbor
Index   IP Address        Last Update   Bad Pkts   Bad Routes
1       10.0.0.2          00:00:11      0          0

Routing Information Sources:
  10.0.0.2            IPCOM-R2          2 routes

$ (IPCOM-R1) show ip route
...
R        172.20.2.0/24 [120/1] via 10.0.0.2, lan0.0
```

IPCOM-R2側だけに存在する`172.20.2.0/24`がRIPで正しく学習され、
AD/メトリック`[120/1]`も正確に表示された。

**OSPF:**
```
$ (IPCOM-R1) show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.0.0.2        1     Full/DR         00:00:28    10.0.0.2        lan0.0
```

隣接関係が`Full/DR`まで正しく確立した（DR選出込みの実際のOSPF
ステートマシンが動いていることを確認）。

## フロントエンド（Web UI）対応

`static/index.html`の以下を拡張:
- `getPrompt()` — IPCOMは`>`/`#`（操作者/管理者EXEC）と
  `(config)`/`(edit)`/`(edit-if)`/`(edit-router)`のプロンプト
  サフィックスを表示
- 「＋ IPCOM EX2」ボタン（ランチャー画面・メイン画面の両方）
- `DEV_TEMPLATES`、`portOptions()`、`defPort()`、装置種別の色分け、
  設定テキストからのベンダー自動判定（`_inferDeviceType`）

## テスト

新規の単体テストファイルは作成していない（既存の
`tests/test_config_submodes.py`等はIPCOM固有のモード遷移
（`_ipcom_process`は`CONFIG_SUBMODES`登録簿を経由しない自己完結型
ハンドラのため対象外）。動作確認は上記の実機検証ログの通り、実際に
起動したエミュレータへ`/api/cli`を叩いて確認した。全回帰テスト
（1352 passed、既知のflaky ipsec_dpd_timers 2件のみ）で既存機能への
影響が無いことも確認済み。

## 制約・今後の拡張余地

- アンチウィルス機能（`rule virus`、クラウドサンドボックス検査、
  フォルスポジティブ・コントロール等）は未実装。マニュアルの
  中心テーマだが、ユーザーの選択で今回のスコープ外とした
- `save`/`commit`は実際にはバッファリングせず、コマンド投入時点で
  即座に反映する簡略化（他機種と同様の割り切り）
- OSPFの`network`文はCisco IOSワイルドカードマスク構文のみ対応
  （IPCOM実機のプレフィックス表記の可能性がある構文とは異なる）
- BGP4は`router bgp <asn>`でモード遷移するところまでのみ確認。
  `neighbor`等のサブコマンド自体の実機検証はしていない
- SNMP/syslog/ユーザー認証（`user`/`user-role`構成定義モード等）は
  未対応
- インターフェース種別は`lan<N>.<VLAN>`のみ。マニュアルにある
  `bndN`（ボンディング）、`vlanN`、`pppN`、`passN`は未対応
