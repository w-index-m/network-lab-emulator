# EVPN/VXLAN（Cisco Nexus）サンプル構成

**結論**: `feature nv overlay` / `evpn` / `interface nve1` /
`address-family l2vpn evpn` 系のコンフィグは投入・保持・
`show running-config`への反映ができるようになった。ただし
**実際のBGP EVPN NLRI（Type-2 MACルート、Type-3 IMETルート等）の
交換や、VXLANカプセル化そのものは実装していない**。PPPoEサーバ実装
（`docs/uptime-kuma-automation.md`と同時期に追加した機能）と同じ
深さ——設定の受理・保持・plausibleな`show`出力——に留まる。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`のHTTP APIに対するcurlコマンドで再現できる。
サーバーは実際にuvicornで起動して検証すること
（TestClientの制約は`docs/aws-directconnect-bgp-design.md`参照）。

## Ciscoルータ（IOS）でEVPNは張れるか？

**張れません。** EVPN/VXLANはデータセンターのリーフ・スプライン
構成で使う技術で、通常のCisco IOSルータ(ISR/ASR等)にはVXLAN EVPNの
コントロールプレーン機能は無い。

一方で**Catalyst 9000シリーズ(IOS-XE)はスタンドアロン構成で
VXLAN EVPNに対応している**（UADP ASIC世代からVXLANのハードウェア
カプセル化を持ち、IOS-XE 17.x以降でDNA Center無しでもBGP EVPNを
コントロールプレーンにしたVXLANが組める）。コマンド文法は
NX-OSとは異なり、`feature`コマンド自体が無く`l2vpn evpn`が起点になる
（詳細は本ドキュメント末尾の「Catalyst 9000 (IOS-XE) 版」節）。
このエミュレータでは`device_type in ('nexus', 'catalyst')`の両方に
対応している。

## 今回実装したコマンド体系

- `feature nv overlay` / `feature vn-segment-vlan-based`（既存の
  `feature <name>`受理機構をそのまま利用、追加実装なし）
- `vlan <id>` サブモード内 `vn-segment <vni>`（VLAN⇔VNIマッピング）
- `evpn` グローバルサブモード → `vni <id> l2` サブモード →
  `rd auto` / `route-target import|export|both auto`
- `interface nve1`（自動生成）→ `source-interface <if>` /
  `member vni <id> [associate-vrf]` サブモード →
  `ingress-replication protocol bgp` / `mcast-group <ip>`
- `router bgp <asn>` 配下 `address-family l2vpn evpn` サブモード →
  `neighbor <ip> activate` / `advertise-all-vni` /
  `exit-address-family`
- `show nve peers` / `show nve vni` / `show bgp l2vpn evpn summary`

## サンプル構成（Leaf-Leaf、VLAN 10 = VNI 10010）

```
[NX-Leaf1: Lo0 10.0.0.1/32] ===VXLAN(未実装/概念のみ)=== [NX-Leaf2: Lo0 10.0.0.2/32]
      VLAN10 (VNI 10010)                                    VLAN10 (VNI 10010)
```

### 投入コマンド（Leaf1側。Leaf2はIPを対称に入れ替えるだけ。`!`はVXLAN/EVPN本体の目印）

```
configure terminal
! --- 機能有効化(NX-OS特有。IOS-XEには"feature"コマンド自体が無い) ---
feature nv overlay              ! ← VXLAN(nve/カプセル化)機能そのもの
feature vn-segment-vlan-based   ! ← VLAN配下でvn-segment(VNI)を書けるようにする
feature bgp
! --- アンダーレイ: VTEPのループバック(VXLANカプセル化の送信元IP) ---
interface loopback0
ip address 10.0.0.1 255.255.255.255
exit
! --- VLAN10をVNI10010としてVXLANに乗せる対応付け ---
vlan 10
vn-segment 10010
exit
! --- ここからがEVPN(VXLANのコントロールプレーン)本体 ---
evpn
vni 10010 l2
rd auto                         ! Route Distinguisher(VNIごとの経路の重複防止)
route-target both auto
exit
exit
! --- ここがVXLANの実際のトンネル終端(カプセル化/デカプセル化)インタフェース ---
interface nve1
source-interface loopback0      ! ← VXLANパケットの送信元IP = loopback0
member vni 10010
ingress-replication protocol bgp ! ← VNI 10010のBUMトラフィックをVXLAN(ユニキャスト)で複製
exit
exit
! --- アンダーレイ: 拠点間でVTEP(loopback0)同士が到達できるようにするだけ。VXLAN自体はここでは張らない ---
router bgp 65001
address-family l2vpn evpn       ! ← ここがEVPNの制御プレーン(MAC/IP経路の交換)
neighbor 10.0.0.2 activate
advertise-all-vni
end
```

**注意（このエミュレータ固有のハマりどころ）**: サブモードが
`vlan`→`evpn`→`vni <n> l2`→`interface nve1`→`member vni <n>`と
何段にもネストするため、`exit`の回数を実機同様に正確に打つ必要が
ある。PPPoEの`crypto ipsec transform-set`のときと同様、
このエミュレータでは想定より多く`exit`するとモードが一段飛ばしで
戻ってしまう実装（`_cmd_exit`が未知のサブモードにいる場合は
config-if/configまで一気に抜ける）になっている箇所があるため、
コマンドを1行ずつ順番に投入し、想定通りのモードで受理されているか
（エラーが出ていないか）を都度確認するのが安全。

## 実際に確認できた結果

### `show running-config`（Leaf1側）

```
vlan 10
  vn-segment 10010

evpn
  vni 10010 l2
    rd auto
    route-target both auto

interface loopback0
  ip address 10.0.0.1/32
  no shutdown

interface nve1
  source-interface loopback0
  member vni 10010
    ingress-replication protocol bgp
  no shutdown

router bgp 65001
  address-family l2vpn evpn
    neighbor 10.0.0.2 activate
    advertise-all-vni
```

VLAN↔VNIマッピング、EVPN VNIインスタンスのRD/RT、nve1インタフェースの
source-interfaceとmember vni、BGPのaddress-family l2vpn evpn配下の
neighbor activate/advertise-all-vniが、いずれも投入した通りに
再現されている。

### `show nve peers`

```
Interface  Peer-IP          State LearnType Uptime   Router-Mac
nve1       10.0.0.2         Up    CP        00:00:10 n/a
```

### `show nve vni`

```
Interface VNI      Multicast-group  State Mode Type [BD/VRF]  Flags
nve1      10010    n/a              Up    CP   L2
```

### `show bgp l2vpn evpn summary`

```
BGP summary information for VRF default, address family L2VPN EVPN
BGP router identifier 0.0.0.0, local AS number 65001
Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
10.0.0.2        4 65001 0       0       0        0    0    00:00:10  0

advertise-all-vni: enabled
```

## 未実装（今後の課題）

`show nve peers`の「Up」は**`address-family l2vpn evpn`配下で
`neighbor activate`されているかどうかだけを見て常にUpと表示している**
（IPsecのDPDやRIP/OSPF/EIGRPのように実際のプロトコルタイマーで
状態遷移するエンジンではない）。実際にEVPNらしい検証
（MACアドレスの学習・広報、VNI単位のブリッジドメイン分離、
Type-2/Type-5ルートの伝播）をしたい場合は、少なくとも以下が要る:

1. **BGP EVPNアドレスファミリの実データプレーン**: 現状の
   `BgpEngine`（`engine/protocols.py`）はIPv4ユニキャストのRIBしか
   扱っていない。EVPN NLRI（Route Type 2: MAC/IP、Route Type 3:
   Inclusive Multicast Ethernet Tag）を扱う別テーブルが必要
2. **MACアドレス学習のシミュレーション**: どのVNIでどのMACが
   どのVTEP配下にいるかを追跡する状態
3. **VXLANカプセル化のデータプレーン**: 現状のping/tracerouteは
   L3到達性のみのシミュレーションで、VNI単位のL2到達性
   （同一VLAN/VNIのホスト同士だけ疎通する）は再現されていない

これらは今回のPPPoE/IPsec検証より一段大掛かりな実装になるため、
「設定が通ってrunning-configに正しく反映される」レベルで一旦区切った。

## Nexus Dashboard風ファブリックビュー（追記）

Cisco Nexus Dashboard（旧DCNM/Nexus Dashboard Fabric Controller）の
ような「ファブリック全体を俯瞰するダッシュボード」を模したビューを
追加した。実際のNexus DashboardのAPI/データモデルの再現ではなく、
このエミュレータ内のNexus装置に投入されたCLI設定を集計して
ダッシュボード形式に投影したもの。

- **画面**: `/static/nexus_dashboard.html`
- **データAPI**: `GET /api/nexus/dashboard`（`device_type in
  ('nexus', 'catalyst')`の装置を集計。認証は他のAPIと同じ
  セッショントークン方式）

返すJSONの主な項目:

```jsonc
{
  "fabric": {"switch_count": 2, "vtep_count": 2, "vni_count": 1, "vxlan_ready_count": 2},
  "switches": [
    {
      "device_id": "nd-demo1", "hostname": "ND-Demo-Leaf1", "role": "VTEP",
      "features": ["bgp", "nv overlay", "vn-segment-vlan-based"],
      "overlay_enabled": true, "vxlan_ready": true,
      "bgp_asn": 65001, "evpn_address_family": true, "advertise_all_vni": true,
      "vlan_vni_map": [{"vlan": 10, "vni": 10010}],
      "nve_peers": [{"interface": "nve1", "peer_ip": "10.1.1.2", "state": "Up"}],
      "member_vnis": [{"vni": 10010, "interface": "nve1", "type": "L2",
                        "ingress_replication": true, "mcast_group": null}]
    }
  ],
  "vnis": [{"vni": 10010, "l2vpn_evpn": true, "switches": ["nd-demo1", "nd-demo2"]}]
}
```

`vxlan_ready`は「`feature nv overlay`が有効・`nve1`にVNIメンバーが
構成済み・`address-family l2vpn evpn`が有効」の3条件が揃っている
ことだけを見て判定している（実際にBGP EVPN NLRIが交換されている
ことの確認ではない）。

### 実際に確認した動作

2台のNexus（`nd-demo1`/`nd-demo2`）に本ドキュメント前半のサンプル
構成を投入したところ、ダッシュボードは以下の通り表示された:

- SWITCHES: 14（トポロジー上の全Nexus装置。他は未構成なので
  PARTIAL表示）
- VTEPS: 2、VNIS: 1、VXLAN READY: 2
- `ND-Demo-Leaf1` / `ND-Demo-Leaf2` カードに `VTEP` / `VXLAN READY`
  バッジ、`nve1`のPeer IPと`Up`状態、VNI 10010がVLAN 10に
  マッピングされていることが表示された
- VNIインベントリ表に`10010`が両スイッチの参加として一覧化された

### 実装時に見つけた既存バグ（今回修正）

NX-OSの`feature <name>`受理ロジック（`engine/rules.py`）は
`\S+`で1語だけを切り出していたため、`feature nv overlay`が
`feature nv`として保存され、`overlay`が欠落していた
（他のNX-OS feature名—`ospf`/`bgp`/`vpc`/`eigrp`/
`vn-segment-vlan-based`—はすべて1語なので今まで顕在化していな
かった）。`nv overlay`だけ2語であることを先に個別マッチする形で
修正し、`show running-config`にも`feature nv overlay`
（およびこれまで欠落していた`feature vn-segment-vlan-based`
/`feature bgp`）を出力するようにした。

## Catalyst 9000 (IOS-XE) 版

NX-OSと同じ「VLANをVNIにマッピングしてnve1で終端し、BGP EVPNで
コントロールプレーンを持たせる」考え方は同じだが、**IOS-XEには
`feature`コマンドが無く**、コマンド文法が変わる。装置の内部状態
（`state.nve`/`state.evpn_vnis`/`state.vlan_vn_segment`）はNX-OSと
共通のデータモデルに正規化して持たせているので、`show
running-config`・`show nve peers`・`show nve vni`・
`/api/nexus/dashboard`はどちらの装置種別でも同じ形で確認できる。

| 項目 | NX-OS (Nexus) | IOS-XE (Catalyst 9000) |
|---|---|---|
| 機能有効化 | `feature nv overlay`等 | 不要（`l2vpn evpn`が起点） |
| EVPNインスタンス | `evpn` → `vni <n> l2` | `l2vpn evpn` → `instance <n> vlan-based` |
| VLAN⇔VNI | `vlan <n>` 配下 `vn-segment <n>` | `vlan configuration <n>` 配下 `member evpn-instance <n> vni <n>` |
| ingress-replication | `ingress-replication protocol bgp` | `ingress-replication`（`protocol bgp`無し） |
| nve1インタフェース | 共通（`source-interface`/`member vni`） | 共通 |
| BGP address-family | 共通（`address-family l2vpn evpn`） | 共通 |

### 投入コマンド（`!`はどこがVXLAN/EVPN本体かの目印）

```
configure terminal
! --- アンダーレイ: VTEPのループバック(VXLANカプセル化の送信元IP) ---
interface loopback0
ip address 10.0.0.1 255.255.255.255
exit
! --- ここからがEVPN(VXLANのコントロールプレーン)本体 ---
l2vpn evpn
replication-type ingress          ! BUM(ブロードキャスト等)をingress-replicationで運ぶ
router-id loopback0
instance 10010 vlan-based         ! EVPN InstanceとVNIを結びつける
encapsulation vxlan               ! ← このインスタンスの実データ経路がVXLAN
exit
exit
! --- VLAN10をVNI10010としてVXLANに乗せる対応付け ---
vlan configuration 10
member evpn-instance 10010 vni 10010
exit
! --- ここがVXLANの実際のトンネル終端(カプセル化/デカプセル化)インタフェース ---
interface nve1
no shutdown
source-interface loopback0        ! ← VXLANパケットの送信元IP = loopback0
host-reachability protocol bgp    ! MACアドレス学習をBGP EVPN経由にする(フラッド+学習ではない)
member vni 10010
ingress-replication                ! ← VNI 10010のBUMトラフィックをVXLAN(ユニキャスト)で複製
exit
exit
! --- アンダーレイ: 拠点間でVTEP(loopback0)同士が到達できるようにするだけ。VXLAN自体はここでは張らない ---
router bgp 65001
address-family l2vpn evpn         ! ← ここがEVPNの制御プレーン(MAC/IP経路の交換)
neighbor 10.0.0.2 activate
end
```

**VXLANの実体はどこにあるか**: 上のコメントの通り、`interface nve1`が
実際にVXLANパケット（UDP/4789でカプセル化されたL2フレーム）を
送受信する唯一の場所です。`l2vpn evpn`や`router bgp`のaddress-family
はあくまで「どこにMACがいるか」を交換する制御プレーンで、
それ自体はVXLANパケットを運びません。`router bgp`のグローバル部分
（`neighbor 10.0.0.2 remote-as ...`）はさらにその手前の**アンダーレイ**
（VTEP同士がIPで到達できるようにするだけの、ただのルーティング）で、
VXLAN/EVPNの話とは別レイヤーです。

### 実際に確認できた結果（`show running-config`）

```
l2vpn evpn
 replication-type ingress
 router-id loopback0
 instance 10010 vlan-based
  encapsulation vxlan
!
vlan configuration 10
 member evpn-instance 10010 vni 10010
!
interface nve1
 source-interface loopback0
 host-reachability protocol bgp
 member vni 10010
  ingress-replication
 no shutdown
!
router bgp 65001
 neighbor 10.0.0.2 remote-as 65002
 address-family l2vpn evpn
  neighbor 10.0.0.2 activate
 exit-address-family
```

`/api/nexus/dashboard`でも`role: "VTEP"`・`vxlan_ready: true`・
`nve_peers`にピアが載ることを確認した（Catalystの場合`features`は
`feature`コマンドが無いため`["l2vpn evpn"]`という1要素だけになる、
というのがNexusとの実装上の違い）。

回帰テストは`tests/test_evpn_vxlan.py`の`TestCatalyst9000Evpn`
（NX-OSとIOS-XEのコマンドが互いに誤って受理されないことの確認も含む）。

## 関連ドキュメント

- `docs/ipsec-cisco-cisco-verified.md` / `docs/ipoe-ipsec-verified.md`
  — 同時期に検証したIPsec関連
- `docs/feature-inventory.md` — 機能インベントリ（EVPN/VXLANの節を
  追加予定）
