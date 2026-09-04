# Si-R G110B 実機比較（2026-09-04）

TeraTermマクロでログインした実機 **Si-R G110B** の `show running-config`
を、エミュレーターの新規Si-R装置のデフォルト出力と突き合わせた記録。

## 実機の出力（factory-default、初回ログイン直後）

```
Si-R G110B# show run
ether 1 1 vlan untag 1
ether 2 1 vlan untag 2
ether 2 2 vlan untag 2
ether 2 3 vlan untag 2
ether 2 4 vlan untag 2
lan 0 ip dhcp service client
lan 0 ip dhcp info time 1d
lan 0 ip rip use off v1 0 off
lan 0 ip nat mode multi any 1 5m
lan 0 vlan 1
lan 1 ip address 192.168.1.1/24 3
lan 1 ip dhcp service server
lan 1 ip dhcp info dns 192.168.1.1
lan 1 ip dhcp info address 192.168.1.2/24 253
lan 1 ip dhcp info time 1d
lan 1 ip dhcp info gateway 192.168.1.1
lan 1 ip rip use v1 v1 0 off
lan 1 vlan 2
syslog facility 23
time auto server 0.0.0.0 dhcp
time zone 0900
resource system vlan 4089-4094
consoleinfo autologout 8h
telnetinfo autologout 5m
terminal pager enable
terminal charset SJIS
alias history "show logging command brief"
```

ログイン時に`<WARNING> weak admin's password: set the password`が
出ることも確認（初期パスワードのままだと警告が出る）。

## 見つかった食い違いと修正

エミュレーターの`show running-config`は、末尾に**実機に存在しない
固定行を無条件で常時追加していた**（`app.py`の`_build_running_config`）。

| 修正前（存在しない/誤った行） | 修正後（実機に一致） |
|---|---|
| `consoleinfo authtype password` | `consoleinfo autologout 8h` |
| `telnetinfo authtype password` | `telnetinfo autologout 5m` |
| `rebootlog use on`（実機に存在しない） | 削除 |
| `syslog facility 1` | `syslog facility 23` |
| （`terminal pager enable`が丸ごと欠落） | 追加 |
| `save`（アクションコマンドなのに設定行として混入） | 削除 |

`terminal charset SJIS`は元々一致していた。

## まだ突き合わせていない部分（大きめの構造差）

実機は物理ポート(`ether`)をVLANに割り当て、論理インターフェース
`lan 0`/`lan 1`をそのVLANに紐付ける構成（`lan 0`=WAN側、DHCPクライアント
+NAT、`lan 1`=LAN側、DHCPサーバー）になっている。

エミュレーターのSi-Rは`lan 0`/`wan 1`という別々のインターフェース種別
を使うモデルで、この`ether`↔VLAN↔`lan`という対応関係そのものを持って
いない。これは今回の修正対象にした「末尾の固定行」よりずっと大きい
構造的な差で、既存のVRRP/RIP/OSPFテスト等がこの`lan 0`/`wan 1`モデルに
広く依存しているため、今回は変更していない。

`ether <slot> <port> vlan untag <vlan>` / `lan <n> ip dhcp service
client|server` / `lan <n> ip nat mode multi ...` / `lan <n> ip rip use
<recv> <send> <auth> <passive>` / `resource system vlan <range>` /
`time auto server <ip> dhcp` / `time zone <val>` / `alias <name>
"<command>"` は、コマンド自体はエラーにならず`show running-config`に
そのままエコーされることを確認済み（Si-Rの設定キャプチャは投入した
コマンドをほぼそのまま記録する方式のため）。ただし**意味的な動作
（実際にDHCPクライアントとして振る舞う、NATが効く等）までは
何も実装されていない**。

## テスト

`tests/test_sir_running_config_defaults.py`を新規作成（6件）。
