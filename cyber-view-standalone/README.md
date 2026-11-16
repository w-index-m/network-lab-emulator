# Cyber View スタンドアロン版

network-lab-emulator の `/cyber-view`(NIRVANA 改風の 3D 監視画面)を、**実機の監視環境**で使うための単体版。
エミュレータには依存せず、既にある監視基盤からデータを読む。

| データ元 | 読むもの | 画面での使われ方 |
|---|---|---|
| Prometheus + snmp_exporter | `rate(ifHCIn/OutOctets)`・`ifOperStatus`・`up` | リンクの流量と流れのアニメ、LINK DOWN の検知経路「POLL」、「NO RESPONSE 消失」 |
| Loki(syslog) | ログ行 | 装置の上に浮かぶログ、LINK DOWN の検知経路「SYSLOG」、error 以上の警報、フラップの警告(注) |
| Zabbix(API) | `Interface …: Bits received / Bits sent / Operational status`、未解決の問題、ホストの到達性 | 流量(Prometheus が無い場合)、「POLL」、Zabbix の問題を警報として表示 |
| SNMP trap(任意) | linkDown / linkUp | LINK DOWN の検知経路「TRAP」 |

装置とつながり(トポロジ)は設定ファイルに手書きする。

実機の Catalyst を登録する手順(装置側の SNMP/syslog 設定から確認方法まで)は、リポジトリの `docs/real-catalyst-registration.md`。

画面では Prometheus を **ヴァルハラ(VALHALLA)** と表示する(呼び名だけ。設定項目名 `sources.prometheus` などは Prometheus のまま)。

## 必要なもの

- Python 3.9 以上(`pip install -r requirements.txt` … FastAPI / uvicorn / PyYAML のみ)、または docker
- 監視基盤への HTTP 到達性(Prometheus 9090 / Loki 3100 / Zabbix フロントエンド)

## 使い方

```bash
cd cyber-view-standalone
cp config.example.yaml config.yaml     # 装置・つながり・データ元を自分の環境に書き換える
pip install -r requirements.txt
ZABBIX_TOKEN=xxxx python3 server.py -c config.yaml --port 8090
# → ブラウザで http://<このホスト>:8090/
```

docker で動かす場合:

```bash
cp config.example.yaml config.yaml
ZABBIX_TOKEN=xxxx docker compose up -d --build
```

社内プロキシで独自の CA を使っている環境では、その CA 証明書(`.crt`)を `extra-ca/` に置いてからビルドする。

## 設定ファイル

`config.example.yaml` にすべての項目の説明がある。要点:

- **devices**: `id`(画面内の名前)、`hostname`(表示名。syslog 本文・Zabbix のホスト名との照合にも使う)、
  `type`(`cisco` / `catalyst` / `nexus` / `sir` / `srs` など。フラップ検知でログ形式を判定する)、
  `mgmt_ip`(Prometheus の instance / syslog の送信元)、`interfaces`(IP アドレス空間の画面に置く IF。
  `name` は ifName と同じ表記。`GigabitEthernet0/0/0` と `Gi0/0/0` は同じものとして扱う)
- **links**: `a` / `a_if` / `b` / `b_if`、`speed`(bps。使用率の分母)
- **sources**: 使うものだけ書く。トークンは `${ZABBIX_TOKEN}` のように環境変数で渡す
- **auth.token**(任意): 設定すると `http://host:8090/?token=...` で開く必要がある

### どのデータ元で何が見えるか

| | Prometheus だけ | Zabbix だけ | Loki を追加 |
|---|---|---|---|
| 流量・流れのアニメ | ○ | ○ | — |
| LINK DOWN | POLL(ifOperStatus) | POLL(Operational status) | + SYSLOG |
| 応答なし(消失) | ○(up=0) | ○(ホストの SNMP 到達性) | — |
| 警報・警告 | link_down | link_down + Zabbix の問題 | + error 以上のログ、フラップ |

### Zabbix で気をつけること

- テンプレートは「Network interfaces by SNMP」系(`Cisco IOS by SNMP` など)のアイテム名を前提にしている。
- これらのテンプレートは、管理者が `shutdown` した IF を自動検出(LLD)から外し、アイテムを
  「検出ルールにより無効化」する。この画面はそれを「その IF は落とされている(down)」として扱う。
  **復旧後も Zabbix 側は次の自動検出(テンプレート既定 1 時間)まで無効のまま**なので、早く戻したい場合は
  Zabbix で検出ルールを「今すぐ実行」するか、検出間隔を短くする。
- 流量は Zabbix の取得間隔(Cisco テンプレートは 1 分ごとの SNMP walk)で更新される。

### ヴァルハラ(Prometheus)からの再生・予測・OSPF・警報

`sources.prometheus` があれば次も使える(詳細は `docs/valhalla-nirvana.md`):

- ROUTER LINKS の「⏪ 再生」: `rate(ifHC*Octets)` と `ifOperStatus` の履歴から過去の各時刻を 3D で再生
- 予測: IF の使用率(`rate / ifHighSpeed`)から「このままだと何日で 80%」と、平常時とのずれ
- OSPF ボタン: snmp_exporter で OSPF-MIB(`ospfRouterId` / `ospfNbrState`)を取っていれば、設定ファイルのリンクごとの
  隣接状態を出す(最短経路ツリーと Dead タイマーは SNMP では取れないので出さない)。snmp_exporter の設定例は
  リポジトリの `iac/cisco-traffic-monitoring/snmp_exporter/snmp.yml`
- ヴァルハラで発報中の警報を、警報の花に「VALHALLA」の花びらで出す(新しく出たものだけ)

### BOARD / EXPLORE / ALERTS(Grafana が持つ機能)

ダッシュボード(8種のパネル・変数・時間範囲・自動更新・注釈・編集)、問い合わせ(PromQL / LogQL / 内部ログ)、
アラートルールと webhook 通知が使える。データソースは `sources.prometheus` / `sources.loki` と、collector が組み立てた
リンク状態・状態ボードを貯める「NIRVANA 内部」。Grafana のダッシュボード JSON もそのまま取り込める。
保存先は `nirvana_board.json`(`board.path` または `NIRVANA_BOARD_PATH` で変更)。詳細はリポジトリの `docs/nirvana-board.md`。

## ラボ版との違い

- ラボ専用のもの(ping の経路の粒、ポートスキャン検知、経路の変化「情」、DEMO)は無い。DEMO ボタンは自動で隠れる。
- 検知経路の「STATE」(エミュレータ内部の IF 状態)は無い。
- 画面(`web/cyber_view.html`)と検知ルール(`cvs/flap_rules.py`・`cvs/link_evidence.py`)はラボ版と同じファイル。
  ラボ側を直したら `cp static/cyber_view.html cyber-view-standalone/web/` 等で揃える
  (`tests/test_cyber_view_standalone.py` がずれを検出する)。

動作確認の記録(本物の Prometheus・snmp_exporter・Loki・Zabbix 7.0 で試した結果と画面): `../docs/cyber-view-standalone.md`
