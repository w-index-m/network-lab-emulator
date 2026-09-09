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

## RESTCONFヘルスダッシュボード（追記）

複数のCisco/Catalyst装置を横断的にヘルスチェックできるダッシュボードを
追加した。Nexus Dashboard風ビューと同じ構成（集計API + 静的HTML）。

- **画面**: `/static/restconf_dashboard.html`
- **データAPI**: `GET /api/restconf/dashboard`（こちらは`/restconf/*`と
  違い、他の`/api/*`と同じセッショントークン認証。RESTCONF本体への
  実際のアクセスはBasic認証のまま）

返すJSONの例:

```jsonc
{
  "summary": {"device_count": 2, "restconf_ready_count": 2,
              "total_interfaces": 58, "total_interfaces_up": 9},
  "devices": [
    {
      "device_id": "mock-cat3650", "hostname": "MOCK-CAT3650",
      "restconf_enabled": true, "http_secure_server": true,
      "interface_count": 29, "interface_up": 4, "interface_down": 25,
      "interface_with_ip": 1,
      "interfaces": [{"name": "GigabitEthernet1/0/1", "enabled": true, ...}]
    }
  ]
}
```

### 実際に確認した動作

`mock-cat3650`と`mock-cat9200`（どちらも`device_type: catalyst`）に
`ip http secure-server` / `restconf`を投入し、`mock-cat3650`の
`GigabitEthernet1/0/2`を`shutdown`した状態でダッシュボードを開いたところ:

- 両カードとも「RESTCONF READY」バッジが表示された
- `MOCK-CAT3650`側は「4 up / 25 down」、shutdownしたポートが
  インタフェース一覧で赤ドット表示に切り替わった
- `MOCK-CAT9200`側は変更していないため「5 up / 24 down」のまま

前述の通り、このエミュレータは機種（3650/9200）によるIOS-XEバージョン
差やRESTCONF対応可否の違いを再現していないため、`device_type:
catalyst`である限り両者は同じ挙動になる。実機での機種差検証は
実際の装置の`show version` / `show restconf`の結果を別途照合する
必要がある。

## johann (flopach/johann-network-device-monitoring) を参考にした追加機能

Cisco IOS-XE向けのOSS監視ツール
[flopach/johann-network-device-monitoring](https://github.com/flopach/johann-network-device-monitoring)
の設計を参考に、3つの機能を追加した。

### 1. CSV一括デバイス登録（`tools/bulk_device_import.py`）

johannの「CSVで複数デバイスを一括追加」機能を参考にした、
このエミュレータの`/api/device`・`/api/link`向けCSVインポートツール。

```bash
python tools/bulk_device_import.py samples/bulk_import_example_devices.csv \
  --links samples/bulk_import_example_links.csv \
  --base-url http://127.0.0.1:8000
```

装置CSV（列: `id,type,hostname`）とリンクCSV（列: `a,b,iface_a,iface_b`）
を分けて指定する。`--no-auth`で`NETLAB_AUTH_DISABLE=1`のサーバー向けに
ログインをスキップできる。実際に3台・リンク2本を一括投入して動作確認済み。

### 2. RESTCONFダッシュボードの時系列グラフ化

johannのレポート/グラフ機能（Matplotlib）を参考に、SNMPダッシュボード
（`snmp_dashboard.html`）で使っていたsparkline SVGの仕組みを移植。
サーバー側で`/api/restconf/dashboard`がポーリングされるたびに
device_idごとの`{t, up, down}`を直近60件分バッファし（`_restconf_history`、
`_metrics_history`と同じ`deque(maxlen=60)`方式）、各装置カードに
「Interfaces Up 推移」のスパークラインとして表示する。

### 3. ダッシュボードからのshutdown/no shutdown操作

johannの「RESTCONF設定ツール」を参考に、各インタフェース行に
`shutdown`/`no shutdown`ボタンを追加。クリックすると
`PUT /restconf/{device_id}/data/ietf-interfaces:interfaces/interface={ifname}`
を直接叩く。RESTCONF本体はHTTP Basic認証のままのため、ダッシュボード上部の
「設定する」ボタンでユーザー名/パスワードを入力し、**ブラウザの
sessionStorageにのみ**保存する設計にした（サーバーには送らない・
保存しない）。

### 実際に確認した動作

Playwrightで実際にブラウザ操作を再現し、資格情報設定→
`GigabitEthernet1/0/1`の`no shutdown`ボタンをクリック→
ボタン表示が`shutdown`に切り替わり、ドットが緑になることを
スクリーンショットで確認した。裏側では実際にPUTリクエストが
飛び、`state.interfaces`が書き換わっている。

## トラフィック量・CPU使用率・ICMPカウンタの追加（さらに追記）

「トラフィック量やCPU使用率もダッシュボードで見たい」「ICMP関連の
流量も見たい」という要望を受けて、`/api/restconf/dashboard`と
`restconf_dashboard.html`に3種類のメトリクスを追加した。

### CPU使用率・トラフィック量

新規のデータ収集エンジンは作らず、既存の`/api/snmp/dashboard`と
**同じデータソース**（`snmp_agent._build_mib(device_id)`が持つ
MIB-II/CISCO-PROCESS-MIB相当の値）を流用している。ダッシュボードの
各装置カードに、SNMPダッシュボードと同じsparkline表示でCPU%と
累計トラフィックバイト数の推移を追加した。

### ICMPカウンタ（新規実装）

これまで`show ip traffic`自体が未実装だった
（過去の会話で「Catalyst 9300でICMP Redirectが発生する数をチェック
する方法」を扱った際は実機の一般論としてサンプル出力を示しただけで、
このエミュレータには入っていなかった）。今回、
`engine.protocols.IcmpEngine`に`icmp_stats`（装置ごとのecho/echo reply/
unreachable/redirectの送受信カウンタ）を追加し、`ping()`が呼ばれる
たびに実際に加算するようにした:

- 到達可能なping → 送信元の`echo_sent`/`echo_reply_rcvd`、宛先の
  `echo_rcvd`/`echo_reply_sent`をそれぞれ加算
- 到達不能なping → 送信元の`echo_sent`/`unreachable_rcvd`を加算
  （実機のping timeoutでICMP Unreachableが返る状況に相当）

`show ip traffic`（Cisco/Catalyst、新規実装）のICMPセクションに
このカウンタをそのまま表示し、`clear ip traffic`でリセットできる。

### ICMP Redirectの検出（新規実装）

「pingの数ではなく、ICMP Redirectが多発しているかを知りたい」という
要望を受けて、実際にRedirect発生条件を判定するロジックを追加した
（`IcmpEngine._maybe_generate_redirect`）。

実機でRedirectが発生する典型条件は、「ホストのデフォルトゲートウェイ
(R1)が、宛先への最適経路として、ホストと同一サブネット上の別ルータ
(R2)を持っている」場合。この時R1はホストから受け取ったパケットを
R2へ転送しつつ、ホストに「次からR2へ直接送るように」というICMP
Redirectを送り返す。今回はパケット単位のインタフェース追跡までは
行わず、**ping一回ごとに「最初のホップの次ホップが送信元と同一
サブネット上にあるか」を判定**し、条件成立時に該当ホップの
`redirect_sent`と送信元の`redirect_rcvd`を加算する形にした。

検証構成（`tests/test_icmp_traffic_counters.py::TestIcmpRedirectDetection`）:

```
[host] --- [switch] --- [R1] (defaultゲートウェイ)
               |
             [R2] --- [host2]
```

hostのデフォルトゲートウェイはR1だが、R1はhost2向けネットワークを
「hostと同一サブネット上のR2」経由と学習している（非対称な経路）。
この状態でhostからhost2へpingすると:

- R1側の`show ip traffic`: `Sent: 5 redirects`
- host側の`show ip traffic`: `Rcvd: ... 5 redirects`

が実際に加算されることを確認した。逆に、ゲートウェイ自身が最終的な
宛先であるなど「又貸し」が発生しない構成では、Redirectは0のまま
であることも確認済み（`test_no_redirect_when_gateway_is_directly_on_path`）。

### 実際に確認した動作

`mock-cat3650` → `mock-cat9200`へ複数回ping、および到達不能な
アドレスへのpingを実行した状態でダッシュボードを開いたところ、
両カードにCPU%・トラフィック量(KB単位)・ICMPパケット累計数と、
echo/echo reply/unreachable/redirectそれぞれの送受信内訳が
正しく表示されることを確認した（スクリーンショット参照）。上記の
非対称経路構成を実際にサーバー上で再現し、`show ip traffic`と
`/api/restconf/dashboard`の両方でRedirectカウンタが増えることも
別途確認済み。

## 関連ドキュメント

- `docs/evpn-vxlan-nexus.md` — 同時期に実装したNexus Dashboard風ビュー
  （こちらは既存のセッショントークン認証をそのまま使用）
- `docs/aws-directconnect-bgp-design.md` — TestClientの制約について
