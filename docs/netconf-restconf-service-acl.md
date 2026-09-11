# NETCONF / RESTCONF サービスレベルACL（Catalyst / IOS-XE）

管理プロトコル（NETCONF・RESTCONF）への着信を、**送信元アドレスだけで絞る**
ACL の実装記録。インタフェースに当てる通常のACLとは別物で、サービス単位に
適用する。

- 実装: `app.py` / `engine/protocols.py` / `engine/netconf_agent.py`
- テスト: `tests/test_service_level_acl.py`（14件）
- 関連: `docs/netconf-catalyst.md`, `docs/restconf-catalyst.md`

> **出典について**: cisco.com はこの環境のegressプロキシでブロックされている
> ため、公式ガイドを直接参照できていない。コマンド構文は二次情報
> （下記「参考」）で確認し、`show` の出力書式はIOS-XEの一般的な表記に
> 合わせている。公式マニュアルで差異が見つかったら直す前提。

## 対応コマンド

```
ip access-list standard <name>
 [<seq>] permit|deny {any | host A.B.C.D | A.B.C.D W.W.W.W}

netconf-yang ssh {ipv4|ipv6} access-list name <acl>
netconf-yang ssh port <n>
restconf {ipv4|ipv6} access-list name <acl>
```

`no` 形式もすべて対応。`netconf-yang ssh port` の `no` は既定の 830 に戻る。

## 設定例

```
ip access-list standard MGMT_ACL
 permit 10.99.0.0 0.0.0.255
 permit host 192.0.2.7
 deny   any
!
netconf-yang
netconf-yang ssh ipv4 access-list name MGMT_ACL
netconf-yang ssh port 8830
!
ip http secure-server
restconf
restconf ipv4 access-list name MGMT_ACL
```

## 実際の出力

```
ACL1# show ip access-lists
Standard IP access list MGMT_ACL
    10 permit 10.99.0.0, wildcard bits 0.0.0.255
    20 permit 192.0.2.7
    30 deny   any
```

running-config は**そのまま投入し直せる形**で出る:

```
ip access-list standard MGMT_ACL
 10 permit 10.99.0.0 0.0.0.255
 20 permit host 192.0.2.7
 30 deny any
!
restconf
restconf ipv4 access-list name MGMT_ACL
netconf-yang ssh ipv4 access-list name MGMT_ACL
netconf-yang ssh port 8830
```

## 実際に遮断されることの確認

許可されていない送信元からのRESTCONF:

```console
$ curl -u admin:admin http://127.0.0.1:8000/restconf/acl1/data/ietf-interfaces:interfaces
{"ietf-restconf:errors":{"error":[{"error-type":"transport",
  "error-tag":"access-denied",
  "error-message":"source 127.0.0.1 denied by RESTCONF service ACL \"MGMT_ACL\""}]}}
HTTP=403
```

`deny any` より前のシーケンス番号で許可を入れると通る（**先勝ち**）:

```
ACL1(config-std-nacl)# 5 permit host 127.0.0.1

ACL1# show ip access-lists
Standard IP access list MGMT_ACL
    5 permit 127.0.0.1          ← deny any より前なのでこちらが勝つ
    10 permit 10.99.0.0, wildcard bits 0.0.0.255
    20 permit 192.0.2.7
    30 deny   any
```

```console
$ curl -u admin:admin http://127.0.0.1:8000/restconf/acl1/data/ietf-interfaces:interfaces
{"ietf-interfaces:interfaces":{"interface":[{"name":"GigabitEthernet1/0/1",...
HTTP=200
```

**注意**: 後から `permit` を足しても、既に `deny any` がある場合は
シーケンス番号を明示しないと末尾に付くだけで効かない。実機と同じ挙動。

NETCONF側は `NetconfServer._loop` の accept 直後に判定し、
SSHネゴシエーションに入る前にTCPを切る（実機と同じく無応答になる）:

```
[NETCONF] acl1 203.0.113.1 をサービスレベルACLで拒否しました
```

## 実装中に直した既存の不具合

| 症状 | 原因 |
|---|---|
| `ip access-list standard` が通らない | 名前付き**標準**ACLのサブモードが未実装だった（拡張ACLのみ存在） |
| ACLがどのルールにも一致せず全部拒否 | `_match_addr` が `A.B.C.D W.W.W.W`（ワイルドカードマスク形式）を解釈できなかった |
| 標準ACLが `Extended IP access list` と表示 | ACL名から種別を判別できないのに、番号ACLの判定ロジックを流用していた。`acl_kind` を持たせて明示的に記録 |
| running-config を投入し直せない | ASA形式の1行表記（`access-list NAME permit ...`）で出力していた。IOS形式の入れ子に変更 |

## ハマりどころ

1. **新しい設定サブモードは2か所に登録が必要。**
   `engine/rules.py` の `process()` のモード許可リストと `_cmd_exit()` の
   両方に入れないと、コマンドがハンドラに届かず `% Invalid input` になる。
   今回は `config-std-nacl` を両方に追加した。
2. **存在しないACL名を指定しても全拒否にはしない。**
   実機は一致するACLが無ければ素通しなので、`check_source()` も
   ルール0件ならTrueを返す。ここをFalseにすると、ACL名のtypoで
   管理経路ごと閉め出される。
3. **サービスレベルACLは送信元しか見ない。** 宛先・ポート・プロトコルは
   評価対象外なので、`check_packet()` ではなく専用の `check_source()` を使う。

## 参考

- [Configuring and Verifying NETCONF and RESTCONF on Cisco IOS XE (AlphaPrep)](https://blog.alphaprep.net/configuring-and-verifying-netconf-and-restconf-on-cisco-ios-xe-for-ccnp-350-401-encor/)
- [Simple Netconf Security Configurations for Cisco Devices (Knowledge Addict)](https://knowledgeaddict.co.uk/2025/01/29/simple-netconf-security-configurations-for-cisco-devices/)
