# WORLD(地球儀)— クラウド宛ての流量・Cloudflare Radar・IIJ IHR・MAWI

`/cyber-view` の **WORLD** ボタン(`?view=world`)。地球儀の上に次の4つを重ねる。

| 何を | どこから | 地球儀での見え方 |
|---|---|---|
| ラボ/自社から **AWS・Google Cloud** の各リージョンへの流量 | 公開されている IP 帯の一覧(AWS `ip-ranges.json`、GCP `cloud.json`)と、装置の ping・NetFlow/sFlow の宛先 IP を最長一致で照合 | 拠点(SITE)からリージョンへの弧(AWS 橙 / GCP 青)とプレート(直近 bps・累計) |
| 世界の **HTTP 流量の多い国**・**L7 攻撃の発信国→標的国**・**通信の異常** | Cloudflare Radar API(無料の API トークンが必要) | 国の位置に立つ柱(上位5つはプレート)、攻撃は赤い弧 |
| 日本の主要 AS の **遅延の異常**・**障害(到達不能)** | IIJ Internet Health Report(IHR)API(トークン不要) | 「注」の印、右のパネル |
| **日米回線の本物のトラフィック** | MAWI(WIDE プロジェクト samplepoint-F)の pcap を再生 | 太平洋の紫の帯。フローは NETWORK ビューに紫の「MAWI」で流れる |

![WORLD](images/cyber-view-world.jpg)

MAWI を再生中の NETWORK ビュー(紫が MAWI のフロー):

![MAWI](images/cyber-view-world-net-mawi.jpg)

## 正直な限界

- **地球儀は「どこへ流れているか」だけで、Google や AWS の中身の流量は誰にも取れない。** 取れるのは「自社から
  各クラウドのどのリージョンへ何 bps 流れたか」(宛先 IP が公開 IP 帯に入るかで判定)。
- **Radar / IHR の値は割合・件数**。回線の bps ではない(Cloudflare が見た世界の HTTP の割合など)。
- **MAWI のアドレスは匿名化**(上位ビットの構造を保つ方式)されているので、国・クラウドの判定には使わない。
  プロトコル・ポート・量・時間の形は本物。地球儀では日米間の帯としてだけ表示する。
- 見本モード(`NETLAB_WORLD_SAMPLE=1`)は、取得に失敗した元だけを本物の応答と同じ形の作り物に差し替え、画面に
  「見本 SAMPLE」と出す。**見本からは警報を作らない。**

## 仕組み

- `engine/world_sources.py`
  - `CloudRanges`: AWS / GCP の IP 帯を読み込み、最長一致。同じ帯なら `AMAZON` より具体的なサービス(EC2/S3…)を優先。
    取得に失敗したら `data/world/` のキャッシュを使う。リージョン → 場所(`AWS_REGIONS` / `GCP_REGIONS`)。
  - `RadarClient`: `/radar/http/top/locations`・`/radar/attacks/layer7/top/attacks`・`/radar/traffic_anomalies`
    (形は github.com/cloudflare/api-schemas の openapi.json で確認)。`CLOUDFLARE_API_TOKEN` が無ければ `no_token`。
  - `IhrClient`: `/network_delay/alarms/`(`timebin__gte` 必須、`startpoint_name` は `|` 区切りで日本の主要 AS)と
    `/disco/events/`(形は ihr-django の serializers で確認)。同じ事象は1つにまとめる。
  - `WorldState`: 10分ごとに更新、宛先 IP ごとの流量をリージョンへ積算(`observe_flow`)。MAWI のフローは数えない。
- `engine/mawi.py`: pcap / pcap.gz(URL ならダウンロードしながら展開)を読み、1秒ごとにフローへまとめて、上位だけを
  指定の速さで NetFlow/sFlow と同じ入口(`_on_flow`、`via: 'mawi'`)へ流す。Ethernet(VLAN タグ可)/ Raw IP /
  Linux SLL。pcapng は未対応。
- `engine/traffic_analytics.py`: 警報 `ihr_outage` / `ihr_delay`(偏差 10 以上)/ `radar_anomaly`(日本のみ)、花びら WORLD。
- 地図: `static/world/world.json`(Natural Earth 1:110m のパブリックドメインの国境・海岸線を world-atlas から、
  国名は i18n-iso-countries から。`tools/build_world_data.py` で作る)。

## API と設定

| | |
|---|---|
| `GET /api/world` | クラウドのリージョン別流量・Radar・IHR・拠点の位置・MAWI の状態 |
| `POST /api/world/refresh` | 今すぐ取り直す |
| `POST /api/mawi/replay` | `{"stamp":"202411151400"}` か `{"source":"<URL かローカルの pcap(.gz)>"}`、`limit`(既定 200000 パケット)・`speed`(1=実時間)・`topk`(1秒あたりのフロー数)・`loop` |
| `POST /api/mawi/stop` / `GET /api/mawi/status` | 停止 / 状態(プロトコル別の数・よく使われたポート) |

| 環境変数 | |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Radar のトークン(Cloudflare のダッシュボードで「Radar: Read」の無料トークン) |
| `NETLAB_WORLD_FETCH=0` | 外部へ取りに行かない(テストは既定でこれ) |
| `NETLAB_WORLD_SAMPLE=1` | 取れなかった元を見本で埋める(画面に SAMPLE) |
| `NIRVANA_SITE_LATLON` | 拠点の緯度,経度(既定 `35.68,139.69` 東京) |

## ライブ検証(この作業環境で)

- **AWS**: 本物の `ip-ranges.json` を取得(10758 帯)。ラボの Cisco から `ping 13.112.1.1 repeat 200 size 1400` 等を
  打つと、東京(ap-northeast-1 EC2)・バージニア北部・アイルランド・オレゴンへの弧と bps が地球儀に出た(上の画像)。
- **Google Cloud / Radar / IHR / MAWI**: この作業環境の通信ポリシーで `www.gstatic.com`・`api.cloudflare.com`・
  `ihr.iijlab.net`・`mawi.wide.ad.jp` がブロック(403)。画面にはそれぞれ「取得できません(理由)」と出ることを確認し、
  応答の形は各プロジェクトの公開スキーマで確認した。見本モードで表示を確認。
- **MAWI の再生**: 本物の MAWI と同じ形式の pcap(6000 パケット、Ethernet、gzip)を手元で作って再生し、NETWORK ビューに
  紫の MAWI フローが流れ、地球儀の帯に「6/60s」・プロトコル別の数が出ることを確認(上の画像)。
- **未確認**: 本物の Radar・IHR・MAWI のデータでの表示。これらのホストを許可した環境(作成済みの
  `network-lab-emulator` 環境)の新しいセッションで確認する。Radar はさらにトークンが必要。

テスト: `tests/test_world_view.py`(9件。外部へは出ない)。

## 出典・ライセンス

- Cloudflare Radar(CC BY-NC 4.0。表示に出典を出す)、IIJ Internet Health Report、MAWI Working Group Traffic Archive
  (WIDE プロジェクト)、AWS / Google Cloud の公開 IP 帯一覧、Natural Earth(パブリックドメイン)・world-atlas(ISC)・
  i18n-iso-countries(MIT)。
