# Catalyst 9300：センター3台＋各拠点2台のスター型OSPF/HSRP構成

## 構成概要

モックCatalyst 9300を使用し、センター側に3台、各拠点に2台ずつ配置する構成です。
センターから各拠点へL3リンクを放射するスター型とし、拠点内の2台はHSRPでデフォルトゲートウェイを冗長化します。

> この構成はネットワークラボエミュレーター上で試すための設定例です。実機のIOS-XEと完全に同一ではない場合があるため、エミュレーターで利用可能なコマンドに合わせて調整してください。

## トポロジー

```text
                              Site-A
                    +-------------------------+
                    | Cat-A1 ==== Cat-A2      |
                    |  HSRP VIP 10.10.10.1    |
                    +----+---------------+----+
                         |               |
                         | OSPF          | OSPF
                         |               |
                    +----+---------------+----+
                    |       Center Core        |
                    |                           |
                    | C1 ----- C2 ----- C3      |
                    |  \       |       /        |
                    +---+------+-------+--------+
                        |      |       |
                      OSPF   OSPF    OSPF
                        |      |       |
              +---------+      |       +---------+
              |                |                 |
          Site-B             Site-C           （必要に応じて追加拠点）
       Cat-B1 ==== Cat-B2  Cat-C1 ==== Cat-C2
       VIP .20.1          VIP .30.1
```

### 役割

| 装置 | 役割 | OSPF Router ID | Loopback |
|---|---|---:|---|
| Center-C1 | センターコア | 1.1.1.1 | 1.1.1.1/32 |
| Center-C2 | センターコア | 1.1.1.2 | 1.1.1.2/32 |
| Center-C3 | センターコア | 1.1.1.3 | 1.1.1.3/32 |
| Site-A-Cat1 | 拠点A HSRP Active候補 | 10.10.10.11 | 10.10.10.11/32 |
| Site-A-Cat2 | 拠点A HSRP Standby候補 | 10.10.10.12 | 10.10.10.12/32 |
| Site-B-Cat1 | 拠点B HSRP Active候補 | 10.20.20.11 | 10.20.20.11/32 |
| Site-B-Cat2 | 拠点B HSRP Standby候補 | 10.20.20.12 | 10.20.20.12/32 |
| Site-C-Cat1 | 拠点C HSRP Active候補 | 10.30.30.11 | 10.30.30.11/32 |
| Site-C-Cat2 | 拠点C HSRP Standby候補 | 10.30.30.12 | 10.30.30.12/32 |

## アドレス設計

### 拠点LAN

| セグメント | HSRP VIP | Cat1 | Cat2 | 用途 |
|---|---|---|---|---|
| 10.10.10.0/24 | 10.10.10.1 | 10.10.10.11 | 10.10.10.12 | Site-A LAN |
| 10.20.20.0/24 | 10.20.20.1 | 10.20.20.11 | 10.20.20.12 | Site-B LAN |
| 10.30.30.0/24 | 10.30.30.1 | 10.30.30.11 | 10.30.30.12 | Site-C LAN |

### センター－拠点間のL3リンク

| 接続 | センター側 | 拠点側 |
|---|---|---|
| C1－Site-A-Cat1 | 172.16.11.1/30 | 172.16.11.2/30 |
| C2－Site-A-Cat2 | 172.16.12.1/30 | 172.16.12.2/30 |
| C2－Site-B-Cat1 | 172.16.21.1/30 | 172.16.21.2/30 |
| C3－Site-B-Cat2 | 172.16.22.1/30 | 172.16.22.2/30 |
| C3－Site-C-Cat1 | 172.16.31.1/30 | 172.16.31.2/30 |
| C1－Site-C-Cat2 | 172.16.32.1/30 | 172.16.32.2/30 |

センターコア間は次のL3リンクで接続します。

| 接続 | 片側 | 反対側 |
|---|---|---|
| C1－C2 | 172.16.100.1/30 | 172.16.100.2/30 |
| C2－C3 | 172.16.100.5/30 | 172.16.100.6/30 |
| C3－C1 | 172.16.100.9/30 | 172.16.100.10/30 |

## 共通設定

センターコアと各拠点スイッチで、L3ルーティングとOSPFを有効化します。

```ios
conf t
ip routing
router ospf 1
 router-id <ROUTER-ID>
 passive-interface default
 no passive-interface <L3接続ポート>
 network <接続ネットワーク> <ワイルドカード> area 0
 network <Loopbackネットワーク> 0.0.0.0 area 0
end
write memory
```

拠点LANのSVIはHSRPで利用するため、LAN側インターフェースはOSPFのpassive-interfaceにします。HSRPの仮想IPはOSPFに広告するため、SVIネットワーク自体はOSPFへ登録します。

## センターコア設定例

### Center-C1

```ios
conf t
hostname Center-C1
ip routing

interface Loopback0
 ip address 1.1.1.1 255.255.255.255
 no shutdown

interface GigabitEthernet1/0/1
 description L3-to-Center-C2
 no switchport
 ip address 172.16.100.1 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/2
 description L3-to-Site-A-Cat1
 no switchport
 ip address 172.16.11.1 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/3
 description L3-to-Site-C-Cat2
 no switchport
 ip address 172.16.32.1 255.255.255.252
 no shutdown

router ospf 1
 router-id 1.1.1.1
 passive-interface default
 no passive-interface GigabitEthernet1/0/1
 no passive-interface GigabitEthernet1/0/2
 no passive-interface GigabitEthernet1/0/3
 network 1.1.1.1 0.0.0.0 area 0
 network 172.16.11.0 0.0.0.3 area 0
 network 172.16.32.0 0.0.0.3 area 0
 network 172.16.100.0 0.0.0.3 area 0
end
write memory
```

### Center-C2

```ios
conf t
hostname Center-C2
ip routing

interface Loopback0
 ip address 1.1.1.2 255.255.255.255
 no shutdown

interface GigabitEthernet1/0/1
 description L3-to-Center-C1
 no switchport
 ip address 172.16.100.2 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/2
 description L3-to-Center-C3
 no switchport
 ip address 172.16.100.5 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/3
 description L3-to-Site-A-Cat2
 no switchport
 ip address 172.16.12.1 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/4
 description L3-to-Site-B-Cat1
 no switchport
 ip address 172.16.21.1 255.255.255.252
 no shutdown

router ospf 1
 router-id 1.1.1.2
 passive-interface default
 no passive-interface GigabitEthernet1/0/1
 no passive-interface GigabitEthernet1/0/2
 no passive-interface GigabitEthernet1/0/3
 no passive-interface GigabitEthernet1/0/4
 network 1.1.1.2 0.0.0.0 area 0
 network 172.16.12.0 0.0.0.3 area 0
 network 172.16.21.0 0.0.0.3 area 0
 network 172.16.100.0 0.0.0.3 area 0
 network 172.16.100.4 0.0.0.3 area 0
end
write memory
```

### Center-C3

```ios
conf t
hostname Center-C3
ip routing

interface Loopback0
 ip address 1.1.1.3 255.255.255.255
 no shutdown

interface GigabitEthernet1/0/1
 description L3-to-Center-C2
 no switchport
 ip address 172.16.100.6 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/2
 description L3-to-Center-C1
 no switchport
 ip address 172.16.100.10 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/3
 description L3-to-Site-B-Cat2
 no switchport
 ip address 172.16.22.1 255.255.255.252
 no shutdown

interface GigabitEthernet1/0/4
 description L3-to-Site-C-Cat1
 no switchport
 ip address 172.16.31.1 255.255.255.252
 no shutdown

router ospf 1
 router-id 1.1.1.3
 passive-interface default
 no passive-interface GigabitEthernet1/0/1
 no passive-interface GigabitEthernet1/0/2
 no passive-interface GigabitEthernet1/0/3
 no passive-interface GigabitEthernet1/0/4
 network 1.1.1.3 0.0.0.0 area 0
 network 172.16.22.0 0.0.0.3 area 0
 network 172.16.31.0 0.0.0.3 area 0
 network 172.16.100.4 0.0.0.3 area 0
 network 172.16.100.8 0.0.0.3 area 0
end
write memory
```

## 拠点内HSRP設定例

各拠点は同じLAN VLANを共有し、2台のCatalyst 9300でHSRPを動作させます。以下はSite-Aの例です。Site-BとSite-CではIPアドレス、HSRPグループ、優先度を置き換えてください。

### Site-A-Cat1（HSRP Active候補）

```ios
conf t
hostname Site-A-Cat1
ip routing

vlan 10
 name SITE-A-LAN

interface Vlan10
 description Site-A-default-gateway
 ip address 10.10.10.11 255.255.255.0
 standby version 2
 standby 10 ip 10.10.10.1
 standby 10 priority 110
 standby 10 preempt
 no shutdown

interface GigabitEthernet1/0/1
 description Trunk-to-Site-A-Cat2
 switchport mode trunk
 switchport trunk allowed vlan 10
 no shutdown

interface GigabitEthernet1/0/2
 description L3-to-Center-C1
 no switchport
 ip address 172.16.11.2 255.255.255.252
 no shutdown

router ospf 1
 router-id 10.10.10.11
 passive-interface default
 no passive-interface GigabitEthernet1/0/2
 network 10.10.10.0 0.0.0.255 area 0
 network 10.10.10.11 0.0.0.0 area 0
 network 172.16.11.0 0.0.0.3 area 0
end
write memory
```

### Site-A-Cat2（HSRP Standby候補）

```ios
conf t
hostname Site-A-Cat2
ip routing

vlan 10
 name SITE-A-LAN

interface Vlan10
 description Site-A-default-gateway
 ip address 10.10.10.12 255.255.255.0
 standby version 2
 standby 10 ip 10.10.10.1
 standby 10 priority 100
 standby 10 preempt
 no shutdown

interface GigabitEthernet1/0/1
 description Trunk-to-Site-A-Cat1
 switchport mode trunk
 switchport trunk allowed vlan 10
 no shutdown

interface GigabitEthernet1/0/2
 description L3-to-Center-C2
 no switchport
 ip address 172.16.12.2 255.255.255.252
 no shutdown

router ospf 1
 router-id 10.10.10.12
 passive-interface default
 no passive-interface GigabitEthernet1/0/2
 network 10.10.10.0 0.0.0.255 area 0
 network 10.10.10.12 0.0.0.0 area 0
 network 172.16.12.0 0.0.0.3 area 0
end
write memory
```

### Site-B / Site-Cへの置き換え

| 項目 | Site-B | Site-C |
|---|---|---|
| VLAN | 20 | 30 |
| LAN | 10.20.20.0/24 | 10.30.30.0/24 |
| HSRP VIP | 10.20.20.1 | 10.30.30.1 |
| Cat1 | 10.20.20.11 / priority 110 | 10.30.30.11 / priority 110 |
| Cat2 | 10.20.20.12 / priority 100 | 10.30.30.12 / priority 100 |
| HSRP group | 20 | 30 |

Site-Bのセンター接続はCat1が `172.16.21.2/30`、Cat2が `172.16.22.2/30`、Site-Cのセンター接続はCat1が `172.16.31.2/30`、Cat2が `172.16.32.2/30` です。

## 確認コマンド

### OSPF

```ios
show ip ospf neighbor
show ip ospf interface brief
show ip route ospf
show ip route 10.10.10.0
show ip route 10.20.20.0
show ip route 10.30.30.0
```

期待値：

- 各L3リンクの隣接が `FULL`
- センターコア3台が相互にOSPF隣接
- 各拠点から他拠点LAN（10.10.10.0/24、10.20.20.0/24、10.30.30.0/24）がOSPF経路として見える

### HSRP

```ios
show standby brief
show standby vlan 10
```

期待値（Site-A）：

```text
Group 10
Virtual IP 10.10.10.1
Site-A-Cat1  Active   priority 110
Site-A-Cat2  Standby  priority 100
```

### 疎通確認

```ios
ping 10.10.10.1
ping 10.20.20.1
ping 10.30.30.1
traceroute 10.30.30.1
```

## 障害試験

### HSRPフェイルオーバー

1. Site-A-Cat1で対象SVIを停止します。

```ios
conf t
interface Vlan10
 shutdown
end
```

2. Site-A-Cat2で確認します。

```ios
show standby brief
```

3. HSRP状態がCat2の `Active` に切り替わることを確認します。

### OSPF経路冗長性

センターから拠点への片側リンクを停止し、OSPFが代替経路へ収束することを確認します。

```ios
conf t
interface GigabitEthernet1/0/2
 shutdown
end

show ip ospf neighbor
show ip route ospf
```

復旧時は `no shutdown` を実行します。

## エミュレーター起動

```bash
git clone https://github.com/w-index-m/network-lab-emulator.git
cd network-lab-emulator
pip install -r requirements.txt
python app.py
```

ブラウザで `http://localhost:8000` を開き、Catalyst 9300を9台（センター3台＋3拠点×2台）起動して、上記設定を投入します。

## 検証チェックリスト

- [ ] Catalyst 9300を9台起動
- [ ] センターコア3台をL3リンクで相互接続
- [ ] 各拠点2台をL2トランクで接続
- [ ] 各拠点のHSRP VIPを設定
- [ ] センター－拠点間のL3リンクを設定
- [ ] 全装置でOSPF Router IDを一意に設定
- [ ] `show ip ospf neighbor` で隣接がFULL
- [ ] `show standby brief` で各拠点のActive/Standbyを確認
- [ ] 拠点間LANへのOSPF経路を確認
- [ ] HSRP Active停止後、StandbyがActiveへ遷移することを確認
- [ ] OSPFリンク停止後、代替経路へ収束することを確認

## 注意事項

- 物理Catalyst 9300では、拠点内のHSRP用リンクをL2トランク、センター接続をL3 routed portとして設計しています。
- エミュレーターが一部のIOS-XEコマンドを簡略化している場合は、`router ospf`、`network`、`neighbor`等の対応CLIへ読み替えてください。
- OSPFは同一エリア（Area 0）のシンプルな構成です。大規模化する場合は拠点ごとのエリア分割も検討できます。
- HSRPの仮想IPは端末のデフォルトゲートウェイとして使用します。各拠点LANのDHCPまたは端末設定には、対応するVIPを指定してください。
