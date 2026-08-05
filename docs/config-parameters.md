# 設定パラメータ 随時メモ

エミュレータが解釈する設定コマンド／パラメータの備忘録。
実装・検証のたびに追記する。**「対応」= 実サーバ(HTTP/CLI)で動作確認済み。**

- 検証方法: `NETLAB_AUTH_DISABLE=1 NETLAB_FAST_TIMERS=1 uvicorn app:app --port 8099` 起動後、
  `python tests/scenario_regression.py` で全シナリオ回帰。
- 最終更新: 2026-08-05

---

## OSPF (Cisco/Catalyst)

| コマンド | 説明 | 状態 |
|---|---|---|
| `router ospf <pid>` | OSPFプロセス開始 | 対応 |
| `network <ip> <wildcard> area <area>` | 参加ネットワーク/エリア指定 | 対応 |
| `passive-interface <if>` / `passive-interface default` | Hello抑制 | 対応 |
| `interface <if>` → `ip ospf cost <n>` | コスト | 対応 |

- **多段伝播**: エリア内全域へLSAをfixpointまでフラッディング。3台チェーンでも遠方LANを学習。
- DR/BDR選出は非プリエンプティブ (RFC2328 §9.4)。dead = hello×4。
- 確認: `show ip ospf neighbor` / `show ip route ospf` / `show ip ospf database`

## RIP (Cisco)

| コマンド | 説明 | 状態 |
|---|---|---|
| `router rip` | RIP開始 | 対応 |
| `version 2` | RIPv2 | 対応 |
| `network <classful>` | **その範囲に実IFがある場合のみ有効化**し直結網を広告（実機準拠） | 対応 |
| `distribute-list <prefix-list名> in\|out` | 経路フィルタ | 対応 |
| `distribute-list <ACL番号> in\|out` | 標準ACLベースの経路フィルタ | 対応 |

- タイマー: timeout 180s → garbage 120s (RFC2453)。split-horizon + poison reverse。
- メトリック = ホップ数（2ホップ先 = metric 3）。
- 注意: 同一クラスネットワーク内の不連続サブネットは auto-summary で集約（実機どおり）。

## BGP (Cisco) — 基本

| コマンド | 説明 | 状態 |
|---|---|---|
| `router bgp <as>` | BGP開始 | 対応 |
| `neighbor <ip> remote-as <as>` | eBGP/iBGPネイバー | 対応 |
| `network <ip> mask <mask>` | 経路広告（**maskをプレフィックス長へ変換**） | 対応 |

- FSM: Idle→Connect→OpenSent→OpenConfirm→Established (RFC4271)。
- ベストパス選択: local-preference → AS-path長 → MED。
- **トランジット**: 他ピアから学習した経路も再広告（eBGPは自ASをprepend）。iBGP学習経路は他iBGPピアへ再広告しない(split-horizon)。AS-pathループ防止（自ASを含む経路は拒否）。
- 確認: `show ip bgp` / `show ip bgp summary`

## BGP — AWS Direct Connect / VPN 向け拡張

| コマンド | 説明 | 状態 |
|---|---|---|
| `neighbor <ip> password <pw>` | TCP MD5認証 (RFC2385)。両端不一致で確立不可(`%TCP-6-BADAUTH`, Active) | 対応 |
| `neighbor <ip> route-map <name> in\|out` | インバウンド/アウトバウンド経路制御 | 対応 |
| `neighbor <ip> fall-over bfd` | BFD高速障害検知を有効化 | 対応 |
| `bfd interval <ms> min_rx <ms> multiplier <n>` | BFDパラメータ | 対応 |
| `route-map <name> permit <seq>` | route-map定義 | 対応 |
| `set as-path prepend <as> [<as> ...]` | AS-path prepend | 対応 |
| `set local-preference <n>` | local-pref設定 | 対応 |
| `set metric <n>` | MED設定 | 対応 |

- **冗長構成**: 複数eBGPピア。DX=local-pref高/AS-path短で優先、VPN=prependで予備。
- **フェイルオーバー**: IFダウン/BFD DownでセッションIdle→学習経路撤去→バックアップへ。IFアップで自動復帰。
- 確認: `show ip bgp`（`*>`ベスト/`*`冗長候補）/ `show bfd neighbors`
- 典型パラメータ例:
  - AWS側AS: `64512`（VGWデフォルト） / 顧客AS: `65000` など private ASN
  - BGP peer IP: `169.254.x.x/30`（APIPA、AWSトンネル内IP）

## STP / RSTP (Catalyst)

| コマンド | 説明 | 状態 |
|---|---|---|
| `spanning-tree mode rapid-pvst\|pvst\|rstp` | モード | 対応 |
| `spanning-tree vlan <id> priority <n>` | ブリッジ優先度（4096刻み） | 対応 |
| `spanning-tree portfast` / `bpduguard enable` / `guard root` | ポート保護 | 対応 |

- 3台トライアングルで最小優先度がルート選出、1ポートがAlternate/Blockingでループ遮断。
- 確認: `show spanning-tree` / `show spanning-tree summary`

## EtherChannel / LACP (Catalyst)

| コマンド | 説明 | 状態 |
|---|---|---|
| `interface range <if> - <n>` → `channel-group <n> mode active\|passive\|on` | バンドル構成 | 対応 |

- モード成立: active+active / active+passive / on+on。passive+passive・モード不一致は不成立。
- メンバー障害: `shutdown`で`(D)`、残1本でもPoは`SU`維持、全滅で`SD`。`no shutdown`で`(P)`復帰。
- 確認: `show etherchannel summary`

## 経路フィルタ共通 (prefix-list / ACL / route-map)

| コマンド | 説明 | 状態 |
|---|---|---|
| `ip prefix-list <name> [seq <n>] permit\|deny <net>/<len> [ge <n>] [le <n>]` | プレフィックスリスト | 対応 |
| `access-list <num> permit\|deny <net> <wildcard>\|host <ip>\|any` | 標準ACL（経路フィルタにも橋渡し） | 対応 |

---

## 環境変数

| 変数 | 用途 |
|---|---|
| `NETLAB_AUTH_DISABLE=1` | ログイン認証を無効化（テスト用） |
| `NETLAB_FAST_TIMERS=1` | 各プロトコルのタイマーを短縮（収束を高速化） |

## 回帰シナリオ (`tests/scenario_regression.py`)

1. 3台Cisco OSPFチェーン（多段ルート交換＋双方向ping）
1b. 3台Cisco RIPチェーン（広域イーサ中継）
2. 3台Catalyst STPトライアングル
3. Catalyst EtherChannel メンバー障害/復旧
4. 擬似AWS eBGP（VGW相当）
5. RIP + distribute-list（prefix-list / 標準ACL）
6. 擬似AWS Direct Connect + VPNバックアップ（MD5/prepend/BFD/フェイルオーバー）
7. BGP 3-ASトランジット（AS-path伝播・prepend・ループ防止）
