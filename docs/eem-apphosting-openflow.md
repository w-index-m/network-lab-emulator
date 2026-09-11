# EEM / アプリケーションホスティング / OpenFlow（Catalyst / IOS-XE）

プログラマビリティ第3弾。実装記録。

- 実装: `engine/programmability.py`, `app.py`（`_handle_programmability`）
- テスト: `tests/test_programmability_batch3.py`（36件）

> **出典について**: cisco.com はこの環境のegressプロキシでブロックされて
> いるため公式ガイドを直接参照できていない。コマンド構文と `show` の
> 出力書式は下記「参考」の二次情報（実機出力を貼った公開リポジトリ）で
> 確認した。公式マニュアルで差異が見つかったら直す前提。

---

## 1. EEM（Embedded Event Manager）

**`action N cli command "..."` は表示用のモックではなく、実際に
ルールエンジンへコマンドを流して装置の設定を変える。** つまり
applet から本当に装置を操作できる。

### 対応コマンド

```
event manager applet <name> [authorization bypass]
 event none                              … 手動実行用（テストの定石）
 event syslog pattern "<regex>"
 event cli pattern "<regex>" [sync yes|no]
 event timer watchdog time <sec>
 action <seq> syslog msg "<text>"
 action <seq> cli command "<cli>"
 action <seq> puts "<text>"
!
event manager policy <file> type user|system
event manager directory user policy <dir>
event manager run <name>
show event manager policy registered | available
show event manager history events
show event manager statistics
show event manager directory user
```

### 実際の出力

設定例（**実機のEEMは `action cli command` を exec コンテキストで
実行するので、設定を変えるには applet 内で `configure terminal` を
通す必要がある**。これが定石）:

```
event manager applet TEST-APPLET
 event none
 action 1.0 syslog msg "applet fired"
 action 2.0 cli command "configure terminal"
 action 3.0 cli command "hostname RENAMED-BY-EEM"
 action 4.0 cli command "end"
 action 5.0 puts "done"
```

```
P3B# show event manager policy registered
No.  Class     Type    Event Type          Trap  Time Registered           Name
1    applet    user    none                Off   Fri Sep11 12:07:24 2026  TEST-APPLET
 1.0 syslog msg "applet fired"
 2.0 cli command "configure terminal"
 3.0 cli command "hostname RENAMED-BY-EEM"
 4.0 cli command "end"
 5.0 puts "done"
```

```
P3B# event manager run TEST-APPLET
%HA_EM-6-LOG: TEST-APPLET: applet fired
done
```

**appletが本当に設定を変えている**:

```
P3B# show running-config | include ^hostname
hostname RENAMED-BY-EEM          ← applet実行で変わった
```

```
P3B# show event manager history events
No.  Job Id Proc Status   Time of Event             Event Type    Name
1    1      Actv success  Fri Sep11 12:07:24 2026   none          applet: TEST-APPLET
```

### 実機どおりの制約

- **`action cli command` は exec コンテキストで実行される。**
  設定変更するappletは `configure terminal` を明示的に通す
  （この挙動は `end` の修正で判明した。修正前は `end` がサブモードを
  一段しか戻らなかったため、装置がconfigモードに居座って
  `hostname` が直接効いてしまっていた）
- **`event none` のappletだけが手動実行できる。**
  syslogトリガのappletに `event manager run` すると拒否される
  （実機でもappletのテストには `event none` を使うのが定石）
- **アクションは投入順ではなくシーケンス番号順に実行される。**
  `3.0 → 1.0 → 2.0` の順に入れても `1.0 → 2.0 → 3.0` で走る
- syslogイベントは正規表現でマッチする（`notify_syslog()`）

### Pythonポリシー

```
event manager directory user policy flash:/
event manager policy eem_script.py type user
```

`show event manager policy registered` に `script` クラスとして出る。
**スクリプトの中身は実行しない**（後述「未対応」）。

---

## 2. アプリケーションホスティング（IOx / Docker）

### 状態遷移（実機どおり）

```
(未導入) --install--> DEPLOYED --activate--> ACTIVATED --start--> RUNNING
             ^                      ^                                |
             |                      +---------- stop ----------------+
        uninstall              deactivate
```

`uninstall` は DEPLOYED からのみ。RUNNING のまま消そうとすると拒否される。

### 設定例

```
iox
!
app-hosting appid syslogng
 app-vnic AppGigabitEthernet trunk
  vlan 46 guest-interface 0
   guest-ipaddress 10.1.1.9 netmask 255.255.255.0
 app-default-gateway 10.1.1.3 guest-interface 0
 app-resource docker
 app-resource profile custom
  cpu 3700
  memory 1792
  persist-disk 200
  vcpu 1
```

```
app-hosting install appid syslogng package usbflash1:syslogng/syslogng.tar
app-hosting activate appid syslogng
app-hosting start appid syslogng
```

### 実際の出力

```
C9300# show app-hosting list
App id                                   State
---------------------------------------------------------
syslogng                                 RUNNING
```

```
C9300# show app-hosting detail appid syslogng
App id                 : syslogng
Owner                  : iox
State                  : RUNNING
Application
  Type                 : docker
  Name                 : syslogng
  Version              : v1
  Path                 : usbflash1:syslogng/syslogng.tar
Activated profile name : custom

Resource reservation
  Memory               : 1792 MB
  Disk                 : 200 MB
  CPU                  : 3700 units
  VCPU                 : 1

Attached devices
  Type              Name               Alias
  ---------------------------------------------
  serial/shell     iox_console_shell   serial0
  ...
Network interfaces
   ---------------------------------------
eth0:
   MAC address         : 52:54:dd:00:00:2e
   IPv4 address        : 10.1.1.9
   Network name        : vlan46
```

リソースは**実際に引き算される**:

```
C9300# show app-hosting resource
CPU:
  Quota: 7400(Units)
  Available: 3700(Units)        ← 7400 - 3700
Memory:
  Quota: 2048(MB)
  Available: 256(MB)            ← 2048 - 1792
```

### 実機どおりの検証

```
C9300# app-hosting install appid syslogng package ...
% IOx is not enabled. Configure "iox" first.

C9300# app-hosting activate appid syslogng          （install前）
% App syslogng is not installed

C9300# app-hosting start appid syslogng             （activate前）
% App syslogng is in state DEPLOYED, cannot start

C9300# app-hosting uninstall appid syslogng         （RUNNING中）
% App syslogng is in state RUNNING. Deactivate it before uninstalling.
```

---

## 3. OpenFlow（faucetパイプライン）

### 対応コマンド

```
boot mode openflow          … リロードが必要（実機同様）
feature openflow
openflow
 switch <n> pipeline <p>
  controller ipv4 <ip> port <port> [vrf <vrf>] [security none|tls]
  datapath-id 0x<hex>
  probe-interval <sec>
  logging flow-mod
show openflow switch <n> [controllers | flows | ports]
```

### 実機どおりの前提条件

**通常のスイッチングモードのままでは `feature openflow` が通らない**:

```
SW-OF(config)# feature openflow
% OpenFlow requires the switch to be in OpenFlow boot mode.
  Configure "boot mode openflow" and reload.

SW-OF(config)# boot mode openflow
Changes to the boot mode preferences have been stored
%% Reload the switch to apply the new boot mode
```

### 実際の出力

```
SW-OF# show openflow switch 1
Logical Switch Context
  Id: 1
  Switch type: Forwarding
  Pipeline id: 1
  Data plane: secure
  Table-Miss default: drop
  Configured protocol version: Negotiate
  Config state: no-shutdown
  Working state: enabled
  DPID: 0xabcdef1234
  Number of tables: 9
  Capabilities: FLOW_STATS TABLE_STATS PORT_STATS
  Controllers: 2
```

```
SW-OF# show openflow switch 1 controllers
Total Controllers: 2
  Controller: 1
    192.168.0.91:6653
    Protocol: tcp
    VRF: Mgmt-vrf
    Connected: Yes
    Role: Equal
    Negotiated Protocol Version: OpenFlow 1.3
  ...
```

ポート一覧は**CLIのshutdown状態を反映する**:

```
SW-OF# show openflow switch 1 ports
Port    Interface Name   Config-State     Link-State
   1           Gi1/0/1      PORT_DOWN      LINK_DOWN    ← shutdown済み
   2           Gi1/0/2        PORT_UP        LINK_UP
```

---

## ハマりどころ

1. **設定サブモードは `engine/rules.py` の `CONFIG_SUBMODES` 登録簿に
   1行足すだけでよい**（親モードと exit 時に消す属性を書く）。
   以前は `process()` のモード許可リストと `_cmd_exit()` の2か所に
   別々に書く必要があり、片方を忘れるとコマンドがハンドラに届かず
   `% Invalid input detected` になった。現在は両方をこの表から導出し、
   `tests/test_config_submodes.py` が全モードを総当たりで検証する。
2. **入れ子サブモードは親を登録簿に書くだけ。**
   `config-openflow-switch` の親は `config-openflow`。`exit` は
   一段ずつ戻り、`end` はどこからでも exec まで戻る。
3. **引用符を含むコマンドはシェル経由でテストしない。**
   `curl -d '{"command":"action 1.0 syslog msg \"x\""}'` はシェルの
   クォート処理で壊れる。検証中これで「アクションが保存されない」と
   誤診しかけた（実際にはPythonドライバで流すと正常だった）。
   **JSONを組み立てるのはPython側でやること。**
4. **actionのシーケンス番号は小数**（`1.0` / `2.5`）。
   文字列ソートすると `10.0` が `2.0` より前に来るので、
   `float()` でソートする。

## 未対応（実機との差）

- **EEM Pythonポリシーのスクリプト本体は実行しない。**
  登録と一覧表示のみ。実行するにはGuestShell（CentOSコンテナ）の
  再現が要る
- Guest Shell そのもの（`guestshell enable` / `guestshell run python`）
- App Hosting は**実際にコンテナを動かさない**。状態遷移・リソース
  計算・ネットワーク設定の再現まで
- OpenFlow は**コントローラと実際にTCP接続しない**。`Connected: Yes`
  は設定上の表示で、フローも table-miss の1件が固定で出るだけ
  （本物にするならOpenFlow 1.3のTCPチャネル実装が要る）
- `event timer watchdog` は登録できるが、時間経過で自動発火しない

## 参考

- [jasoncdavis/AppHosting-SyslogNG](https://github.com/jasoncdavis/AppHosting-SyslogNG) — `show app-hosting detail` の実機出力
- [jeremycohoe/c9kwireshark](https://github.com/jeremycohoe/c9kwireshark) — app-hosting の設定とライフサイクル
- [faucetsdn/faucet — Cisco向けドキュメント](https://github.com/faucetsdn/faucet/blob/main/docs/vendors/cisco/README_Cisco.rst) — OpenFlow設定とshow出力
- [networklessons — Cisco IOS Embedded Event Manager](https://networklessons.com/system-management/cisco-ios-embedded-event-manager)
- [LookingPoint — Guestshell with EEM scripting](https://www.lookingpoint.com/blog/understanding-the-power-of-guestshell-with-eem-scripting)
