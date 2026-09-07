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
構成で使う技術で、Cisco製品では**Nexusシリーズ(NX-OS)専用**の
機能。通常のCisco IOSルータ(ISR/ASR等)にはVXLAN EVPNの
コントロールプレーン機能は無い(一部ASRにVXLANのデータプレーンだけ
載る例はあるが、EVPNコントロールプレーンと組み合わせる構成は
基本Nexus/ACIの領域)。このエミュレータでも実装は`device_type ==
'nexus'`限定にしている。

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

### 投入コマンド（Leaf1側。Leaf2はIPを対称に入れ替えるだけ）

```
configure terminal
feature nv overlay
feature vn-segment-vlan-based
feature bgp
interface loopback0
ip address 10.0.0.1 255.255.255.255
exit
vlan 10
vn-segment 10010
exit
evpn
vni 10010 l2
rd auto
route-target both auto
exit
exit
interface nve1
source-interface loopback0
member vni 10010
ingress-replication protocol bgp
exit
exit
router bgp 65001
address-family l2vpn evpn
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
- **データAPI**: `GET /api/nexus/dashboard`（`device_type == 'nexus'`
  の装置だけを集計。認証は他のAPIと同じセッショントークン方式）

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

## 関連ドキュメント

- `docs/ipsec-cisco-cisco-verified.md` / `docs/ipoe-ipsec-verified.md`
  — 同時期に検証したIPsec関連
- `docs/feature-inventory.md` — 機能インベントリ（EVPN/VXLANの節を
  追加予定）
