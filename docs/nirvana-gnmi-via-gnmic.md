# ニルヴァーナで gNMI の値を見る(gnmic → Prometheus 経由)

```
Catalyst ──gNMI(50052)──▶ gnmic ──:9804/metrics──▶ Prometheus(ヴァルハラ)──▶ ニルヴァーナ
```

ニルヴァーナ単体版(`cyber-view-standalone`)は gNMI を直接は話しません。
gnmic が gNMI で購読した値を Prometheus の形で公開し、ニルヴァーナはそれを Prometheus から読みます。

- 流量: `interfaces_interface_state_counters_in/out_octets` の rate
- リンクの断(POLL): `interfaces_interface_state_oper_status` の `oper_status` ラベル(UP / DOWN / LOWER_LAYER_DOWN)

snmp_exporter と並べて使えます(`flavor: both` では同じ装置・IF は SNMP を優先し、無いところを gNMI で埋めます)。

![gNMI の値で描いた ROUTER LINKS](images/nirvana-gnmi-links.jpg)

## 1. 装置(Catalyst)の設定

IOS XE 16.x(例: WS-C3650 16.12.11)の場合です。17.x では `gnmi-yang` の代わりに `gnxi` / `gnxi server` を使います。

```
aaa new-model
aaa authentication login default local
aaa authorization exec default local
username admin privilege 15 secret <パスワード>
netconf-yang           ! 16.x では gNMI の土台(DMI)を起こすために必要
gnmi-yang
gnmi-yang server       ! 暗号化なし・ポート 50052
```

`show platform software yang-management process` で `gnmib : Running` になるまで 1〜3 分待ちます。
`show running-config | include gnmi` に `gnmi-yang` と `gnmi-yang server` の 2 行が出ることを確かめます。

## 2. gnmic(Linux / WSL)

gnmic は Linux と Mac 向けだけです。Windows では WSL の Ubuntu で動かします。

```bash
bash -c "$(curl -sL https://get-gnmic.openconfig.net)"
```

`cyber-view-standalone/gnmic.example.yaml` を `gnmic.yaml` にコピーし、`targets` に装置の IP を書きます。

```bash
export GNMI_USER=admin GNMI_PASSWORD='<パスワード>'
gnmic --config gnmic.yaml subscribe
```

```bash
curl -s http://127.0.0.1:9804/metrics | grep in_octets
```

`prometheus` 出力の `strings-as-labels: true` と `export-timestamps: true` は外さないでください。

- `strings-as-labels: true`: oper-status(文字列)をラベルにします。これが無いと断を判定できません。
- `export-timestamps: true`: 状態が UP → DOWN に変わると、古い UP の系列も gnmic の `expiration` まで残ります。ニルヴァーナは系列ごとの最新時刻で新しい方を選びます。

## 3. Prometheus

```yaml
scrape_configs:
  - job_name: gnmic
    static_configs: [{targets: ['<gnmic を動かす機械>:9804']}]
```

## 4. ニルヴァーナ

```yaml
devices:
  - id: sw1
    hostname: Switch
    type: catalyst
    mgmt_ip: 192.168.1.1          # gnmic の targets と同じ IP
    interfaces:
      - {name: GigabitEthernet1/0/1, ip: 192.168.1.1, prefix: 24}
sources:
  prometheus:
    url: http://127.0.0.1:9090
    flavor: gnmic                 # SNMP も使うなら both
    gnmic_job: gnmic
```

`install.sh` は Prometheus に gnmic の系列があれば見つけて、`flavor` と `gnmic_job` を書きます。
gnmic は IF の IP を出さないので、リンク(`links`)は手で書きます。

gnmic には装置ごとの `up` が無いので、gnmic だけのときは「応答なし」の判定はしません。

## ライブ確認(2026-11-20)

ラボの Catalyst 2 台(`gnxi server`、10.77.0.1 / 10.77.0.2、Gi1/0/1 同士をリンク)に対して、本物の gnmic と Prometheus 2.54.1 を使い、
`python3 server.py -c <設定> --port 8091` のニルヴァーナで次のとおり確かめました。

| 操作 | gnmic の出力 | ニルヴァーナ(`/api/traffic/links`) |
|---|---|---|
| 起動直後 | `oper_status="UP"` | `POLL: up`・0 bps |
| `ping 10.77.0.2 repeat 2000 size 1500` を流す | `in_octets` が増える | 約 8.1 Mbps ずつ(上の画面写真は約 15.3 Mbps)、`source: prometheus`(ヴァルハラ) |
| GN2 で `shutdown` | `oper_status="DOWN"`(古い UP の系列も残る) | `down: true`・`POLL: down` |
| `no shutdown` | `oper_status="UP"`(古い DOWN の系列も残る) | `down: false`・`POLL: up` |

最初は oper_status の問い合わせで `timestamp(... == 1)` と書いていました。比較の結果には問い合わせ時刻が付くため、新旧の系列を区別できませんでした。
`timestamp(M) and (M == 1)` に直して、断と復旧の両方が正しく切り替わることを確かめています。

実機の WS-C3650-24TD(IOS XE 16.12.11)では、ユーザーが gnmic(WSL)で同じパスの購読ができることを確かめています。
Windows の pygnmi は get は通りましたが、subscribe は `Unsupported subscription list qos selection` で断られました
(pygnmi が付ける QoS の欄を 16.12 が受け付けない)。gnmic は QoS を付けないので通ります。

試験は `tests/test_nirvana_gnmic_source.py` です。
