# gNMI / gRPC / モデル駆動型テレメトリ（Catalyst / IOS-XE）

`gnxi server` を入れると立ち上がる**実物のgNMIサーバ**（gRPC）と、
`telemetry ietf subscription` によるモデル駆動型テレメトリ（MDT）の実装記録。

gnmic / pygnmi など**本物のgNMIクライアント**から
Capabilities / Get / Set / Subscribe が実行できる。
protoは openconfig/gnmi の `gnmi.proto` **原本**を
`grpc_tools.protoc` でコンパイルしたものを使っている（自作の擬似protoではない）。

NETCONF・RESTCONFと同じ `state.interfaces` を読み書きするため、
gNMIで書いた設定はCLIの `show running-config` からもそのまま見える。

## 実装ファイル

- `engine/gnmi_proto/gnmi.proto` — openconfig/gnmi の原本
- `engine/gnmi_proto/gnmi_pb2*.py` — protocでコンパイルしたスタブ
- `engine/gnmi_agent.py` — gNMIサーバ本体
- `app.py` — `gnxi` 系コマンド、MDT、`show gnxi state` / `show telemetry ...`
- `tests/test_gnmi.py`（23件）, `tests/test_mdt_telemetry.py`（13件）

> **出典について**: cisco.com はこの環境のegressプロキシでブロックされて
> いるため公式ガイドを直接参照できていない。CLI構文とMDTのshow出力書式は
> 下記「参考」の二次情報で確認した。`show gnxi state detail` だけは
> 正確な桁揃えを確認できる資料が無く、IOS-XEの一般的なstate表示に
> 合わせている。公式マニュアルで差異が見つかったら直す前提。

## 対応コマンド

**IOS-XE 17.3以降は `gnmi-yang` ではなく `gnxi` 系**（ここが変わっている）:

```
gnxi                        … 機能の有効化
gnxi server                 … 非TLSサーバ（既定ポート 50052）
gnxi port <n>
gnxi secure-init            … 自己署名トラストポイント(gnxi-cert)を作る
gnxi secure-server          … TLSサーバ（既定ポート 9339）
gnxi secure-port <n>
gnxi secure-password-auth
show gnxi state [detail]
```

`gnxi` より先に `gnxi server` を入れるとエラーになる（実機同様）。
`secure-server` も `secure-init` が先に必要。

## gNMI 実行結果（実際のgRPC越し）

### Capabilities

```
gNMI version: 0.8.0
  Cisco-IOS-XE-native (Cisco Systems, Inc.) 2026-09-01
  ietf-interfaces (IETF) 2014-05-08
  openconfig-interfaces (OpenConfig working group) 2021-04-06
  encodings: ['JSON_IETF', 'JSON']
```

### Get

```python
stub.Get(gnmi_pb2.GetRequest(
    path=[P('ietf-interfaces:interfaces',
            'interface[name=GigabitEthernet1/0/1]')],
    encoding=gnmi_pb2.JSON_IETF))
```

```json
{
  "name": "GigabitEthernet1/0/1",
  "type": "iana-if-type:ethernetCsmacd",
  "enabled": true,
  "description": "uplink-to-core",
  "ietf-ip:ipv4": {
    "address": [{"ip": "10.77.0.1", "netmask": "255.255.255.0"}]
  }
}
```

### Set → CLIに反映される

```python
u.path.CopyFrom(P('ietf-interfaces:interfaces',
                  'interface[name=GigabitEthernet1/0/1]', 'description'))
u.val.json_ietf_val = json.dumps("configured-by-gNMI").encode()
stub.Set(req)          # op: ['UPDATE']
```

```
GN1# show running-config
interface GigabitEthernet1/0/1
 description configured-by-gNMI      ← gNMIで書いた内容
 ip address 10.77.0.1 255.255.255.0
 no shutdown
```

### Subscribe

**ONCE**:

```
  Cisco-IOS-XE-native:native/hostname = "GN1"
  ietf-interfaces:interfaces/interface/description = "configured-by-gNMI"
sync_response: True
```

**STREAM / SAMPLE**（`sample_interval=1_000_000_000` = 1秒）:

```
  t=1789118672960556288 hostname="GN1"
  sync_response
  t=1789118673961027072 hostname="GN1"      ← 約1秒後
  t=1789118674961529344 hostname="GN1"      ← さらに約1秒後
```

**POLL**（pollリクエストのたびに応答＋sync_response）:

```
  poll応答: hostname="GN1"
  sync_response #1
  poll応答: hostname="GN1"
  sync_response #2
```

## モデル駆動型テレメトリ（MDT / gRPC Dial-Out）

```
telemetry ietf subscription 101
 encoding encode-kvgpb
 filter xpath /process-cpu-ios-xe-oper:cpu-usage/cpu-utilization/five-seconds
 source-address 10.77.0.1
 stream yang-push
 update-policy periodic 500
 receiver ip address 10.77.0.99 57500 protocol grpc-tcp
```

**`period` はセンチ秒**（500 = 5秒）。最小値は100（1秒）。

```
GN1# show telemetry ietf subscription all
Telemetry subscription brief

ID               Type        State       Filter type
-----------------------------------------------------
101              Configured  Valid       xpath
```

```
GN1# show telemetry ietf subscription 101 detail
Subscription ID: 101
Type: Configured
State: Valid
Stream: yang-push
Filter type: xpath
XPath: /process-cpu-ios-xe-oper:cpu-usage/cpu-utilization/five-seconds
Update Trigger: periodic
Period: 500
Encoding: encode-kvgpb
Source Address: 10.77.0.1

Receivers:
Address          Port             Protocol
------------------------------------------------------------------
10.77.0.99       57500            grpc-tcp
```

```
GN1# show telemetry ietf subscription 101 receiver
Subscription ID: 101
Address: 10.77.0.99
Port: 57500
Protocol: grpc-tcp
State: Connected
```

`stream` / `encoding` / `filter` / `update-policy` / `receiver` が
**揃って初めて `State: Valid`** になる（揃うまでは `Invalid`）。

## ハマりどころ

1. **gNMIのパスを文字列に潰してはいけない。**
   `"ietf-interfaces:interfaces/interface[name=GigabitEthernet1/0/1]"` を
   `"/"` で分割すると、**Ciscoのインタフェース名に含まれるスラッシュで
   バラバラになり**パスが解決できない。最初これで実装してGetが
   `NOT_FOUND` になった。`gnmi.Path` の `elem`（名前＋キー辞書）のまま
   扱うこと（`path_elems()`）。`path_to_str()` は表示・エラー用のみ。
2. **`gnxi port` の変更は再起動しないと効かない。**
   gRPCサーバは起動時にbindするので、ポート変更時はstop→startしている。
3. **`filter xpath` は元の大小文字を保つ。**
   `/Cisco-IOS-XE-memory-oper:...` を小文字化すると別モデル名になる。
   このコードベースの慣習（小文字化した `c` で判定）に流されないこと。
4. **grpcioは必須依存にしない。**
   未導入環境では `_HAS_GRPC=False` になり、gNMI機能だけ無効化されて
   アプリ自体は起動する。テストも `pytest.mark.skipif` で退避する。

## 未対応（実機との差）

- TLS（`gnxi secure-server`）は設定を保持するだけで、実際のTLS待受は未実装
- gNOI（OS/Cert/Reset等のオペレーション用RPC）
- MDTは設定と表示のみで、**実際にreceiverへgRPCでデータを送出しない**
  （送出まで作るならreceiver側のgRPCサーバも要る）
- `ON_CHANGE` サブスクリプションは受け付けるが変更契機での通知は未実装
  （SAMPLEのみ実動作）

## 参考

- [openconfig/gnmi (gnmi.proto 原本)](https://github.com/openconfig/gnmi)
- [jeremycohoe/cisco-ios-xe-gnmi](https://github.com/jeremycohoe/cisco-ios-xe-gnmi)
- [jeremycohoe/cisco-ios-xe-programmability-lab-module-5-gnmi](https://github.com/jeremycohoe/cisco-ios-xe-programmability-lab-module-5-gnmi)
- [jeremycohoe/cisco-ios-xe-mdt](https://github.com/jeremycohoe/cisco-ios-xe-mdt)

## 関連ドキュメント

- `docs/netconf-catalyst.md` — 同じデータモデルを使うNETCONF
- `docs/netconf-restconf-service-acl.md` — サービスレベルACL
- `docs/model-based-aaa-nacm.md` — NACM
