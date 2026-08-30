# network_route_injector — 実機ルータ経路注入ツール

RIP / OSPF(P2P・Broadcast) / BGP(FlowSpec対応) の経路配信・ネイバー確立を
1つのWindows GUIにまとめた**実機検証用**ツール。本エミュレータが「机上でトポロジを組む」のに対し、
これは**実機ルータ（Catalyst / Si-R 等）へ本物のパケットで経路を注入**する側。

> ⚠️ **安全上の注意（必読）**
> 本ツールはネイバー偽装・経路注入・大量偽ネイバー生成・FlowSpec discard など強力な機能を含みます。
> **自分が管理する検証ラック／許可されたラボ機器に対してのみ**使用してください。
> 実運用ネットワークや許可のない対象への実行は絶対に行わないこと（ツール免責にも明記）。

## 動作要件
- **Windows**（Tkinter GUI）
- RIP / BGP タブ … 標準ライブラリのみ。**管理者権限不要**
- OSPF タブ（P2P/Broadcast） … `pip install scapy` ＋ **Npcap(WinPcap互換)** ＋ **管理者権限**で起動
- ツールを動かすPCを、**対象ルータのインターフェースと同一L2セグメント**に接続すること
  （EVE-NG利用時は Cloud/pnet でPCのNICをブリッジ）

## 起動
```
python tools/route_injector/network_route_injector.py
```

---

## Catalyst への経路注入

### A) RIP（一番簡単・管理者権限不要）
**Catalyst 側**（対象セグメント 10.0.0.0/24、Catalyst=10.0.0.254 とする）:
```
router rip
 version 2
 network 10.0.0.0
```
**ツール [RIP] タブ**:
- Version: `2` / 宛先: `224.0.0.9`(マルチキャスト) または `10.0.0.254`
- 送信元IP(bind): `10.0.0.100`（同一セグメントの空きIP）
- 経路を追加: `network=172.16.50.0 netmask=255.255.255.0 nexthop=10.0.0.100 metric=1`
- 送信 → Catalyst で `show ip route rip` に `R 172.16.50.0` が載れば成功

### B) OSPF（scapy+Npcap+管理者）
**Catalyst 側**:
```
interface Vlan10                 ! ツールと同一セグメントのSVI
 ip address 10.0.0.254 255.255.255.0
 ip ospf network point-to-point  ! P2Pタブを使う場合
router ospf 1
 network 10.0.0.0 0.0.0.255 area 0
```
**ツール [OSPF P2P] タブ**（1対1で確実に張りたいとき）:
- iface=対象NIC / my_ip=`10.0.0.100` / router_id=`1.1.1.1` / area=`0.0.0.0` / mask=`255.255.255.0`
- Full到達後、External経路を注入: `172.16.60.0 / 255.255.255.0 metric=20`
- Catalyst で `show ip ospf neighbor`（Full）→ `show ip route ospf`（`O E2 172.16.60.0`）
- **[OSPF Broadcast] タブ**は priority=0 の偽ネイバーを多数生成し、DR/BDR配下での大量ネイバー検証に使う

### C) BGP（管理者権限不要・ルータ側に事前設定必須）
**Catalyst 側**（Catalyst AS65000、ツール AS65100）:
```
router bgp 65000
 neighbor 10.0.0.100 remote-as 65100
```
**ツール [BGP] タブ**:
- peer=`10.0.0.254` / local_as=`65100` / remote_as=`65000` / router_id=`10.0.0.100`
- Established後、経路広告: `192.0.2.0/24 next_hop=10.0.0.100`（community/MED/local-pref/FlowSpecも可）
- Catalyst で `show ip bgp` / `show ip route bgp`

---

## Si-R（富士通）への経路注入

Si-R は機種により対応プロトコルが異なる。**RIP が最も手堅い**。

### A) RIP（推奨）
**Si-R 側**（lan 0 = 10.0.0.253/24 の例）:
```
lan 0 ip rip use v2 v2 off 0
rip use on
```
**ツール [RIP] タブ**: Catalystと同様（宛先 `224.0.0.9` or `10.0.0.253`、送信元 `10.0.0.100`）
→ Si-R で `show ip route` に注入経路（`R`）が載れば成功

### B) OSPF（対応機種のみ・scapy+Npcap+管理者）
**Si-R 側**:
```
ospf use on
ospf area 0.0.0.0
lan 0 ip ospf use on
```
**ツール [OSPF P2P/Broadcast] タブ**で隣接確立→External注入。
> Si-R の OSPF はネットワークタイプ（broadcast/p2p）や options の一致に敏感なことがある。
> Full にならない場合は hello/dead 間隔・area・mask を実機に合わせる。

---

## 注入後の検証（自動化）
本リポジトリの検証ツールと組み合わせると、「注入 → 実機に載ったか」をデータで確認できる:
- Catalyst(IOS-XE): `tools/eveng_deploy.py verify` の `route_present`（RESTCONF）で
  注入プレフィックスがRIBにあるかを構造判定（`docs/c3650-api.md` / `examples/c3650/checks.routes.json`）
- もしくは各機器で `show ip route {rip|ospf|bgp}` を直接確認

## うまくいかない時のチェック
1. **同一セグメントか**（送信元IPが対象IFのサブネット内／PCが同じVLANに刺さっているか）
2. **プロトコル有効化**（router rip / router ospf / router bgp・Si-Rの `use on`）
3. **OSPF**: Npcap導入・管理者起動・NIC選択・hello/dead/area/mask一致
4. **BGP**: ルータ側に `neighbor <ツールIP> remote-as <ツールAS>` があるか、AS番号一致
5. Windows Firewall がRIP(UDP520)/BGP(TCP179)/OSPF(89)を止めていないか
