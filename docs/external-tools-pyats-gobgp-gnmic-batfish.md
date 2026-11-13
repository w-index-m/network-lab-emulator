# 外部CLIツール4本との連携: pyATS/Genie・GoBGP・gnmic・Batfish

実機向けの定番ツールをそのままエミュレータに当て、「本物のツールから見て
本物の機器として振る舞うか」を確かめられるようにした。4本とも、当てた
ことで**エミュレータ側の実バグ**が見つかり修正している(下の「見つかった
バグ」)。

| ツール | 何をするか | 繋ぎ方 | 追加したもの |
|---|---|---|---|
| pyATS/Genie | show出力を構造化・設定変更前後の差分 | unicon で本物のSSH(TCP/22) | `tools/pyats_testbed.py`, `GET /api/ssh/listeners` |
| GoBGP | 本物のBGPスピーカーとピアリング | 実TCP/179 | `engine/real_bgp_agent.py` をRFC準拠に書き直し |
| gnmic | gNMI Capabilities/Get/Set/Subscribe | 実gRPC(既定50052) | Subscribe の on-change 修正 |
| Batfish | running-config の静的解析・経路の差分検証 | `show running-config` を書き出し | `tools/batfish_snapshot.py` |

## インストール(このサンドボックスで実施した手順)

```bash
# GoBGP / gnmic (Go製。proxy.golang.org はegressで許可されている)
go install github.com/osrg/gobgp/v3/cmd/gobgp@latest
go install github.com/osrg/gobgp/v3/cmd/gobgpd@latest
# gnmic は go.mod の replace 指示のため `go install ...@latest` が失敗する → clone してビルド
git clone --depth 1 https://github.com/openconfig/gnmic && (cd gnmic && go build -o ~/go/bin/gnmic .)

# pyATS/Genie・pybatfish: システムの cryptography(debian管理)をpipが
# アンインストールできず失敗するので、--system-site-packages の venv に入れる
python3 -m venv --system-site-packages ~/venv-netops
~/venv-netops/bin/pip install "pyats[library]" pybatfish
apt-get install -y openssh-client      # unicon は ssh コマンドを使う

# Batfish 本体
docker run -d --name batfish -p 9996:9996 -p 9997:9997 batfish/allinone
```

いずれも `requirements*.txt` には入れていない(netmiko/ansible/zgrab2 と同じく
手動/オプションの検証扱い)。テストはツールが無ければ skip する。

## 1. GoBGP — 本物のBGPスピーカーと経路交換

`engine/real_bgp_agent.py` は以前、Capabilityを一切広告しないOPENを返し、
AS_PATHを常に4バイトで読み、WITHDRAWNを無視し、自分からは経路を一切
送らないパッシブリスナーだった。`tools/route_injector_cli.py`(4-octet AS
capability付きで送る)からの注入には足りていたが、実装として本物の相手と
話すには不十分だったので書き直した:

- OPENで MP-BGP(IPv4 unicast)/Route Refresh/4-octet AS を広告し、相手のOPENと
  突き合わせて AS_PATH の幅(2/4バイト、AS4_PATH 対応)を決める
- WITHDRAWN の処理、セッション断時の学習経路撤回
- 自発 KEEPALIVE(hold/3)
- `network` で広告した経路・他ピアから学んだ経路を相手へ UPDATE で広告し、
  変化があれば追加/撤回を送る(eBGPは自ASをprepend、next-hop-self、
  iBGP→iBGPはsplit horizon)
- `neighbor <ip> remote-as <as>` が1つでも設定された装置は、未設定の相手を
  NOTIFICATION Cease/Connection Rejected、AS不一致を OPEN Error/Bad Peer AS で
  拒否(neighbor未設定の装置は従来どおり誰でも受ける = route_injector互換)
- `router bgp`/`neighbor`/`network`/IP変更の投入後に `ensure_bgp_agent()` で
  リスナーを起動/張り直す(起動時に居なかった装置もピアリングできる)

ライブ検証(`tests/test_gobgp_real_peering.py` と同じ構成):

```
$ gobgp neighbor 127.0.31.1 -j | jq .state.session_state     # → "established"
$ gobgp neighbor 127.0.31.1 adj-in
   ID  Network              Next Hop             AS_PATH              Age        Attrs
   0   10.77.0.0/24         127.0.31.1           65200                00:00:01   [{Origin: i}]
$ gobgp global rib add 10.88.0.0/24 -a ipv4
   → エミュレータの loc_rib に 10.88.0.0/24 (as_path [65100]) が入る
$ gobgp global rib del 10.88.0.0/24 -a ipv4
   → エミュレータから撤回される
```

neighbor に 65999 を期待させて GoBGP(AS 65100)を繋ぐと
`bad peer AS 65100 (expected 65999)` で拒否され Established にならない。

## 2. gnmic — gNMI の4つのRPC

```
$ gnmic -a 127.0.32.1:50852 --insecure -u admin -p admin capabilities
gNMI version: 0.8.0
$ gnmic ... get --path /interfaces/interface[name=GigabitEthernet0/0/0]
  → name/enabled/ietf-ip:ipv4 address 203.0.113.2 ...
$ gnmic ... set --update-path /interfaces/interface[name=GigabitEthernet0/0/0]/description \
            --update-value set-by-gnmic          → 装置の desc が変わる
$ gnmic ... set --update-path /native/hostname --update-value GNMIC-RENAMED
$ gnmic ... subscribe --path /native/hostname --mode once
$ gnmic ... subscribe --path /native/hostname --mode stream --stream-mode on-change
  → 初期値 "CHANGED-MID-STREAM" の後、変更した瞬間に "ONCHANGE-2" が届く
```

**修正したバグ**: STREAM購読をサブスクリプションのモードに関係なく全部
SAMPLE扱い(sample_interval未指定なら10秒周期)にしていたため、
`--stream-mode on-change` が「10秒ごとのポーリング」になり、変更が即時に
届かず、変化が無くても値を送り続けていた。サブスクリプションごとに
SAMPLE(sample_interval/suppress_redundant)・ON_CHANGE(変化時のみ、
heartbeat_interval対応)・TARGET_DEFINED(=ON_CHANGE扱い)で処理するように
した。`tests/test_gnmic_cli.py`(8 tests)。

## 3. pyATS/Genie — 本物のSSH越しにパース・差分

```bash
# 1) SSHが有効な装置(crypto key generate rsa 済み)から testbed を作る
python tools/pyats_testbed.py testbed --url http://localhost:8000 \
    --username admin --password 'Adm1nP@ss' -o testbed.yaml
# 2) パース
python tools/pyats_testbed.py parse -t testbed.yaml --device BF-R1 "show ip route"
# 3) 変更前後のスナップショットと Genie Diff
python tools/pyats_testbed.py snapshot -t testbed.yaml --device BF-R1 -d before
(r2 の GigabitEthernet0/0/1 を shutdown)
python tools/pyats_testbed.py snapshot -t testbed.yaml --device BF-R1 -d after
python tools/pyats_testbed.py diff before after
```

ライブ検証の diff 出力(r2 の LAN を落とした結果、r1 から O IA 経路が消えた):

```
### show ip route
 vrf:
  default:
   address_family:
    ipv4:
     routes:
-     10.227.2.0/24:
-      active: True
-      metric: 20
-      next_hop: ... next_hop: 10.227.12.2 / outgoing_interface: GigabitEthernet0/0/0
-      source_protocol: ospf
-      source_protocol_codes: O IA
```

unicon(iosxe プラグイン)の接続シーケンス(`term length 0` 等の初期化コマンド)
はそのまま通り、`show version`/`show ip interface brief`/`show interfaces`/
`show ip route`/`show ip ospf neighbor`/`show ip ospf interface`/
`show inventory`/`show ip ssh`/`show access-lists`/`show running-config` が
すべて Genie でパースできる。

**修正したバグ(pyATSで発見)**:
- `show interfaces`(引数なし)が Cisco/Catalyst でも Si-R 形式
  (`IP address/masklen:`)を返しており、Genie のパース結果が空だった
- `show inventory` が全機種 WS-C3650 スタック。cisco の show version
  (ISR4321)と矛盾していた → cisco は ISR4321、wlc は C9800-CL に揃えた。
  catalyst は実機 WS-C3650 の show tech-support 採取に合わせた既存の固定値
  (environment/sdm 等と同じ採取元)なので変えていない(show version の
  C9300 表記との食い違いは残っている)
- `show ip ssh` が未実装(`% Invalid input`)
- running-config の `version 17.3` が show version の 17.9.1 と矛盾
- OSPF の Dead Time が「最終Hello受信からのカウントダウン」ではなく
  「隣接確立からの経過 mod dead_interval」で、0〜40秒を不規則に回っていた
  (たまたま `00:00:00` が出て気づいた)
- インタフェースを shutdown してもそのセグメントが Router LSA から外れず、
  対向に経路が残り続けた(snapshot/diff で「差分なし」になって発見)

## 4. Batfish — 静的解析とエミュレータとの差分検証

```bash
python tools/batfish_snapshot.py --url http://localhost:8000 \
    --devices bf-r1,bf-r2 --out ./snap --analyze --compare-routes
```

エミュレータの `show running-config` を Batfish のスナップショットとして
書き出し、Batfish に解析させ、さらに**エミュレータ自身の `show ip route`
と、Batfish が同じ設定から計算した経路を突き合わせる**。両者が食い違えば
どちらか(多くはエミュレータ)がおかしい、という差分検証。終了コードは
一致で0、差分ありで2。

ライブ検証(r1: area 0 のみ、r2: area 0 + area 1 の ABR、r1 に ACL):

```
## fileParseStatus     bf-r1.cfg PASSED CISCO_IOS / bf-r2.cfg PASSED CISCO_IOS
## initIssues          (none)
## ospfSessionCompatibility
  bf-r1[GigabitEthernet0/0/0] 10.227.12.1 Area 0 ↔ bf-r2[GigabitEthernet0/0/0] ... ESTABLISHED
## filterLineReachability
  ['bf-r1: BLOCK']  30 permit ip any any  Blocking_Lines ['20 deny ip any any']  BLOCKING_LINES
## routes bf-r1: MATCH (6 common)
## routes bf-r2: MATCH (5 common)
```

`filterLineReachability` は「`deny ip any any` の後ろに書いた `permit` は
絶対にマッチしない」という設定ミスを指摘している(意図的に仕込んだもの)。

**修正したバグ(Batfishで発見)**: 最初の比較は
`routes bf-r1: DIFF — only_batfish ospf 10.227.2.0/24` で、Batfish は
OSPF隣接が成立すると判定しているのにエミュレータは `(No neighbors)` だった。

- 1台に複数エリアを設定する(ABR)と、ノード単位の `area_id` が最後に
  入力したエリアで上書きされ、**全インタフェース**がそのエリアになって
  いた。area 0 のリンクにも area 1 の Hello が出てエリア不一致で隣接が
  張れなかった → Hello をリンクごと(そのIFのIPを含む network 文のエリア)
  に送受信するようにし、`show ip ospf interface` の Area もIFごとにした。
  ABRが属する全エリアへLSAをフラッディングするようにもした
- 自分の属さないエリアの経路は `O IA`(エリア間)として扱う
- `O IA`/`O E2`/`O N2` が `IA`/`E2`/`N2` と O 抜きで表示されていた
- running-config の `network ... area` が全行同じ(最後の)エリアで、
  しかも `area 0` が `area 0.0.0.0` になっていた → network ごとのエリアを
  入力どおりの表記で出す
- `ip access-group` がどのインタフェースで打っても内部的に `lan0` に
  紐付き、running-config にも出ていなかった → 打ったインタフェースに
  紐付け、running-config の該当インタフェースに出す
  (なお、インタフェースACLは元々データプレーン(ping等)では強制されて
  いない。`check_packet` を呼んでいる箇所が無い既存のスコープで、今回は
  変えていない。ACLの到達性は Batfish の静的解析で確認できる)

## テスト

| ファイル | 内容 | 外部ツール |
|---|---|---|
| `tests/test_gobgp_real_peering.py` | GoBGPと双方向の経路交換・撤回・Bad Peer AS 拒否 | gobgpd/gobgp(無ければskip) |
| `tests/test_gnmic_cli.py` | capabilities/get/set/subscribe(once・sample・on-change)・エラー | gnmic(無ければskip) |
| `tests/test_pyats_batfish_tools.py` | testbed生成・show ip route パース・経路差分(常時)、Genie で実出力をパース | genie(無ければその部分だけskip) |
| `tests/test_external_tool_fidelity.py` | 上記で見つけたバグの回帰(外部ツール非依存) | なし |

## 既知の残り

- OSPFのエリアモデルは依然として簡略化されている(LSDBはエリア別に分かれて
  おらず、Type 3 LSA の要約も実機ほど厳密ではない)。単一ABR/バックボーン
  の構成では Batfish と一致することを確認した。
- 一度止めた OSPF プロセスに同じ network 文を入れ直すと、2つ目以降のエリアの
  network が `show ip ospf interface`/running-config に出ないことがある
  (回帰テストを作る途中で、テスト基盤が各テスト後にOSPFを止める挙動から
  気づいた。通常の CLI 操作での再現手順は未確認のため未修正)。
