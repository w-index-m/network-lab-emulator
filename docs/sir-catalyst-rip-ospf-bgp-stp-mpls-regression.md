# Si-R/Catalyst: RIP/OSPF/BGP/STP/MPLS リグレッション記録（2026-09-09〜10）

Si-R実機マニュアル(`docs/reference/Si-R.G120G121G210G211cmd_reference-g12x_g21x_202606.pdf`)
との突き合わせ、および3台Catalyst OSPF構成でのライブ検証を通じて見つけた
不具合の修正記録と、再現用のコマンドサンプルをまとめる。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`のHTTP APIに対するcurl/requestsで再現できる。
サーバーはTestClientではなく実際にuvicornで起動して検証すること。

## 見つけて修正した不具合（コミット順）

### 1. Si-R G210/G211: `ether mode`/`ether duplex` が未実装だった

マニュアル第4.1.4/4.1.5章に基づき追加。`mode`と`duplex`は別コマンドで、
`mode 1000`または`mode auto`のときは`duplex`設定が無効になり常にfull
固定で動作する仕様に注意。

```
configure terminal
ether 1 1 mode 100
ether 1 1 duplex full
exit
show ether brief
```

DPDタイマー同様、数値だけでなく単位が必要な項目は無いが、
`ether <group> <port> ...`の`<port>`は範囲指定可（`1-2`, `1,2`等）。

### 2. Si-Rの`show ip rip route`がGateway/Time/Interfaceを誤表示

- Gateway欄: ホスト名ではなくネクストホップIPを表示すべき（修正済み）
- Time欄: 「学習からの経過時間」ではなく「タイムアウトまでの残り時間」の
  カウントダウン（修正済み）
- Interface欄: 常に"lan0"固定だったのを実際の受信インタフェースに修正

### 3. Si-Rの`show ip ospf neighbor`がCiscoスタイルの表示だった

実機はインタフェースごとに見出しを分けた表示
(`Neighbor with lan0 result:`)で、列もDeadtime/DDL/ReqL/RtrL。
Si-R専用フォーマッタ`_format_ospf_neighbor_sir`(app.py)を追加。

### 4. OSPFルータID自動選出が未接続インタフェースのデフォルトIPで衝突

Si-R/Catalystは、ケーブル未接続のインタフェースにも同一の工場出荷時
デフォルトIP(Si-R: wan1=203.0.113.1、Catalyst: Gi0/0/0=203.0.113.2等)
を全装置共通で持っているため、router-id未設定のままOSPFを組むと
「実際にリンクしているLANインタフェース」より「未接続のデフォルトIP」の
方が数値的に大きく、複数の無関係な装置のRouter IDが同じ値に衝突する
ことがあった。

`_pick_ospf_default_router_id(state, device_id)`に`device_id`を渡し、
`vnet.interface_links`で実際にリンクされているインタフェースのIPを
優先するよう修正（該当が無ければ従来通りフォールバック）。

### 5. Si-RにSTP設定コマンド(`stp mode/age/delay/hello/domain priority`)が未実装

マニュアル第5章に基づき追加。実機はRSTP非対応で`stp mode disable|stp`
のみ。`stp age`(6-40s)/`stp delay`(4-30s)/`stp hello`(1-10s)は範囲外だと
`<ERROR> : 3 : format error`。`stp domain <instance_id> priority <p>`は
この機種はinstance 0のみ、priorityは4096刻みの0〜61440のみ有効。

```
configure terminal
stp mode stp
stp age 25s
stp delay 18s
stp hello 3s
stp domain 0 priority 4096
exit
show spanning-tree
```

あわせてSi-R形式`show spanning-tree`(`_format_spanning_tree_sir`)も追加。
Cisco/Rapid-PVST+前提のVLAN別表示ではなく、実機同様
`ether <group> <port>`名でポートを表示する。

**副次的に見つけた不具合**: `handle_protocol_config()`の戻り値が
`cli_command`側で常に握り潰されており、コマンド不正値に対するエラー
メッセージが一切CLI出力に反映されていなかった。戻り値が空でない場合
のみそれをCLI出力として返すよう修正(空文字列/Noneは従来通り
rule_engine側にフォールスルー)。

### 6. MPLS(LDP)がSi-Rに存在しないと判明 → Cisco/Nexus系に新規実装

Si-R実機マニュアルにはMPLS機能自体が無い(MPLSCPは「未サポート」と
明記のみ)。ユーザー確認の上、Cisco IOS-XE/NX-OS系向けに`MplsEngine`
(engine/protocols.py)を新規実装:

```
configure terminal
interface GigabitEthernet0/0
mpls ip
exit
mpls ip
exit
show mpls interfaces
show mpls ldp neighbor
show mpls ldp bindings
show mpls forwarding-table
```

実装範囲外(コメントに明記): LSPホップ単位のラベルスワップ計算、
RSVP-TE、MPLS-VPN/VRF。`show mpls forwarding-table`のoutgoing labelは
実際にLDPで配布された値ではなく宛先ごとに決定的に算出した表示専用値。

### 7. `show ip ospf neighbor`の二重登録・IP/インタフェース欠落（Cisco含む全機種共通）

3台CatalystでのOSPFライブ検証中に発見。内部エンジンのHello処理由来の
エントリ(device_idキー、正しくFullまで遷移)と、実UDPリスナー
(`engine/real_ospf_agent.py`)由来のエントリ(router_id文字列キー、
DBD交換までは進まずInitのまま残る)が別々に登録され、同一隣接が2行
表示されることがあった。Full側エントリは`nbr.iface`/`nbr.ip`が一度も
設定されておらず、Interface列は常時"GigabitEthernet0/0/0"固定、
Address列はhash値から捏造した実在しないIPになっていた。

`format_show_ospf_neighbor`を修正:
- 同一router_idのエントリはstateが最も進んだもの1件だけを表示
  (`Full > Loading > Exchange > ExStart > TwoWay > Init`)
- `vnet.interface_links`と(渡された場合)`device_sessions`から実際の
  送出インタフェース名・相手インタフェースIPを解決し、解決できない
  場合のみ従来の捏造フォールバックを使う

## サンプル: 3台Catalyst OSPF構成（チェーン A-B-C）

```python
import requests
base = "http://127.0.0.1:PORT"
A, B, C = "cat-a", "cat-b", "cat-c"

def cli(dev, cmd):
    return requests.post(f"{base}/api/cli",
        json={"device_id": dev, "command": cmd}).json().get("output", "")

for d in (A, B, C):
    requests.post(f"{base}/api/device", json={"id": d, "type": "catalyst", "hostname": d})

requests.post(f"{base}/api/link", json={
    "a": A, "iface_a": "GigabitEthernet1/0/1", "b": B, "iface_b": "GigabitEthernet1/0/1"})
requests.post(f"{base}/api/link", json={
    "a": B, "iface_a": "GigabitEthernet1/0/2", "b": C, "iface_b": "GigabitEthernet1/0/1"})

for c in ["configure terminal",
          "interface GigabitEthernet1/0/1", "no switchport",
          "ip address 10.5.12.1 255.255.255.0", "no shutdown", "exit",
          "interface GigabitEthernet1/0/2", "no switchport",
          "ip address 10.5.1.1 255.255.255.0", "no shutdown", "exit",
          "router ospf 1", "network 10.5.12.0 0.0.0.255 area 0",
          "network 10.5.1.0 0.0.0.255 area 0", "exit", "exit"]:
    cli(A, c)
# B, Cも同様（B: 10.5.12.2/10.5.23.2の2インタフェース、C: 10.5.23.3/10.5.3.1）

# 収束待ち（8秒程度）後
cli(A, "show ip ospf neighbor")
cli(A, "show ip route")
```

**重要**: `/api/link`のインタフェース指定パラメータ名は`iface_a`/`iface_b`
（`a_iface`/`b_iface`ではない）。デバイス作成は`/api/device`(`id`/`type`)
であって`/api/cli`のdevice_type引数は無視される点に注意
（`/api/cli`はdevice_idが未登録なら常にtype="cisco"で自動生成する）。

### 確認できた結果（実際の出力例）

```
# cat-a: show ip ospf neighbor
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.5.23.2       1     Full/DR         00:00:32    10.5.12.2       GigabitEthernet1/0/1

# cat-a: show ip route（抜粋）
C        10.5.1.0/24 is directly connected, GigabitEthernet1/0/2
C        10.5.12.0/24 is directly connected, GigabitEthernet1/0/1
O        10.5.23.0/24 [110/20] via 10.5.12.2, GigabitEthernet1/0/1
O        10.5.3.0/24 [110/30] via 10.5.12.2, GigabitEthernet1/0/1
```

## サンプル: ルートインジェクター → Prometheus → Grafana 監視パイプライン

```bash
# 1. 実バイナリのPrometheus/Alertmanager/Grafana一式を起動
#    （初回はGitHub Releasesからダウンロード。以降は既存プロセスを再利用）
./tools/setup_monitoring_stack.sh setup
./tools/setup_monitoring_stack.sh status   # 各サービスのヘルスチェック

# 2. Catalyst機を作成してルートインジェクターで経路投入
python3 tools/routing_generator.py --url http://localhost:8000 \
  --device <device_id> --count 10 --base-network 172.25.0.0 \
  --prefix 24 --next-hop 10.0.0.99

# 3. Prometheusで直接確認
curl -s 'http://localhost:9090/api/v1/query?query=netlab_route_count%7Bdevice_id%3D%22<device_id>%22%7D'

# 4. Grafanaにダッシュボード作成（APIで投入可能）
curl -s -X POST http://localhost:3000/api/dashboards/db \
  -u admin:admin -H "Content-Type: application/json" \
  -d '{"dashboard": {...panels with netlab_route_count query...}, "overwrite": true}'
```

- Exporter: `tools/prometheus_exporter.py`（標準ライブラリのみ、
  エミュレータの`/api/snmp/dashboard`をポーリングして`/metrics`に公開）
- Grafanaの初期ユーザーは`admin`/`admin`
- Playwright(`/opt/pw-browsers/chromium`)でスクリーンショット取得可能
  （Prometheus UIは新CodeMirrorベースのクエリボックスなので
  `page.click('.cm-content')` → `page.keyboard.type(...)`で入力する。
  `page.fill('textarea', ...)`は効かない）

**IPアドレス衝突の落とし穴（再掲・毎回発生するので必ず確認）**:
テスト用アドレス帯(`192.0.2.0/24`, `198.51.100.0/24`等)は
`saved_config.json`に永続化された過去の検証用デバイスが既に使っている
ことが多く、ピア探索・ルータID自動選出が誤爆する。新規検証の前に
以下で未使用か確認すること:

```python
import json
d = json.load(open('saved_config.json'))
used = set()
for dev in d.get('devices', {}).values():
    for t in (dev.get('ipsec_tunnels') or {}).values():
        if t.get('local_ip'): used.add(t['local_ip'])
    for info in (dev.get('interfaces') or {}).values():
        if isinstance(info, dict) and info.get('ip'):
            used.add(info['ip'])
```

## 未着手・見送った項目

- Si-Rの`show ip rip database`相当コマンド: 未実装（Si-R向けshow系は
  `show ip rip route`/`show ip rip protocol`のみ実装済み）
- MPLSのLSPホップ単位のラベルスワップ計算、RSVP-TE、MPLS-VPN/VRF

## 関連ドキュメント

- `docs/routing-cli-verification.md` — RIP/OSPF/EIGRP/BGP/vPCの
  以前の実機突き合わせ記録（同じ手法）
- `docs/sir-ipsec-vpn-sample.md` — Si-R拠点間IPsec VPNの設定サンプル
- `docs/restconf-catalyst.md` — RESTCONF/STPの実機突き合わせ記録
- `docs/prometheus-grafana-windows.md` — Prometheus Exporter設計と
  Windows向けセットアップ手順
- `tools/setup_monitoring_stack.sh` — このLinuxサンドボックスで
  実バイナリ検証済みの監視スタック一式のセットアップスクリプト
