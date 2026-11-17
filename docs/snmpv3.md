# SNMPv3(USM)

ユーザーの依頼:

- 「SNMPv2、v3 どっちも対応済み?」
- 「SNMPv3 も実装お願いします」

これまでエミュレータの SNMP は v1/v2c(community)だけだった。今回、v3 を次の3か所に入れた。

- エミュレータの SNMP エージェント
- ニルヴァーナ単体版の SNMP クライアント
- 監視 IaC(snmp_exporter)

## 対応範囲

| | 内容 |
|---|---|
| メッセージ | RFC 3412(v3)。engineID の発見、時刻合わせ(notInTimeWindows)、Report PDU |
| 認証 | HMAC-MD5-96 / HMAC-SHA-96(RFC 3414)、HMAC-SHA-2 224/256/384/512(RFC 7860) |
| 暗号 | DES-CBC(RFC 3414)、AES-128-CFB(RFC 3826)、AES-192/256 |
| 権限 | 簡易 VACM。グループの安全レベル(noauth/auth/priv)と read view(included/excluded の最長一致) |
| 機種 | cisco・catalyst・wlc・asa・arista・nexus |
| 未対応 | 3DES-EDE、v3 での SET、context、ACL(`access`)、v3 の trap/inform 送信 |

AES-192/256 は RFC が無く、鍵の伸ばし方が2通りある。

- IOS / IOS-XE / ASA: **Reeder 方式**。Cisco と同じで、net-snmp では `-x AES256C`。
- Arista EOS: **Blumenthal 方式**。net-snmp では `-x AES256`。

単体版のクライアントでは、`aes256c`(Reeder)と `aes256`(Blumenthal)を書き分ける。

## 設定コマンド

IOS / IOS-XE / ASA / Arista:

```
snmp-server view NMSVIEW iso included
snmp-server view NMSVIEW 1.3.6.1.2.1.4 excluded        ← ip 以下は見せない
snmp-server group NMS v3 priv read NMSVIEW
snmp-server user nms NMS v3 auth sha Auth-Pass1 priv aes 128 Priv-Pass1
snmp-server engineID local 800000090300AABBCCDDEEFF    ← 省略時は装置ごとに決まる Cisco 形式
show snmp user / show snmp group / show snmp engineID / show snmp view
```

NX-OS:

```
snmp-server user admin network-admin auth sha-256 Auth-Pass1 priv aes-128 Priv-Pass1
show snmp user / show snmp engineID
```

実機と同じく、パスワードそのものは保存しない。持つのは engineID で局所化した鍵だけで、`saved_config.json` にもパスワードは残らない。

- IOS は `snmp-server user` を running-config に出さない(実機と同じ)。
- NX-OS は `snmp-server user ... auth sha-256 0x… priv aes-128 0x… localizedkey` と鍵で出す。
  この行をそのまま入れ直すと、同じ鍵に戻る。
- engineID を変えると、局所化した鍵が合わなくなる。そのため、ユーザーの作り直しが必要だと警告する。

## 確かめたこと(本物のツールで)

### net-snmp の snmpget / snmpwalk → エミュレータ

```
$ snmpget -v3 -l authPriv -u nms -a SHA -A Auth-Pass1 -x AES -X Priv-Pass1 10.77.1.1 1.3.6.1.2.1.1.5.0
iso.3.6.1.2.1.1.5.0 = STRING: "V3R1"
$ snmpget -v3 -l authPriv -u nms256 -a SHA -A Auth-Pass1 -x AES256C -X Priv-Pass1 10.77.1.1 1.3.6.1.2.1.1.5.0
iso.3.6.1.2.1.1.5.0 = STRING: "V3R1"                       ← IOS の AES-256(Reeder)
$ snmpget -v3 -l authPriv -u nmsdes -a MD5 -A Auth-Pass1 -x DES -X Priv-Pass1 10.77.1.1 1.3.6.1.2.1.1.5.0
iso.3.6.1.2.1.1.5.0 = STRING: "V3R1"
$ snmpget -v3 -l authPriv -u admin -a SHA-256 -A Auth-Pass1 -x AES -X Priv-Pass1 10.77.2.1 1.3.6.1.2.1.1.5.0
iso.3.6.1.2.1.1.5.0 = STRING: "V3NX"                       ← NX-OS
$ snmpget -v3 -l authPriv -u u256 -a SHA-512 -A Auth-Pass1 -x AES256 -X Priv-Pass1 10.77.3.1 1.3.6.1.2.1.1.5.0
iso.3.6.1.2.1.1.5.0 = STRING: "V3EOS"                      ← Arista の AES-256(Blumenthal)
$ snmpwalk -v3 -l authPriv -u nms ... 10.77.1.1 1.3.6.1.2.1.4
iso.3.6.1.2.1.4 = No Such Object available on this agent at this OID   ← view で除外した ip 以下
$ snmpbulkwalk -v3 -l authPriv -u nms ... 10.77.1.1 1.3.6.1.2.1.31.1.1.1.1
iso.3.6.1.2.1.31.1.1.1.1.1 = STRING: "Gi0/0/0"
```

断られる場合も確かめた。

| 試したこと | net-snmp の表示 |
|---|---|
| 認証のパスワード違い | `Authentication failure (incorrect password, community or key)` |
| 暗号のパスワード違い | `Decryption error` |
| 存在しないユーザー | `Unknown user name` |
| priv のグループに auth だけで問い合わせ | `authorizationError (access denied to that object)` |
| noAuthNoPriv で問い合わせ | `authorizationError (access denied to that object)` |

同じ装置で v2c(`-c public`)も、これまで通り読める。

### 再起動しても使えるか

- `/api/save` のあと、エミュレータを再起動した。
- 同じユーザーで、IOS も NX-OS もそのまま読めた。engineID は装置 ID から決まり、局所化した鍵が保存されているため。
- 保存ファイルにパスワードの文字列が無いことも確認した。

### Prometheus の snmp_exporter(v0.28.0)→ エミュレータ

`iac/cisco-traffic-monitoring/snmp_exporter/snmp.yml` に auth `netlab_v3` を足した。値は SHA / AES、ユーザーは `netlab-v3`。
ラボを作るスクリプト `lab/provision_cisco_lab.sh` は、CR1 と CR2 に同じユーザーを作る。

```
$ curl 'http://127.0.0.1:9126/snmp?target=10.240.12.1&module=cisco_if&auth=netlab_v3'
ifHCInOctets{ifAlias="LAN tokyo",ifIndex="2",ifName="Gi0/0/1"} 0
ifHCInOctets{ifAlias="WAN to CR2 (osaka)",ifIndex="1",ifName="Gi0/0/0"} 0
snmp_scrape_duration_seconds{module="cisco_if"} 0.038
```

パスワードを変えた設定で試すと、`error getting target 10.240.12.1: wrong digest` になった。

v3 で監視するには、`prometheus.yml` の `params.auth` を `[netlab_v3]` に変える。既定は v2c のまま。

### ニルヴァーナ単体版のクライアント → エミュレータ、本物の snmpd

単体版の `cvs/snmp_client.py` は、`community` の代わりに `v3_user({...})` を渡すと v3 で問い合わせる。
`cvs/snmp_usm.py` は `engine/snmp_usm.py` のコピーなので、独自実装同士だけで話が合っていても意味がない。
そこで、本物の net-snmp の `snmpd` 相手にも試した。

| 相手 | 方式 | 結果 |
|---|---|---|
| エミュレータ | SHA / AES-128、SHA-512 / AES-256(Blumenthal)、SHA / AES-256C(Reeder) | 読めた |
| エミュレータ | パスワード違い | `wrongDigests` で断られた |
| net-snmp snmpd 5.9 | SHA / AES、MD5 / DES、SHA-256 / AES-256 | 読めた |

設定は `config.yaml` の `sources.snmp.v3`(装置ごとなら `devices[].snmp_v3`)に書く。パスワードは `${ENV}` で渡す。

## テスト

`tests/test_snmp_v3.py`(22件)。

- RFC 3414 の鍵のテストベクタ
- 6種類の暗号の往復
- Reeder と Blumenthal の違い
- IOS / NX-OS の設定・show・running-config(localizedkey の入れ直し)
- engineID の発見
- 4つの方式での GET
- 3種類の Report(unknownUserNames / wrongDigests / unsupportedSecLevels)と authorizationError
- 認証つきの notInTimeWindows
- view による除外
- v2c との共存
- 保存と復元(パスワードが残らないこと)
- 単体版クライアント
