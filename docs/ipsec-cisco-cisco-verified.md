# Cisco IOS ↔ Cisco IOS IPsec 検証済み設定

**目的**: Cisco ルータ2台間で実際にIPsecトンネルが確立することを
このエミュレータ上で確認し、動作する設定をそのまま記録する。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`（このリポジトリ）のHTTP APIに対する
curlコマンドで再現できる。**`fastapi.testclient.TestClient`は使わず、
実際にuvicornでサーバーを起動してcurlで検証すること**
（理由は`docs/aws-directconnect-bgp-design.md`の
「テストハーネスに関する注意」を参照。バックグラウンドタスクに
依存しないIPsecエンジン自体はTestClientでも動くはずだが、
今回はAWS検証と同じ手順に統一した）。

## 前提: PPPoEで払い出されたIPでは今回まだできない

「PPPoEでIPを払い出された2台のCiscoルータ間でIPsecを張れるか」という
問いが元になっているが、現状このエミュレータのPPPoEサーバ実装
（`bba-group pppoe` / `Virtual-Template` / `ip local pool`）は
**設定の受理・表示のみ**で、実際にPPPoEクライアント側がセッションを
確立してIPを動的に受け取る部分（PADI/PADO/PADR/PADS）は未実装。
そのため今回は、PPPoEを経由せず**手動でIPを設定した状態**で
IPsecが正しく張れることを確認した。IPsecエンジン自体は
「インタフェースに設定されているIP」を見て動くだけなので、
PPPoEクライアント機能を実装してIPが動的に入るようになれば、
今回と全く同じcrypto設定で「PPPoEで払い出されたIP同士のIPsec」も
成立する見込み。

## ネットワーク構成

```
10.1.0.0/24 --- [IOS-Tokyo: Gi0/0 203.0.113.1/30] ===IPsec(ESP)=== [IOS-Osaka: Gi0/0 203.0.113.2/30] --- 10.2.0.0/24
                 Loopback0: 10.1.0.1/24                              Loopback0: 10.2.0.1/24
```

| 項目 | 値 |
|---|---|
| Pre-Shared Key | `Cisco@VPN2024` |
| IKEポリシー | AES-256 / SHA-256 / DHグループ14 / pre-share / lifetime 86400 |
| Transform-set | `esp-aes-256 esp-sha256-hmac` |
| Crypto map | `IOSMAP` seq 10, ipsec-isakmp |

## 検証手順

サーバーを起動:
```bash
NETLAB_AUTH_DISABLE=1 nohup python3 -m uvicorn app:app --host 127.0.0.1 --port 8321 &
```

装置作成とリンク:
```bash
B=http://127.0.0.1:8321
curl -s -X POST $B/api/device -d '{"id":"vpn-tokyo","type":"cisco","hostname":"IOS-Tokyo"}'
curl -s -X POST $B/api/device -d '{"id":"vpn-osaka","type":"cisco","hostname":"IOS-Osaka"}'
curl -s -X POST $B/api/link -d '{"a":"vpn-tokyo","b":"vpn-osaka","iface_a":"GigabitEthernet0/0","iface_b":"GigabitEthernet0/0"}'
```

### IOS-Tokyo（東京側）投入コマンド

```
configure terminal
interface GigabitEthernet0/0
ip address 203.0.113.1 255.255.255.252
no shutdown
exit
interface Loopback0
ip address 10.1.0.1 255.255.255.0
exit
crypto isakmp policy 10
authentication pre-share
encryption aes 256
hash sha256
group 14
lifetime 86400
exit
crypto isakmp key Cisco@VPN2024 address 203.0.113.2
crypto ipsec transform-set TS-ESP256 esp-aes-256 esp-sha256-hmac
crypto map IOSMAP 10 ipsec-isakmp
match address 100
set peer 203.0.113.2
set transform-set TS-ESP256
exit
interface GigabitEthernet0/0
crypto map IOSMAP
exit
crypto isakmp enable GigabitEthernet0/0
end
```

### IOS-Osaka（大阪側）投入コマンド

IPとpeer/matchのASN番号以外は東京側と対称:

```
configure terminal
interface GigabitEthernet0/0
ip address 203.0.113.2 255.255.255.252
no shutdown
exit
interface Loopback0
ip address 10.2.0.1 255.255.255.0
exit
crypto isakmp policy 10
authentication pre-share
encryption aes 256
hash sha256
group 14
lifetime 86400
exit
crypto isakmp key Cisco@VPN2024 address 203.0.113.1
crypto ipsec transform-set TS-ESP256 esp-aes-256 esp-sha256-hmac
crypto map IOSMAP 10 ipsec-isakmp
match address 101
set peer 203.0.113.1
set transform-set TS-ESP256
exit
interface GigabitEthernet0/0
crypto map IOSMAP
exit
crypto isakmp enable GigabitEthernet0/0
end
```

**注意（このエミュレータ固有のハマりどころ）**: `crypto ipsec
transform-set <name> <transforms>`はこのエミュレータでは**サブモードに
入らない**（実機と異なり`mode tunnel`等のサブコマンドを持たない）。
このため`crypto ipsec transform-set ...`の直後に`exit`を打つと、
「configモードのままexitした」扱いになり**execモードまで抜けてしまう**
（`_cmd_exit`は未知のサブモードに居る場合、configモードからのexitとして
execまで抜ける実装のため）。実機の設定例をそのまま貼ると
`mode tunnel` / それに続く`exit`が原因で後続の`crypto map`以降が
全て「invalid input」になる。**transform-set設定後は`exit`を打たず
続けて次のコマンドを入力すること。**

同様に、`access-list <番号> permit ip <src> <wildcard> <dst>
<wildcard>`（Cisco IOS形式の拡張番号ACL）は、内部的には
`ipfilter_engine`に登録される一方で、`rule_engine`側の
Cisco/Catalyst用CLIパーサーには対応する表示ロジックが無く
**画面には「invalid input」と出る**（ASA向けの`access-list <name>
extended permit ...`表記のみ実装されている）。`crypto map`の
`match address`は実際にはACLの存在チェックをしていないため、
今回の検証ではACL自体の定義を省略し、`match address 100`を
未定義のまま参照する形にした（`show crypto map`には
`Extended IP access list 100`と表示されるが、実体は無い）。

## 実際に確認できた結果

### `show crypto ipsec sa`（東京側）

```
interface: GigabitEthernet0/0
    Crypto map tag: IOSMAP, local addr 203.0.113.1

   current_peer 203.0.113.2 port 500
   DPD status: ESTABLISHED
   PERMIT, flags={origin_is_acl,}
   #pkts encaps: 7236, #pkts encrypt: 640
   #pkts decaps: 1051, #pkts decrypt: 9866
   #send errors 0, #recv errors 0

     local crypto endpt.: 203.0.113.1, remote crypto endpt.: 203.0.113.2
     inbound esp sas:
      spi: 0xf359d3d8(decimal: 4082750424)
       Status: ACTIVE
     outbound esp sas:
      spi: 0x7b3d4896(decimal: 2067613846)
       Status: ACTIVE
```

### `show crypto ipsec sa`（大阪側）

```
interface: GigabitEthernet0/0
    Crypto map tag: IOSMAP, local addr 203.0.113.2

   current_peer 203.0.113.1 port 500
   DPD status: ESTABLISHED
   PERMIT, flags={origin_is_acl,}
   #pkts encaps: 6934, #pkts encrypt: 6078
   #pkts decaps: 9659, #pkts decrypt: 2683
   #send errors 0, #recv errors 0

     local crypto endpt.: 203.0.113.2, remote crypto endpt.: 203.0.113.1
     inbound esp sas:
      spi: 0x62b005ae(decimal: 1655702958)
       Status: ACTIVE
     outbound esp sas:
      spi: 0xfe491b82(decimal: 4266204034)
       Status: ACTIVE
```

両側とも`DPD status: ESTABLISHED`、inbound/outbound双方の
ESP SAが`Status: ACTIVE`になっており、peerアドレスも相互に
一致している。**Cisco IOSルータ2台間のIPsecトンネルは実際に
確立できる**ことを確認した。

### `show crypto map`（東京側）

```
Crypto Map "IOSMAP" 10 ipsec-isakmp
  Peer = 203.0.113.2
  Extended IP access list 100
  Security association lifetime: 4608000 kilobytes/28800 seconds
  Transform sets={ ts-esp256 }
  Interfaces using crypto map IOSMAP:
```

「Interfaces using crypto map」欄が空欄になる表示バグがあることも
確認した（`crypto map IOSMAP interface GigabitEthernet0/0`は
`show running-config`には正しく出るが、`show crypto map`側の
インタフェース一覧の集計には反映されていない）。トンネル自体の
確立（`show crypto ipsec sa`）には影響しない、表示専用の問題。

### 既知の未検証事項

- `show crypto isakmp sa`は`(none)`のまま（IKE Phase1のSA一覧表示は
  ネゴシエーション完了後にテーブルへ反映されない実装になっている
  可能性がある。Phase2の`show crypto ipsec sa`は正しく反映される）
- 装置IDを使い回すと、以前の検証で投入したACLやインタフェース定義が
  一部残留する挙動を確認した（`POST /api/device`は完全な状態リセット
  ではない可能性がある）。検証時は装置IDを毎回変えるのが無難。

## 関連ドキュメント

- `docs/pppoe-server.md`（未作成の場合は`docs/feature-inventory.md`の
  PPPoEサーバの節）— 今回「未実装」と整理したPPPoEクライアント側の
  詳細
- `docs/aws-directconnect-bgp-design.md` — TestClientの制約についての
  詳しい説明
- `samples/ipsec_vpn_samples.md` — Si-R↔Si-R、Si-R↔Cisco IOSを含む
  IPsecサンプル集（今回のCisco↔Ciscoパターンの元ネタ）
