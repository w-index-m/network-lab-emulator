# モデルベースAAA（NACM / RFC 8341）— Catalyst / IOS-XE

Cisco IOS-XE の「モデルベースAAA」の実体は **NACM（NETCONF Access Control
Model, RFC 8341）**。NETCONF/RESTCONF からの読み書きを、ユーザが属する
**グループ単位**で許可/拒否する。設定はCLIではなく **NETCONF経由
（`/nacm` サブツリー）** で行うのが規格。

- 実装: `engine/netconf_agent.py` / `app.py`
- テスト: `tests/test_nacm.py`（20件）, `tests/test_local_users.py`（10件）
- 関連: `docs/netconf-catalyst.md`, `docs/netconf-restconf-service-acl.md`

> **出典**: cisco.com・datatracker.ietf.org・netconfcentral.org はいずれも
> この環境のegressプロキシでブロックされている。データモデルは
> **GitHubのYangModels/yang から取得した `ietf-netconf-acm@2018-02-14.yang`
> 原本**（RFC 8341収録のモジュール）に基づいており、leaf名・既定値・
> bit名はそこから直接確認した。Cisco独自の拡張部分は未確認。

## データモデルと既定値（YANG原本どおり）

```
container nacm
  leaf enable-nacm            boolean   default true
  leaf read-default           permit|deny  default permit
  leaf write-default          permit|deny  default deny     ← 既定は書けない
  leaf exec-default           permit|deny  default permit
  leaf enable-external-groups boolean   default true
  leaf denied-operations / denied-data-writes / denied-notifications (counter)
  container groups
    list group { key name; leaf-list user-name }
  list rule-list { key name; leaf-list group
    list rule { key name
      leaf module-name        default "*"
      leaf rpc-name | notification-name | path
      leaf access-operations  default "*"   (bits: create read update delete exec)
      leaf action             permit|deny
      leaf comment } }
```

**要点は `write-default = deny`**。つまり既定は「読めるが書けない」。

## 前提となるCLI設定

```
aaa new-model
aaa authorization exec default local
!
username netadmin privilege 15 secret cisco123
username alice    privilege 5  secret alice123
username bob      privilege 1  secret bob123
!
netconf-yang
```

`username` は従来「受理されるが保存されない」状態だったため、今回
privilege 付きで装置状態に保存するようにした（NETCONFの認証と
NACMのprivilege判定の両方がこれを見る）。

## 復旧セッション（重要）

`write-default=deny` が既定なので、**NACMを完全に迂回できる管理者が
いないと「書けないのでNACMの書き込み許可も設定できない」** という
デッドロックになり、装置を永久に締め出す。RFC 8341 3.3 の
**recovery session** がこれを防ぐ。

本実装の復旧セッション判定（`_is_recovery`）:

1. `ndm-admin` / `PRIV15` グループに属するユーザ
2. ローカルユーザが定義されていれば **privilege 15 のユーザ**
3. ローカルユーザDBが空なら組み込みの `admin`（privilege 15相当）

## 実際の動作（ncclientで確認した出力）

### 既定状態の `/nacm`

```xml
<nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
  <enable-nacm>true</enable-nacm>
  <read-default>permit</read-default>
  <write-default>deny</write-default>
  <exec-default>permit</exec-default>
  <enable-external-groups>true</enable-external-groups>
  <denied-operations>0</denied-operations>
  <denied-data-writes>0</denied-data-writes>
  <denied-notifications>0</denied-notifications>
</nacm>
```

### NACM未設定のまま3ユーザで edit-config を試す

```
netadmin  edit-config -> OK（変更された）        ← privilege 15 = 復旧セッション
alice     edit-config -> 拒否: access-denied / access denied by NACM
bob       edit-config -> 拒否: access-denied / access denied by NACM
```

### netadmin でルールを投入

```python
m.edit_config(target='running', config="""<config>
  <nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
    <groups>
      <group><name>netops</name><user-name>alice</user-name></group>
    </groups>
    <rule-list>
      <name>netops-rules</name>
      <group>netops</group>
      <rule><name>allow-iface</name>
        <module-name>ietf-interfaces</module-name>
        <access-operations>create update delete</access-operations>
        <action>permit</action></rule>
    </rule-list>
  </nacm>
</config>""")
# -> <ok/>
```

### 投入後にもう一度

```
alice     edit-config -> OK（変更された）        ← netopsグループに所属
bob       edit-config -> 拒否: access-denied / access denied by NACM
```

拒否カウンタも規格どおり増える:

```
denied-operations  3
denied-data-writes 3
```

aliceが書いた内容はCLI側にもそのまま反映される（同じデータモデルを
共有しているため）:

```
NACM2# show running-config
interface GigabitEthernet1/0/1
 description by-alice-2
 ip address 10.87.0.1 255.255.255.0
```

## 判定アルゴリズム（RFC 8341 3.4）

1. `enable-nacm=false` なら全許可
2. 復旧セッションなら全許可
3. ユーザの所属グループを集める
4. `rule-list` を順に見て、グループが一致するものの `rule` を**先頭から**評価。
   `module-name` / `access-operations` / `path` が一致した最初のルールの
   `action` を返す（**先勝ち**）
5. どのルールにも当たらなければ
   `read-default` / `write-default` / `exec-default` を使う

## ハマりどころ

1. **`write-default=deny` のブートストラップ問題。**
   復旧セッションを用意しないと誰もNACMを設定できなくなる。
   `tests/test_nacm.py::test_recovery_user_can_bootstrap_nacm` で固定している。
2. **`handle_rpc` にユーザ名を渡さないと拒否される。**
   実セッションはSSH認証を通ってからでないと `handle_rpc` に到達しないので、
   ユーザ名なしの呼び出しは「未認証」として扱う。既存のNETCONFテストは
   `user='admin'` を渡すよう更新した。
3. **ローカルユーザを1人でも定義すると、組み込み `admin` の特別扱いは消える。**
   privilege 15 のユーザを必ず1人作ること。

## 実装中に直した既存の不具合

| 症状 | 原因 |
|---|---|
| `username` を入れても消える | 受理するだけで装置状態に保存していなかった |
| `aaa new-model` がCatalystで効かない | ハンドラをNX-OS専用関数 `_handle_nexus_tacacs_config` の中に置いてしまっていた（`aaa new-model` はIOSのコマンド） |
| インタフェースの `description` を入れても既定のまま | 実装がApresia専用ハンドラにしか無く、Cisco/Catalystでは `_cmd_config` 末尾の「不明な設定コマンドは静かに受け付け」に落ちて捨てられていた |
| running-config に `description` が出ない | インタフェース節に出力処理が無かった。NETCONFで書いた結果がCLIから確認できなかった |

## 未対応（実機との差）

- `enable-external-groups`（TACACS+/RADIUS由来のグループ）は値を保持する
  だけで、外部グループの取り込みは未実装
- `notification-name` ルールと `denied-notifications` カウンタ（通知が未実装のため）
- `path` のXPath評価は部分一致による簡易判定
- RESTCONF側のNACM適用（現状はNETCONFのみ。RESTCONFはサービスレベルACLで制御）
