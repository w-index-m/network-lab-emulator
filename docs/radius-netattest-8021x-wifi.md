# RADIUS / NetAttest / 802.1X / Wi-Fi認証

ユーザー依頼「ネットワーク運用計画radiusなど運用系を伸ばしたい、もしくは
ネットアテストとWi-Fi認証のようなツールを実装して欲しい」への対応。
範囲は「段階1+2+3(Wi-Fiまで)」を選択いただいた。

| 段階 | 内容 |
|---|---|
| 土台 | NetAttest EPS風のRADIUSサーバ装置(`device_type: netattest`)。**本物のRADIUS(UDP 1812/1813)** |
| 1 | Cisco/Catalyst(とNexusの設定)の`radius server`/`aaa authentication login ... group radius` → **実SSH/Telnetログインが本物のRADIUS問い合わせで認証される** |
| 2 | Catalystの有線802.1X。PCの`nmcli`(NetworkManager)がサプリカント。未認証ポートは通信遮断、認証成功で動的VLAN・アカウンティング |
| 3 | Catalyst 9800 WLC(`device_type: wlc`)。WPA2-Enterprise(802.1X)/WPA2-PSKのSSIDに、PCが`nmcli`で接続 |

## 構成と実装の位置

```
 PC(nmcli) ──eth0── Catalyst(802.1X認証スイッチ) ─┐
                                                  ├─ 本物のRADIUS(UDP) ─→ NetAttest(port1:1812/1813)
 PC(nmcli) ──wlan0── C9800 WLC(無線) ─────────────┘
 SSH/Telnetクライアント → Catalyst/Cisco(実SSHリスナー) ─ 本物のRADIUS ─┘
```

- `engine/radius_server.py` — RFC 2865/2866のパケット処理・サーバ・クライアント。
  PAP / CHAP / EAP-MD5(RFC 3748/3579、Message-Authenticator・State・
  Access-Challengeの往復)。Accept時に`Cisco-AVPair shell:priv-lvl=N`・
  `Service-Type`・動的VLAN(`Tunnel-Type=VLAN`/`Tunnel-Medium-Type=IEEE-802`/
  `Tunnel-Private-Group-ID`)・`Class`を返す。
- `engine/aaa_radius.py` — NAS側(cisco/catalyst/nexus/wlc)のRADIUS設定・
  メソッドリスト判定・サーバフェイルオーバー・`test aaa`・`show aaa servers`。
- `engine/nac.py` — 802.1X(スイッチ側)・C9800 WLC・PCの`nmcli`。
- `engine/rules.py` `_netattest_process` — NetAttestのCLI。

### 実機との差(正直な記載)

- **NetAttest EPSは実機ではWeb管理画面で設定する製品で公開CLIが無い**。
  画面の設定項目(RADIUSクライアント/ユーザー/動的VLAN/認証ログ/
  アカウンティングログ)をCLIに置き換えた独自コマンド体系。
  RADIUSのパケット自体は本物で、FreeRADIUSの`radtest`/`radclient`から
  そのまま使える(下記ライブ検証)。
- サプリカント⇔オーセンティケータ間のEAPOLは実L2フレームでは送らず、
  vnetのリンクで「どのポート/どのWLCに繋がっているか」を解決する
  (擬似FLET'S PPPoEの`PppoeEngine`と同じ方式)。**オーセンティケータ⇔
  RADIUSサーバ間は本物のUDP**。
- **EAP-PEAP/EAP-TTLSはTLSトンネルを実装していない**。内側の資格情報は
  EAP-MD5で検証し、`show access-session ... details`や
  `show wireless client ... detail`に
  `EAP-PEAP (emulated: no TLS tunnel, inner credentials verified via EAP-MD5)`
  と明記する。EAP-TLS・MS-CHAPv2は非対応。802.11の暗号化(4-way
  handshake)も扱わない。
- Nexusには実SSHリスナーが無い(NX-OSの`feature ssh`は未モデル化)ため、
  NexusはRADIUS設定・`test aaa`・`show running-config`まで。実SSHログインの
  RADIUS認証はCisco/Catalyst/WLC。

## 使い方(コマンド例)

### NetAttest(RADIUSサーバ)

```
configure
ip port1 address 10.250.0.10/24
radius-client 10.250.0.21 secret testing123 name SW1      ← NAS(スイッチ等)の送信元IPで登録
user alice password pass1 vlan 30 group staff privilege 15
user bob password bobpw
user bob disable
service radius                                           ← UDP 1812/1813で待ち受け開始
end
show radius status | show radius clients | show users
show radius log | show radius accounting | show running-config
clear radius log
```

実機のRADIUSサーバと同じく、**未登録の送信元からの要求には応答しない**
(黙って破棄し、`show radius log`に`drop unknown RADIUS client`と残る)。

### 段階1: 機器ログインをRADIUSで認証(Catalyst/Cisco/WLC)

```
aaa new-model
radius server NA1
 address ipv4 10.250.0.10 auth-port 1812 acct-port 1813
 key testing123
aaa group server radius NAC
 server name NA1
aaa authentication login default group NAC local
crypto key generate rsa modulus 2048
! 旧構文/NX-OS: radius-server host 10.250.0.10 key testing123
! 動作確認: test aaa group NAC alice pass1 legacy / show aaa servers
```

IOSのメソッドリスト仕様どおり、**サーバがRejectを返したらlocalには
進まない**。サーバ無応答(タイムアウト)のときだけ`local`へフォールバックする。
権限はRADIUSの`shell:priv-lvl`で決まる(15なら`#`、無ければ1で`>`)。

### 段階2: 有線802.1X(Catalyst)

```
aaa authentication dot1x default group NAC
aaa authorization network default group NAC        ← これが無いと動的VLANは適用しない(実機同様)
aaa accounting dot1x default start-stop group NAC
dot1x system-auth-control
interface GigabitEthernet1/0/5
 switchport mode access
 switchport access vlan 10
 authentication port-control auto
 dot1x pae authenticator
! show access-session [interface Gi1/0/5 details] / show dot1x all
! clear access-session interface Gi1/0/5
```

PC側(NetworkManagerの実構文):

```
nmcli connection add type ethernet con-name corp ifname eth0 802-1x.eap peap 802-1x.phase2-auth mschapv2 802-1x.identity alice 802-1x.password pass1
nmcli connection up corp
nmcli connection down corp
nmcli device status / nmcli connection show
```

### 段階3: Wi-Fi(Catalyst 9800 WLC)

```
aaa authentication dot1x CORP-AUTH group NAC
aaa accounting identity default start-stop group NAC
wireless profile policy CORP-POL
 aaa-override                       ← RADIUSのVLAN属性でポリシーのVLANを上書き
 vlan 100
 no shutdown
wireless tag policy default-policy-tag
 wlan CORP policy CORP-POL
wlan CORP 1 CORP-SSID               ← 作成直後はWPA2+802.1X・shutdown(実機同様)
 security dot1x authentication-list CORP-AUTH
 no shutdown
wlan GUEST 2 GUEST-SSID
 no security wpa akm dot1x
 security wpa akm psk
 security wpa psk set-key ascii 0 guestpass123
 no shutdown
! show wlan summary / show wireless client summary
! show wireless client mac-address <mac> detail / show wireless profile policy summary
```

PCはwlan0をWLCにリンクしておく(「電波が届く」相当):

```
nmcli device wifi list
nmcli connection add type wifi con-name corp ifname wlan0 ssid CORP-SSID wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 802-1x.identity alice 802-1x.password pass1
nmcli connection up corp
nmcli device wifi connect GUEST-SSID password guestpass123
```

## ライブ検証

### 本物のFreeRADIUSクライアント(`radtest`/`radclient`)との相互接続

`apt-get install freeradius-utils`で入れた実ツールから、このエミュレータの
RADIUSサーバへ問い合わせた結果。属性は実FreeRADIUSの辞書で正しく
デコードされている。

```
$ radtest alice pass1 10.250.0.10 0 testing123
Received Access-Accept Id 52 from 10.250.0.10:1812 to 10.250.0.10:34590 length 67
	Service-Type = Administrative-User
	Cisco-AVPair = "shell:priv-lvl=15"
	Tunnel-Type:0 = VLAN
	Tunnel-Medium-Type:0 = IEEE-802
	Tunnel-Private-Group-Id:0 = "30"
$ radtest -t chap alice pass1 10.250.0.10 0 testing123
Received Access-Accept Id 188 ...（同上）
$ radtest alice wrong 10.250.0.10 0 testing123
Received Access-Reject Id 35 from 10.250.0.10:1812 ...
	Reply-Message = "Authentication failed"
$ echo "User-Name=alice,Acct-Status-Type=Start,Acct-Session-Id=abc123" | radclient -x 10.250.0.10:1813 acct testing123
Received Accounting-Response Id 100 from 10.250.0.10:1813 ...
```

**検証中に踏んだ点**: 最初は`radius-client 127.0.0.1`で登録して応答が
無かった。radtestのパケットはNAS-IP-Address属性こそ127.0.0.1だが、
**UDPの送信元IPはカーネルが選んだ10.250.0.10**(宛先がループバック
エイリアスなので)で、実機のRADIUSサーバと同じく送信元IPでクライアントを
照合するため未登録扱いで破棄されていた。送信元IPで登録して解決
(サーバの挙動としては正しい)。

### 段階1: 本物のSSHクライアント(paramiko)でのログイン

```
ssh alice/pass1   : OK prompt=SW1#      ← RADIUS shell:priv-lvl=15
ssh bob/bobpw     : OK prompt=SW1>      ← priv-lvl無し=1
ssh alice/wrong   : AUTH FAILED
ssh admin/localpw : AUTH FAILED         ← Rejectではlocalに進まない(IOS仕様)
SW1# test aaa group NAC alice pass1 legacy
Attempting authentication test to server-group NAC using radius
User was successfully authenticated.
--- NetAttestで no service radius(サーバ無応答) ---
ssh admin/localpw : OK prompt=SW1#      ← タイムアウト時のみlocalへフォールバック
ssh alice/pass1   : AUTH FAILED
```

### 段階2: 有線802.1X

```
=== 802.1X前 ===
[pc1] ping -c 2 10.250.0.21     → 100% packet loss      ← 未認証ポートは遮断
[pc1] nmcli connection up corp  (bob/誤パスワード)
Error: Connection activation failed: 802.1X supplicant failed
[sw1] show access-session       → Gi1/0/5 ... Unauth
[pc1] nmcli connection up corp  (alice, PEAP)
Connection successfully activated (D-Bus active path: ...)
=== 802.1X後 ===
[sw1] show access-session interface Gi1/0/5 details
            User-Name:  alice
               Status:  Authorized
           EAP Method:  EAP-PEAP (emulated: no TLS tunnel, inner credentials verified via EAP-MD5)
            Vlan Group:  Vlan: 30
[sw1] show interfaces status → Gi1/0/5 ... 30          ← 動的VLAN(10→30)
[pc1] ping -c 2 10.250.0.21     → 0% packet loss
[pc1] nmcli connection down corp
[sw1] show interfaces status → Gi1/0/5 ... 10          ← 元のVLANに戻る
[pc1] ping -c 1 10.250.0.21     → 100% packet loss
[na1] show radius accounting
Start alice 10.250.0.21 GigabitEthernet1/0/5 0x900d3694 -
Stop  alice 10.250.0.21 GigabitEthernet1/0/5 0x900d3694 1
```

**検証中に見つけて直した実バグ(2件)**:

1. **PCの`ping`が常に0% lossを返していた**(既存バグ)。`_pc_ping`は
   宛先にもトポロジーにも関係なく成功を返す固定出力で、同じPCの
   `traceroute`はicmp_engineを使っているのにpingだけ使っていなかった。
   そのため802.1Xで遮断したポート越しでも、存在しないIP宛でもpingが
   通って見えていた。tracerouteと同じicmp_engineで判定するよう修正し、
   到達不可は実Linux同様`Destination Host Unreachable`/
   `100% packet loss`を返す。
2. 802.1Xの未認証ブロック(vnetのdownインタフェース)を、設定順で後から
   打った`no shutdown`が外してしまい、自前の「ブロック済み」記録とずれて
   再適用されなかった。ブロックを毎回入れ直す(冪等にする)よう修正。

### 段階3: Wi-Fi

```
[pc2] nmcli device wifi list
        AB:76:82:2A:A7:EC  CORP-SSID   Infra  36  540 Mbit/s  82  ▂▄▆█  WPA2 802.1X
        01:72:F8:8F:72:1C  GUEST-SSID  Infra  36  540 Mbit/s  82  ▂▄▆█  WPA2
[pc2] nmcli connection up corp  (誤パスワード) → 802.1X supplicant failed
[pc2] nmcli connection up corp  → Connection successfully activated
[wlc1] show wireless client mac-address 022d.9ebf.64e8 detail
Client Username : alice
Policy Profile : CORP-POL
VLAN : 30                        ← ポリシーはvlan 100だがaaa-overrideでRADIUSの30
AAA Override Passed : Yes
[pc2] nmcli device wifi connect GUEST-SSID password nope         → (7) Secrets were required, but not provided.
[pc2] nmcli device wifi connect GUEST-SSID password guestpass123 → successfully activated (Method PSK)
[pc2] nmcli device wifi connect NOPE-SSID password x             → Error: No network with SSID 'NOPE-SSID' found.
```

検証中に直した点: WLCで`aaa new-model`が受理されず(AAAブロックが
cisco/catalyst限定だった)running-configにAAA/WLAN設定が一切出なかった、
Accounting-StopのNAS-Port-IdがStartと食い違っていた、存在しないSSIDへの
`nmcli dev wifi connect`がゴミの接続プロファイルを残していた。

### ブラウザUI(Playwright)

ランチャー画面の「+ NetAttest (RADIUS)」「+ C9800 WLC」から装置を追加して
ラボ起動、ターミナルから上記コマンドが通ることを確認した
(プロンプト`WLC12(config-wlan)#`、ポート候補、設定貼り付け時の装置種別
自動判定も確認、JSエラー無し)。ランチャー側のボタン一覧にはFortiGateも
抜けていたので併せて追加した。

## 永続化

`_save_config()`/`_load_config()`に、RADIUSサーバ定義/グループ・
`aaa new-model`・ログイン用メソッドリスト・ローカルユーザ・dot1x設定・
WLAN/ポリシー設定・NetAttest設定・PCのnmcliプロファイルを追加した。
再起動後はRADIUSサーバ(実UDPソケット)を自動で再起動し、802.1Xポートは
未認証ブロック状態に戻る。認証済みセッション・無線クライアント・
RADIUSログ等のランタイム状態は保存しない(再認証で作り直す、BGP/OSPF
セッションと同じ方針)。

## テスト

`tests/test_radius_netattest_8021x_wifi.py`(34 tests): パケット処理、
PAP/CHAP/EAP-MD5の受理/拒否、未登録クライアントの破棄、共有シークレット
不一致の検出、アカウンティング、NetAttestのCLI、実SSH(paramiko)での
RADIUSログイン(権限/Reject時にlocalへ進まない/無応答時のフォールバック)、
Nexusの旧構文、802.1X(遮断/失敗/動的VLAN/ログオフ/アカウンティング/
`aaa authorization network`無しではVLAN非適用/非802.1Xポートでの
タイムアウト)、Wi-Fi(スキャン/Enterprise成功+aaa-override/誤パスワード/
PSK/存在しないSSID)、PCのpingの到達性判定、永続化の往復。
