# RESTCONF（Catalyst / Cisco IOS-XE）実装

**結論**: `ip http secure-server` + `restconf`を投入した装置に対して、
実際にHTTP経由でRESTCONF風のJSON API(`ietf-interfaces`モデルのGET/PUT)
を叩けるようになった。HTTP Basic認証（実機のRESTCONFと同じ方式）で
保護されており、実際にuvicornを起動して認証込みで動作確認済み。

対象読者はClaude以外のLLM（Qwen等）でも良い。手順は全て
`network-lab-emulator`のHTTP APIに対するcurlコマンドで再現できる。

## 前提: NETCONF/RESTCONFはCatalyst 3650でも使えるか

機種・IOS-XEバージョン・ライセンスによります(実機での確認が必要)。
このエミュレータでは`device_type`が`cisco`/`catalyst`であれば
機種を問わず使える実装にしている。

## このエミュレータでの構成上の違い（重要）

実機のRESTCONFは対象装置のIPアドレス自体でルーティングされる
（`https://<装置のIP>/restconf/data/...`）が、このエミュレータは
**1プロセスで複数装置を仮想的にホストしている**ため、URLに
`device_id`を含める形にしている:

```
実機:            https://<装置IP>/restconf/data/ietf-interfaces:interfaces
このエミュレータ: http://<エミュレータのIP>/restconf/<device_id>/data/ietf-interfaces:interfaces
```

## CLIでの有効化

```
configure terminal
ip http secure-server
restconf
interface GigabitEthernet0/1
ip address 10.5.0.1 255.255.255.0
no shutdown
end
```

確認コマンド:
```
show restconf
show running-config
```

`show restconf`は`RESTCONF: Enabled`、`show running-config`には
`ip http secure-server` / `restconf`が投入した通りに反映される。

## RESTCONF APIの認証

このエミュレータの他のAPI（`/api/*`）はセッショントークン
（`X-Session-Token`ヘッダー）方式だが、RESTCONF部分は**実機と同じ
HTTP Basic認証**にしている（RESTCONFはリクエスト毎にユーザー名/
パスワードを渡す方式が一般的なため）。ユーザー名/パスワードは
`/api/login`と同じ資格情報（既定`admin`/`admin`、
`NETLAB_AUTH_USER`/`NETLAB_AUTH_PASS`で変更可）。

## 実際に確認した動作

サーバーを起動（今回は認証を無効化せず、実際のBasic認証込みで検証）:
```bash
python3 -m uvicorn app:app --host 127.0.0.1 --port 8326
```

ログインしてセッショントークンを取得し、装置を作成・RESTCONFを有効化
（`/api/*`はセッショントークン方式なのでこちらは通常のログインを使う）:
```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8326/api/login \
  -d '{"username":"admin","password":"admin"}' -H 'Content-Type: application/json' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -s -X POST http://127.0.0.1:8326/api/device \
  -d '{"id":"rc-demo","type":"cisco","hostname":"RC-Demo"}' \
  -H "X-Session-Token: $TOKEN" -H 'Content-Type: application/json'
```

CLIでRESTCONFを有効化（`/api/cli`もセッショントークン方式）:
```bash
curl -s -X POST http://127.0.0.1:8326/api/cli \
  -d '{"device_id":"rc-demo","command":"configure terminal"}' \
  -H "X-Session-Token: $TOKEN" -H 'Content-Type: application/json'
# ... 以下 ip http secure-server / restconf / interface設定 / end を同様に投入
```

### 認証無しでRESTCONFを叩いた場合

```bash
curl -i http://127.0.0.1:8326/restconf/rc-demo/data/ietf-interfaces:interfaces
```

```
HTTP/1.1 401 Unauthorized
www-authenticate: Basic realm="RESTCONF"
```

### 誤った資格情報

```bash
curl -o /dev/null -w "%{http_code}\n" -u admin:wrongpass \
  http://127.0.0.1:8326/restconf/rc-demo/data/ietf-interfaces:interfaces
```
→ `401`

### 正しい資格情報でGET

```bash
curl -u admin:admin http://127.0.0.1:8326/restconf/rc-demo/data/ietf-interfaces:interfaces
```

```json
{
    "ietf-interfaces:interfaces": {
        "interface": [
            {
                "name": "GigabitEthernet0/0/0",
                "type": "iana-if-type:ethernetCsmacd",
                "enabled": true,
                "ietf-ip:ipv4": {
                    "address": [{"ip": "203.0.113.2", "netmask": "255.255.255.252"}]
                }
            },
            {
                "name": "GigabitEthernet0/1",
                "type": "iana-if-type:ethernetCsmacd",
                "enabled": true,
                "ietf-ip:ipv4": {
                    "address": [{"ip": "10.5.0.1", "netmask": "255.255.255.0"}]
                }
            }
        ]
    }
}
```

デフォルトで持っている装置テンプレートのインタフェース
（`GigabitEthernet0/0/0`等）と、CLIで追加設定した
`GigabitEthernet0/1`の両方が、`ietf-interfaces`のYANGモデルに
沿ったJSONとして正しく返っていることを確認した。

### PUTでインタフェースをdisableにする（実機のshutdown相当）

```bash
curl -u admin:admin -X PUT \
  http://127.0.0.1:8326/restconf/rc-demo/data/ietf-interfaces:interfaces/interface=GigabitEthernet0/1 \
  -H 'Content-Type: application/yang-data+json' \
  -d '{"ietf-interfaces:interface":{"enabled":false}}'
```

応答:
```json
{"ietf-interfaces:interface": {"name": "GigabitEthernet0/1", "type": "iana-if-type:ethernetCsmacd", "enabled": false, "ietf-ip:ipv4": {"address": [{"ip": "10.5.0.1", "netmask": "255.255.255.0"}]}}}
```

CLI側（`show ip interface brief`）にも反映されることを確認:
```
GigabitEthernet0/1     10.5.0.1        YES NVRAM   administratively down down
```

RESTCONF経由の書き換えがCLIの状態と完全に同期していることが確認できた
（内部的には同じ`state.interfaces`辞書を読み書きしているため）。

## 対応しているエンドポイント

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/restconf/{device_id}/data/ietf-interfaces:interfaces` | 全インタフェース一覧 |
| GET | `/restconf/{device_id}/data/ietf-interfaces:interfaces/interface={ifname}` | 単一インタフェース |
| PUT | `/restconf/{device_id}/data/ietf-interfaces:interfaces/interface={ifname}` | `enabled`のみ書き換え可（shutdown/no shutdown相当） |

RESTCONFが未有効化の装置、または`device_type`が`cisco`/`catalyst`
以外の装置に対しては、実機のRESTCONFエラー形式
（`ietf-restconf:errors`）に沿った404を返す。

## 未実装（今後の課題）

- `ietf-interfaces`以外のYANGモデル（`Cisco-IOS-XE-native`等、
  実機で使われることが多いネイティブモデル）
- NETCONF（`netconf-yang`のCLI受理と`show netconf-yang`の表示までは
  実装したが、実際にポート830でNETCONFセッションを張る部分は未実装）
- YANG Patch / RPC操作（`show`コマンド相当のRPC呼び出し等）
- RESTCONFの`Accept`/`Content-Type`ヘッダー（`application/yang-data+json`）
  の厳密なネゴシエーション（現状は常にJSONを返すのみ）

## 関連ドキュメント

- `docs/evpn-vxlan-nexus.md` — 同時期に実装したNexus Dashboard風ビュー
  （こちらは既存のセッショントークン認証をそのまま使用）
- `docs/aws-directconnect-bgp-design.md` — TestClientの制約について
