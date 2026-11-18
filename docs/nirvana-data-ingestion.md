# ニルヴァーナのデータの取り込み方法と実装

[ニルヴァーナの紹介](nirvana-introduction.md) の「取り込めるデータ」の表を、データ元ごとに具体的に書きます。各データ元について、次の6点を書きます。

- 何を、どの手段で取るか
- 設定に何を書くか
- どの周期で取るか
- どのファイルのどの関数が受け持つか
- 装置とどう対応づけるか
- 前提と、できないこと

## まず決めること: ニルヴァーナはどこで動き、どう取りに行くか

| 取り方 | 対象 | ニルヴァーナから見た通信 |
|---|---|---|
| HTTP で問い合わせる(pull) | Prometheus(ヴァルハラ)、Loki、Zabbix | ニルヴァーナ → 各サーバーの HTTP API |
| SNMP で問い合わせる(pull) | 装置の経路表・CPU | ニルヴァーナ → 装置の UDP 161 |
| 受け口で待つ(push を受ける) | SNMP trap、NetFlow / sFlow | 装置 → ニルヴァーナの UDP 1162 / 2055 / 6343 |
| ローカルのファイルを読む | MRTG、Cacti | ニルヴァーナが動くマシンのファイルシステム(HTTP ではない) |

**MRTG と Cacti は HTTP では取りに行きません。** ニルヴァーナが動いているマシンのファイルを、設定に書いたパスから直接読みます。MRTG・Cacti が別のサーバーにある場合は、次のどちらかにします。

- そのサーバーでニルヴァーナを動かす
- NFS・sshfs・docker のボリュームで、WorkDir や rra ディレクトリを共有する

HTTP のディレクトリ一覧から選んで取りに行く方式はありません。

設定ファイルは2種類あります。

- **単体版**: `cyber-view-standalone/config.yaml` の `sources:` に書きます。書き方の例は `config.example.yaml` です。`install.sh` が既存の監視を探して下書きを作ります。
- **ラボ版**: 環境変数で指定します。ラボ版は、SNMP の直接取得・MRTG・Cacti・Zabbix の代わりにエミュレータの内部の値を使います。

取り込みの順番は単体版の `cvs/collector.py` の `Collector.tick()` が決めます。周期は `poll_interval` 秒で、既定は10秒です。

---

## 1. Prometheus(ヴァルハラ)+ snmp_exporter

| 項目 | 内容 |
|---|---|
| 取るもの | IF の流量・IF の状態・装置が収集できているか |
| 取り方 | HTTP `GET <url>/api/v1/query?query=<PromQL>`(instant query) |
| 単体版の設定 | `sources.prometheus: {url: http://<ヴァルハラ>:9090, job: <snmp_exporter の scrape job 名>}` |
| ラボ版の設定 | 環境変数 `PROMETHEUS_URL`(job は `cisco-snmp`) |
| 周期 | `poll_interval`(既定10秒)。値そのものはヴァルハラの scrape 周期(IaC では15秒)で更新 |
| 実装 | `cvs/sources.py` の `Prometheus`(`rates()` / `oper_status()` / `targets_up()` / `counters()`)。ラボ版は `engine/link_stats.py` と `engine/link_evidence.py` |

実際に投げる PromQL:

```
rate(ifHCInOctets{job="<job>"}[60s]) * 8      # 受信 bps(窓は画面の「更新」の秒数、最小60秒)
rate(ifHCOutOctets{job="<job>"}[60s]) * 8     # 送信 bps
ifOperStatus{job="<job>"}                     # 1=up、2=down・7=lowerLayerDown=断(POLL の検知経路)
up{job="<job>"}                               # 0 なら「NO RESPONSE」+「警 SNMP」
ifHCInOctets{job="<job>"} / ifHCOutOctets     # 累積カウンタ(IF ごとのタイル)
```

- **装置との対応**: 結果の `instance` ラベル(ポートは外す)が設定の `devices[].mgmt_ip` と一致するものを、その装置とみなします。
- **IF との対応**: `ifName` ラベルを使います。そのため snmp_exporter の `if_mib` モジュールは `ifName` を lookup する設定にしてください(IaC の `snmp.yml` はそうなっています)。名前は `Gi0/0/0` と `GigabitEthernet0/0/0` のような略記の違いを吸収して照合します。
- **使う画面**: ROUTER LINKS の流量と使用率、リンク断の検知経路 POLL、NO RESPONSE。履歴の再生・予測・OSPF ビュー(`cvs/valhalla_views.py`)もヴァルハラに問い合わせます。

## 2. Grafana Loki(syslog)

| 項目 | 内容 |
|---|---|
| 取るもの | 装置の syslog の行 |
| 取り方 | HTTP `GET <url>/loki/api/v1/query_range?query=<LogQL>&start=&end=&limit=1000&direction=backward` |
| 単体版の設定 | `sources.loki: {url: http://<loki>:3100, query: '{job=~".+"}', device_labels: [device_id, host, hostname, instance]}` |
| ラボ版の設定 | 環境変数 `LOKI_URL` |
| 周期 | `poll_interval` ごとに、前回読んだ時刻の5秒前から今までを読む(重なった分は重複として除く) |
| 実装 | `cvs/sources.py` の `Loki.logs()`、`cvs/collector.py` の `Collector.poll_logs()`、判定は `cvs/analytics.py` の `record_log()` |

- **装置との対応**: ログのラベルを `device_labels` の順に見て、設定の装置ID・hostname・mgmt_ip・`loki_host` のどれかと一致するものを探します。ラベルで決まらなければ、本文に装置の hostname が含まれるかで決めます。
- **装置から Loki まで**: IaC では装置 → syslog-bridge(UDP 5515、`tools/syslog_to_loki.py`)→ Loki と流します。ラベルは `job="netlab-syslog"` と送信元 IP です。
- **判定**: 次のように判定します。
  - `%LINK-3-UPDOWN` などのリンクの上下はリンク断の検知経路 SYSLOG(`parse_log_link_event`)
  - error 以上のログは「警」(`syslog_error`)
  - 同じ IF の上下の繰り返しは「注」(フラップ、`flap_rules.py`)

## 3. SNMP(ニルヴァーナが直接ポーリング)

| 項目 | 内容 |
|---|---|
| 取るもの | 経路表、CPU 使用率 |
| 取り方 | SNMP GET / GETNEXT(v2c、または v3 の USM)。標準ライブラリだけで書いたクライアント(`cvs/snmp_client.py`、v3 は `cvs/snmp_usm.py`) |
| 単体版の設定 | `sources.snmp: {community: ..., interval: 60, full_every: 5, cpu: {high: 80, jump: 20, hold: 2}}`。v3 は `sources.snmp.v3: {user, auth, auth_password, priv, priv_password}`。装置ごとの上書きは `devices[].snmp_community` / `snmp_v3` / `snmp_cpu_oid` |
| ラボ版 | SNMP は使わず、エミュレータの経路表(`rib_engine`)と CPU を直接見る(`app.py` の `_route_watch_tick`) |
| 周期 | `interval`(既定60秒)。経路の数が変わらなければ表全体は取らず、`full_every` 回に1回は取り直す |
| 実装 | `cvs/snmp_poll.py` の `SnmpPoller`。差分の判定は `cvs/route_watch.py`(`RouteWatcher` / `CpuWatch`) |

問い合わせ先は `devices[].mgmt_ip` の UDP 161 です。使う OID は次のとおりで、上から順に、取れたものを使います。

| 目的 | OID |
|---|---|
| 経路の数 | `1.3.6.1.2.1.4.24.6.0`(inetCidrRouteNumber)、`1.3.6.1.2.1.4.24.3.0`(ipCidrRouteNumber) |
| 経路表 | `1.3.6.1.2.1.4.24.7.1`(inetCidrRouteTable)→ `1.3.6.1.2.1.4.24.4.1`(ipCidrRouteTable)→ `1.3.6.1.2.1.4.21.1`(RFC1213 ipRouteTable) |
| CPU | `1.3.6.1.4.1.9.9.109.1.1.1.1.8`(Cisco cpmCPUTotal5minRev)→ `1.3.6.1.2.1.25.3.3.1.2`(hrProcessorLoad)。機種固有なら `cpu.oid` で指定 |
| IF 名 | `1.3.6.1.2.1.31.1.1.1.1`(ifName。経路の出口 IF の表示用) |

- **判定**:
  - 経路の追加・削除・変更は「情」
  - CPU が前回より `jump` 以上上がると「情」
  - CPU が `high` 以上の状態が `hold` 回続くと「注」
- **前提**: 装置の SNMP の ACL で、ニルヴァーナの IP を許可しておく必要があります。

## 4. SNMP trap

| 項目 | 内容 |
|---|---|
| 取るもの | linkDown(`1.3.6.1.6.3.1.1.5.3`)/ linkUp(`1.3.6.1.6.3.1.1.5.4`) |
| 取り方 | UDP の受け口で待つ |
| 単体版の設定 | `sources.snmp_trap: {listen: 0.0.0.0:1162}` |
| ラボ版の設定 | 環境変数 `SNMP_TRAP_LISTEN=0.0.0.0:1162` |
| 装置の設定(IOS) | `snmp-server host <ニルヴァーナ> traps version 2c <community> udp-port 1162`、`snmp-server trap-source <IF>`、`snmp-server enable traps snmp linkdown linkup` |
| 実装 | `cvs/link_evidence.py`(= `engine/link_evidence.py`)の `TrapStore.start()` が受け、`decode_trap()` で分解 |

- **分解の中身**: varbind の `snmpTrapOID`、`ifDescr`(`1.3.6.1.2.1.2.2.1.2.<ifIndex>`)または `ifName`、`sysDescr` を読みます。
- **装置と IF の対応**: 装置は送信元 IP か `sysDescr` の hostname で、IF は `ifDescr` で決めます。そのため `trap-source` で装置自身の IP から送ってください。
- **判定**: その IF の最新が down ならリンク断の検知経路 TRAP になり、「警」が出ます。up に戻れば「情」が出ます。
- **できないこと**: 受けられるのは SNMPv2c の trap(PDU 0xA7)だけです。v1 の trap、inform、v3 の trap は読みません。ポートを 162 にする場合は、root 権限で動かしてください。

## 5. NetFlow v5 / sFlow v5

| 項目 | 内容 |
|---|---|
| 取るもの | 送信元・宛先・プロトコル・ポート・パケット数・バイト数 |
| 取り方 | UDP の受け口で待つ |
| 単体版の設定 | `sources.flow: {netflow: 2055, sflow: 6343, bind: 0.0.0.0}`、ダークネットは `sources.darknet: {prefixes: [10.250.0.0/24]}` |
| ラボ版の設定 | 環境変数 `NETLAB_FLOW_COLLECT=2055,6343` |
| 装置の設定 | IOS `ip flow-export destination <ニルヴァーナ> 2055` + `ip flow-export version 5` + IF に `ip flow ingress`。NX-OS `feature sflow` + `sflow collector-ip <ニルヴァーナ> vrf default` + `sflow data-source interface <IF>` |
| 実装 | `cvs/flow_collector.py`(= `engine/flow_collector.py`)の `FlowCollector`、`parse_netflow_v5()` / `parse_sflow_v5()`。受けたフローは `cvs/security.py` の `on_flow()` へ |

- **sFlow の分解**: フローサンプルに入っているパケットの先頭(Ethernet + IPv4)を分解し、サンプリング率を掛けて量を見積もります。
- **使われ方**: `on_flow()` は1件ごとに次の4つへ渡します。
  - ダークネットの分類(`darknet.py`。スキャン・DDoS の跳ね返り・設定ミス・リフレクション、「警」)
  - 外部への大量送信の検知
  - WORLD のクラウド宛て流量と攻撃元の都市
  - 画面の「NetFlow / sFlow 送信元 → 宛先 ・ 量」のプレート
- **できないこと**: NetFlow v9 と IPFIX は読みません。

## 6. MRTG

| 項目 | 内容 |
|---|---|
| 取るもの | IF の入力・出力の bps(MRTG が5分ごとに書いた最新の区間の平均) |
| 取り方 | **ローカルのファイルを読む**(HTTP ではない) |
| 単体版の設定 | `sources.mrtg: {cfg: /etc/mrtg.cfg, stale: 900}`。書くのは `mrtg.cfg` のパスだけ |
| 実装 | `cvs/legacy_sources.py` の `parse_mrtg_cfg()`、`LegacyRates.map_mrtg()`、`read_mrtg_log()` / `rrd_last()` |

**ファイル名は `mrtg.cfg` から自動で決まります。** RRD のパスやファイル名を設定に書く必要はありません。

1. `mrtg.cfg` の `WorkDir:` で、ファイルのあるディレクトリが決まります。`LogFormat: rrdtool` があれば RRD、無ければ MRTG の `.log` を読みます。
2. `Target[<名前>]: <ifIndex または #IF名 または /IP>:<community>@<装置>` の各行から、次の名前のファイルを読みます。MRTG と同じく、ファイル名は小文字にします。
   ```
   <WorkDir>/<名前を小文字にしたもの>.log   (または .rrd)
   ```
   例: `Target[10.240.12.1_gi0_0_0]: ...` なら `WorkDir/10.240.12.1_gi0_0_0.log` を読みます。
3. Target の `@` の後ろ(装置の IP か名前)を、設定の装置と照合します。IF は `#IF名`・`/IP`・ifIndex の順に、設定の IF と照合します。cfgmaker が書く `SetEnv[<名前>]: MRTG_INT_IP="…" MRTG_INT_DESCR="…"` があれば、その IP と IF 名も使います。
4. `.log` は2行目(最新の区間)の入力・出力のバイト/秒を、8倍して bps にします。RRD は `rrdtool fetch <ファイル> AVERAGE -s -1800` を実行し、値の入っている最新の行の `ds0`(入力)・`ds1`(出力)を読みます。

補足:

- `rrdtool` コマンドは PATH から探します(`shutil.which('rrdtool')`)。パスを設定で変えることはできません。
- 次のときは、設定で上書きできます。
  - `WorkDir`・`LogFormat` を変えたい: `sources.mrtg.workdir` / `logformat`
  - 自動の対応づけが外れる: `sources.mrtg.targets: [{target: <名前>, device: <装置ID>, if: <IF名>}]` で、その Target だけ指定
  - ファイル名を小文字にしない: `lowercase: false`
- `stale` 秒より古い値は表示しません。MRTG が止まっていても 0 bps とは見せません。
- **`mrtg.cfg` の場所の候補**(インストーラーが順に探します):

  | 場所 | よくある環境 |
  |---|---|
  | `/etc/mrtg.cfg` | Debian / Ubuntu |
  | `/etc/mrtg/*.cfg`(`mrtg.cfg`) | RHEL / Rocky / Alma |
  | `/usr/local/etc/mrtg/*.cfg` | FreeBSD |
  | `/usr/local/mrtg-2/conf/*.cfg` | MRTG のソースを自分で入れた場合(MRTG の手順書の場所) |

  どれにも無いときは、`find / -name 'mrtg*.cfg' 2>/dev/null` で探し、`./install.sh --mrtg-cfg <パス>`(複数回指定できます)で渡すか、設定の `sources.mrtg.cfg` に書きます。
- **探した場所は画面に出ます**: `./install.sh --detect-only` を実行すると、各候補が「無い」か「ある」かと、あっても使えなかった理由(読めない、`Target[...]` が1つも無い)が出ます。
  ```
  MRTG 候補 /etc/mrtg.cfg: 無い
  MRTG 候補 /etc/mrtg/*.cfg: /etc/mrtg/mrtg.cfg
  注: MRTG: /etc/mrtg/mrtg.cfg はありますが Target[...] が1つもありません(cfgmaker で作ると入ります)
  ```
- **設定に書いたパスが無いとき**: 実行中は、`GET /api/health` の `errors.mrtg` に `mrtg.cfg が見つかりません: <パス>(候補: …)` と出ます。以前は黙って対象なしになっていました。

## 7. Cacti

| 項目 | 内容 |
|---|---|
| 取るもの | IF の入力・出力の bps(Cacti の「Interface - Traffic」のデータソース) |
| 取り方 | **ローカルの RRD を `rrdtool fetch` で読む**(HTTP ではない) |
| 単体版の設定 | `sources.cacti: {rra_dir: /var/lib/cacti/rra, targets: [{device: cr1, if: GigabitEthernet0/0/0, file: cr1_traffic_in_12.rrd}]}` |
| 実装 | `cvs/legacy_sources.py` の `LegacyRates.rates()` と `rrd_last()` |

**Cacti は、RRD のファイル名と IF の対応を設定に書く必要があります。** どのファイルがどの IF かは Cacti のデータベースにしか無く、ファイル名(`<ホスト>_traffic_in_<番号>.rrd`)からは IF が分からないためです。

- `install.sh` は `rra_dir` で見つけた RRD の一覧を、設定の下書きにコメントで出します。番号と IF の対応は、Cacti の画面(Data Sources)で確かめて `targets` に書いてください。
- `file` は `rra_dir` からの相対パスでも、絶対パスでも書けます。
- データソース名は既定で `traffic_in` / `traffic_out` です。違う場合は `ds_in` / `ds_out` で指定します。値はバイト/秒として読み、8倍して bps にします。
- `stale`(既定900秒)より古い値は表示しません。
- **`rra_dir` の候補**: `/var/lib/cacti/rra`(Debian 系の既定)、`/usr/share/cacti/rra`、`/var/www/html/cacti/rra`、`/usr/share/cacti/site/rra`。同じく `--detect-only` に「Cacti 候補」として出ます。設定の `rra_dir` が無いときは `errors.cacti` に出ます。

## 8. Zabbix

| 項目 | 内容 |
|---|---|
| 取るもの | IF の流量・IF の状態・未解決の問題・ホストの到達性 |
| 取り方 | HTTP `POST <url>/api_jsonrpc.php`(JSON-RPC)。`host.get`、`item.get`、`problem.get` |
| 単体版の設定 | `sources.zabbix: {url: http://<zabbix>/api_jsonrpc.php, token: ${ZABBIX_TOKEN}}`(user / password でも可) |
| 実装 | `cvs/sources.py` の `Zabbix`(`hosts()` / `interface_items()` / `problems()`) |

- **ホストとの対応**: `host.get` を、設定の装置の `zabbix_host`(無ければ hostname)で絞って呼びます。
- **流量と IF の状態**: `item.get` で「Network interfaces by SNMP」系テンプレートのアイテムを読みます。名前が `Interface <IF名>: Bits received / Bits sent / Operational status` のものを使います。
- **問題**: `problem.get` の重大度を、Disaster・High は「警」(high)、Average は「警」(medium)、Warning は「注」にします。
- **注意**: Zabbix の Cisco テンプレートは、admin shutdown の IF のアイテムを LLD で無効化します。これは「断」として扱います。
- **認証**: API トークンは Zabbix 7.2 以降の `Authorization` ヘッダと、それより前の `auth` パラメータの両方で送ります。

---

## 取り込みの優先順位

1本のリンクの流量が複数のデータ元から取れる場合は、次の順に使います。画面右上の `SOURCE:` に、どこから取ったかが出ます。

1. Prometheus(ヴァルハラ)
2. Zabbix
3. MRTG / Cacti

リンク断は、STATE(ラボのみ)・SYSLOG・TRAP・POLL のどれか1つでも断を示せば断とします。

## 確かめ方

| データ元 | 確かめ方 |
|---|---|
| Prometheus・Loki・trap・フロー・SNMP の ACL | `iac/nirvana-monitoring` で `./deploy.sh check`(結果は [nirvana-monitoring-iac.md](nirvana-monitoring-iac.md)) |
| MRTG・Cacti | 本物の MRTG(cfgmaker)と Cacti の RRD での確認は [nirvana-addon-monitoring-ai.md](nirvana-addon-monitoring-ai.md) |
| Zabbix | 本物の Zabbix 7.0 での確認は [cyber-view-standalone.md](cyber-view-standalone.md) |
| 取り込みの状態 | 単体版の `GET /api/health` の `errors` に、取れなかったデータ元とその理由が出る |
