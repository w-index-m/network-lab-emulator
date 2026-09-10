# NETCONF（Catalyst / Cisco IOS-XE）実装と検証記録

`netconf-yang` を有効にすると立ち上がる、**実物のNETCONFサーバ**の実装記録。
ncclient のような本物のNETCONFクライアントから接続して
`get-config` / `get` / `edit-config` が実行できる。

RESTCONF（`docs/restconf-catalyst.md`）と同じ `ietf-interfaces` データ
モデルを共有しているため、NETCONFで書いた設定は CLI の
`show ip interface brief` や RESTCONF からも見える。

## 実装ファイル

- `engine/netconf_agent.py` — NETCONFサーバ本体
- `app.py` — `netconf-yang` 投入時にリスナーを起動（`ensure_netconf_agent`）
- `tests/test_netconf.py` — プロトコル層のテスト

## 対応範囲

| 項目 | 状態 |
|---|---|
| SSH transport + `netconf` サブシステム (TCP 830) | 対応（paramiko） |
| `<hello>` 交換 / capabilities | 対応 |
| base:1.0 の `]]>]]>` 終端 | 対応 |
| base:1.1 のチャンク framing (RFC 6242) | 対応 |
| `<get-config source=running>` | 対応 |
| `<get>` | 対応 |
| `<edit-config target=running>` merge/replace/delete | 対応（ietf-interfaces） |
| `<close-session>` / `<kill-session>` | 対応 |
| subtree フィルタ | 部分対応（ietf-interfaces以外は空を返す） |
| candidate/startup データストア、commit、validate | **未対応** |
| notification（テレメトリ）、XPathフィルタ、with-defaults | **未対応** |

認証はローカルユーザDBがエミュレータに無いため **admin / admin** 固定。
（`username` コマンドは受理されるが装置状態に保存されない）

## 手順

```bash
# 1. 装置を作ってIPを付け、netconf-yang を有効化
curl -X POST http://127.0.0.1:8000/api/device \
  -H 'Content-Type: application/json' \
  -d '{"id":"sw1","type":"catalyst","hostname":"sw1"}'

# CLIで:
#   configure terminal
#   interface GigabitEthernet1/0/1
#    no switchport
#    ip address 10.79.0.1 255.255.255.0
#    no shutdown
#    exit
#   netconf-yang
#   exit
```

投入すると、アプリのログに以下が出てリスナーが上がる:

```
[NETCONF] sw1 (10.79.0.1:830) 実リスナーを起動しました
```

`show netconf-yang` でも確認できる:

```
NETCONF-YANG server status: Enabled
NETCONF-YANG server ssh port: 830
```

## ncclient からの接続例（実際に確認した出力）

```python
from ncclient import manager

m = manager.connect(host="10.79.0.1", port=830,
                    username="admin", password="admin",
                    hostkey_verify=False, allow_agent=False,
                    look_for_keys=False,
                    device_params={'name': 'default'}, timeout=20)
print(m.session_id)
print(m.get_config(source='running'))
```

### get-config の実際の出力

```xml
<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" message-id="urn:uuid:...">
 <data>
  <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
   <interface>
    <name>GigabitEthernet1/0/1</name>
    <type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">ianaift:ethernetCsmacd</type>
    <enabled>true</enabled>
    <description>#### To PC-1 ####</description>
    <ipv4 xmlns="urn:ietf:params:xml:ns:yang:ietf-ip">
     <address>
      <ip>10.79.0.1</ip>
      <netmask>255.255.255.0</netmask>
     </address>
    </ipv4>
   </interface>
   ...
```

### edit-config の実際の出力

```python
cfg = """<config>
  <interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces">
    <interface>
      <name>GigabitEthernet1/0/2</name>
      <description>configured-by-NETCONF</description>
      <enabled>true</enabled>
      <ipv4 xmlns="urn:ietf:params:xml:ns:yang:ietf-ip">
        <address><ip>10.79.9.9</ip><netmask>255.255.255.0</netmask></address>
      </ipv4>
    </interface>
  </interfaces>
</config>"""
m.edit_config(target='running', config=cfg)
```

```xml
<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" message-id="urn:uuid:..."><ok/></rpc-reply>
```

**CLI側にそのまま反映される**（同じデータモデルを共有しているため）:

```
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet1/0/1   10.79.0.1       YES NVRAM   up                    up
GigabitEthernet1/0/2   10.79.9.9       YES NVRAM   up                    up
```

存在しないインタフェースを指定した場合:

```
rpc-error: interface GigabitEthernet9/9/9 does not exist
```

## 実装中にハマった点（同種の実装をするとき注意）

1. **capability URI の `&` をXMLエスケープしていないとhelloが壊れる**
   `...?module=ietf-interfaces&revision=2014-05-08` の `&` をそのまま
   送ると、クライアント側で `EntityRef: expecting ';'` になり
   セッションが張れない。

2. **`<config>` を名前空間付きでしか探さないと取りこぼす**
   ncclient は渡されたXMLの名前空間宣言をそのまま使うため、
   `<config>` が NETCONF base 名前空間に入らないことがある。
   ローカル名で探す（`_child()` ヘルパ）必要がある。

3. **rpc-error のメッセージをエスケープしないと応答XMLごと壊れる**
   `<config> is required` のようにメッセージ中に `<` が入ると、
   クライアントが `Opening and ending tag mismatch` でパースに失敗する。
   上記2と組み合わさると「edit-configが失敗し、しかもエラー内容も
   読めない」という分かりにくい壊れ方になる。

## 関連ドキュメント

- `docs/restconf-catalyst.md` — 同じデータモデルを使うRESTCONF実装
- `docs/sir-catalyst-rip-ospf-bgp-stp-mpls-regression.md` — 他プロトコルの検証記録
