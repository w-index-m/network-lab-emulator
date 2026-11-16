# Cyber View(/cyber-view): NIRVANA改風の3D表示

NICT の NIRVANA改 の論文図(Network View・/0〜/32 のドリルダウン・Host View・
サブネットを球で囲む表示・赤い六角形のアラート)を参考に、このエミュレータの
実データだけで描く3Dビュー。ロゴ・名称・実在ベンダーのマークは使っていない
(名前は「CYBER VIEW」)。外部ライブラリ無しの Canvas 2D と自前の透視投影で描いている。

`/traffic-flow`(2Dの経路アニメ + アラート + 再現)と相互にリンクしている。

## 画面

| 階層 | 表示 |
|---|---|
| /0・/8・/16 | 16×16 = 256枚のパネルが床面に並ぶ。1枚が1つのアドレスブロック(/0なら x.0.0.0/8)。装置のいるブロックは明るく、直近30秒の通信量で白く光る。クリックで1階層下へ |
| /24 | サブネット全体を**ワイヤーフレームの球で囲む**(論文の図2)。パネル=各IPで、装置のいるIPには通信量に比例した高さの柱が立つ。クリックで Host View |
| /32 (Host View) | 球の中央に装置カード(ホスト名・機種・全IF の IP/UP・DOWN/入出力バイト・フロー数・失敗数)、球面に通信相手(リンク相手 + 実際に通信した相手)、下に SESSION(そのホストの直近7フロー) |

共通:
- 三角形の粒が通信の向きに飛ぶ(水色=到達、赤=到達不可で途中で×、橙=アラートに関係したフロー)。
  今のブロックの外にいる相手とのフローは外周の上空から飛んでくる。
- アラート(直近10分)は、対象パネルの上に赤い「警」の六角形と、ルール別の花びら
  (SCAN / UNRCH / FLOOD / MTU / LINK と件数、色は重要度)が浮かび、パネルまで螺旋の柱で繋がる。
- 左上: `AlertDB:[n] Flows:[n] Hosts:[n] Links:[n]` と時刻。右上: 今の CIDR を大きく表示 + パンくず。
- 左下: `ANY -> ANY` に送信元・宛先(IPの前方一致かホスト名の一部)を入れるとその粒だけ表示。
- 右: 最新アラート6件。クリックするとそのアラートの送信元の Host View へ飛ぶ。
- ドラッグで回転、ホイールで拡大縮小、Esc / Backspace / 「↑ 戻る」で1階層戻る。操作が無いとゆっくり自転する。
- 最初は前回見ていたブロックを開く(localStorage、使えなくても動く)。

![/0](images/cyber-view-0.png)
![/24 をサブネットの球で囲んだ表示](images/cyber-view-24.png)
![Host View](images/cyber-view-host.png)

## データ

- `GET /api/traffic/hosts`(新規): 全装置の IP・プレフィックス・IF の UP/DOWN
  (shutdown と vnet のリンク断)・入出力バイト(dp_engine、SNMP の ifHC*Octets と同じ値)・リンク相手。
  127.0.0.0/8 は表示の意味が無いので除外。5秒ごと。
- `GET /api/traffic/flowmap?since=&alert_since=`(既存): 新しいフローとアラートだけを2秒ごとに取得。
- フローイベントに **`src_ip`** を追加した(以前は送信元の装置IDだけで、IPアドレス空間に置けなかった)。
  送信元IP = 経路の次ホップへ向くIFのIP(IOS の ping と同じ選び方)。次ホップが無ければ最初のIP。

## ライブ検証

CR1–CR2–CR3 のラボ(`iac/cisco-traffic-monitoring/lab/provision_cisco_lab.sh` + CR3 追加)を作り、
`ping 10.240.3.1 repeat 50 size 1500`(CR1→CR3 LAN)、CR3 から 172.16.1〜9.1 へのスイープ
(SCAN と UNRCH のアラート)、`ping 10.240.12.2 size 9000`(MTU アラート)、
到達不可の宛先への ping を流しながら Playwright で /0 → 10 → 240 → 12 → 10.240.12.1 と
実際にパネルをクリックして降り、各階層を撮影した(上の画像。1400×860 と 390×800、JS エラー 0)。
Esc で /32 → /24 に戻ることも確認。

確認しながら直したもの: 階層を移った後に前の階層のツールチップが残る、/24 の球が画面からはみ出す
(/24 だけカメラを引く)、Host View で SESSION 表示がフィルタ欄と重なる、スマホ幅でパンくずが
状態行と重なる・カードの列がはみ出す(スマホではバイト列を省略)。

## テスト

`tests/test_cyber_view.py`: ページのルート・認証除外・トークンのキー、2つのページの相互リンク、
`/api/traffic/hosts` の形と値(カウンタ・shutdown で DOWN・127.x 除外)、
フローの `src_ip` が次ホップ側IFのIPになること。

## スコープ外

- 論文の図にある「通信の自動遮断」は入れていない(アラートは表示のみ)。
- 描けるのは ping 由来のフローだけ(エミュレータに実データプレーンが無いのは /traffic-flow と同じ)。
- 同じIPを持つ装置が複数ある場合(デフォルト装置どうし等)、パネルには最初の1台だけを表示する。

## ROUTER LINKS(ルータ間トラフィックと問題の有無)

上部の **ROUTER LINKS** ボタン(または `/cyber-view?view=links`)で、ルータ同士のリンクだけを
円形に並べた表示に切り替わる。端末(pc)・NetAttest・ALB とのリンクは出さない。

- **更新間隔**: 既定1分(10秒 / 1分 / 5分を選べる)。表示する bps はその間隔の平均で、
  画面上部に「次回 n秒」と最終更新時刻が出る。
- **リンクの線**: 太さ=通信量(対数)、色=状態。両方向それぞれに三角形の粒が流れ、量が多いほど本数が増える。
  中点に「→ A→B の bps / ← B→A の bps」と状態のラベル。
- **状態**(悪い順): `断`(どちらかのIFがshutdown/リンク断、破線が点滅 + ×) /
  `警告`(使用率80%以上、または両端の装置にアラート。ラベル横に「警」) /
  `注意`(使用率50%以上、またはそのリンクを通ったpingが失敗) / `正常`。
  使用率は ifHighSpeed と同じ 1Gbps を分母にしている。
- **ルータ**: 六角柱。枠の色は接続しているリンクの中で最も悪い状態。
- **右のリスト**: 悪い順に並び、各リンクの両方向 bps・使用率バー・直近1時間のスパークライン
  (1分刻み)・問題の理由(「IF断」「使用率 85%」「経由pingの失敗 3」「警 …」)。クリックで3D側を強調。

### データソース(Prometheus から渡すか)

`GET /api/traffic/links?window=60`:

- **PROMETHEUS_URL が無い場合(既定)**: エミュレータのIFカウンタを10秒ごとにサンプリングし
  (起動時から常時。画面を開いた瞬間から1分平均が出る)、window 秒の差分で bps を出す。
  このカウンタは SNMP の `ifHCInOctets`/`ifHCOutOctets` と同じ値なので、Prometheus で
  `rate(ifHCOutOctets[1m])*8` を取るのと同じ意味の数字になる。
- **PROMETHEUS_URL=http://127.0.0.1:9090 を付けて起動した場合**: `iac/cisco-traffic-monitoring` の
  Prometheus に `rate(ifHCOutOctets{job="cisco-snmp"}[1m]) * 8` を問い合わせ、`instance`(ルータのIP)と
  `ifName`(Gi0/0/0 等の短縮名)でリンクの各IFに対応づける。画面右上のバッジが
  `SOURCE: PROMETHEUS` になる。監視対象外のルータのリンクは内部カウンタで補い、
  Prometheus に繋がらない時は内部カウンタに戻してバッジに「Prometheus 不達」と出す。

実機のルータを Prometheus で監視していて、その数字をこの画面に出したい場合は後者を使う。
エミュレータのラボだけなら Prometheus を挟まなくても同じ数字になる。

## 状態ボード(正常/注意/異常)と立体感

画面左上の **SYSTEM STATUS** に、直近5分の状態を4つの観点で出す
(`GET /api/traffic/health?window=300`、10秒ごとに更新)。問題が無ければ緑の「正常」。

| 観点 | 正常 | 注意 | 異常 |
|---|---|---|---|
| ネットワーク | 全リンク up | 端末側のリンク断 | ルータ間のリンク断 |
| トラフィック | 到達率100%・使用率50%未満 | 到達不可が1件以上 / 使用率50%以上 | 使用率80%以上 / 到達率50%未満(4件以上) |
| セキュリティ | アラートなし | 重要度 low/medium のアラート | 重要度 high のアラート |
| syslog | error・warning なし | warning(重要度4) | error 以上(重要度3以下) |

- syslog は、装置が出すログ(syslog送信と同じ `%LINK-3-UPDOWN` 等、OSPF/IKE などのプロトコルログ)を
  送信先の設定の有無にかかわらず記録したもの。重要度は Cisco 形式の `%FAC-<数字>-` を優先する。
  実機同様 `changed state to up` も `%LINK-3` だが、復旧なので問題には数えない。
  装置側の時刻プレフィックス付き/無しで同じ事象が2回入るのは1件にまとめる。
- 総合状態は床のリング(緑/橙/赤)と、床に貼った「SYSTEM 正常/異常」でシーン内にも出る。
  異常時は /24 の球が赤い六角タイルで覆われ、背景も赤く染まる。
- 立体感: アドレスのパネルを厚みのある板にし(装置のいるパネルは厚く)、遠いパネルは霧で薄くした。
  状態ボードは奥へ少し傾けたホログラム板(厚み・走査線付き)、アイコンは押し出した六角柱。
- ROUTER LINKS で断のリンクは、区間平均に断前の通信が残るため速度ではなく「停止」と出す。
- スマホ幅では状態ボードは上部の4つのチップになる。
- 復旧後、ネットワーク観点はすぐ正常に戻るが、トラフィック・セキュリティ・syslog は
  直近5分の履歴で判定するため、問題のあった時刻が5分の窓から外れるまでは注意/異常のまま残る。

正常時と異常時(CR2–CR3 を shutdown、スイープ、9000byte ping、到達不可 ping)のライブ検証:

![正常 /24](images/cyber-view-v2-ok_24.png)
![正常 ROUTER LINKS](images/cyber-view-v2-ok_links.png)
![異常 /24](images/cyber-view-v2-ng_24.png)
![異常 ROUTER LINKS](images/cyber-view-v2-ng_links.png)

## Grafana / Grafana Loki の表示部分をこのビューで(v3)

Grafana がやっている「Prometheus のメトリクスと Loki のログを画面に出す」部分を、この 3D ビューで
行えるようにした。データの収集(snmp_exporter → Prometheus、`tools/device_log_to_loki.py` → Loki)は
そのまま使い、表示だけを置き換える構成。

```
Cisco CR1/CR2/CR3 ─SNMP→ snmp_exporter → Prometheus ─┐ PROMETHEUS_URL (rate(ifHCOutOctets[1m])*8)
                 └show logging→ device_log_to_loki → Loki ─┤ LOKI_URL (query_range {job=~".+"})
                                                           ↓
                                         エミュレータ /api/traffic/links・health・logs → /cyber-view
```

```bash
PROMETHEUS_URL=http://127.0.0.1:9090 LOKI_URL=http://127.0.0.1:3100 python app.py
```

どちらも無い/繋がらない時は、同じ値を持つエミュレータ内のIFカウンタ・ログ記録で表示する
(状態ボード下部に `METRICS: PROMETHEUS|IF COUNTER  LOGS: LOKI|INTERNAL` と出る)。
Prometheus が片側のIFしか監視していないリンクは、監視外の方向だけ内部カウンタで補う(`source: mixed`)。

### 画面内に立体で出すようにしたもの(右脇の文字一覧は既定で隠し、LIST ボタンで出せる)

- **ROUTER LINKS**: リンクの中点に、通信量に比例した高さの六角柱と、その上の立体プレート
  「NORMAL 7.3 Mbps」(状態: NORMAL / HIGH LOAD / CRITICAL / LINK DOWN)。リンク断は赤く脈動する
  「LINK DOWN 停止」プレート + 火花。両端には**インタフェースごと**の送受信
  (「Gi0/0/2 ↑3.9 Mbps ↓3.4 Mbps」)。真ん中のルータは、繋がるリンクごとに別のIFとして出る。
- **リンクの状態はリンク自体(断・使用率)だけで決める**: 1Gbps に 35Mbps 流れているような
  通常の通信は「NORMAL/正常」。両端装置のアラートや経由pingの失敗ではリンクを警告にしない
  (アラートは「警」の六角形、pingの失敗は PING NG プレートで別に出す)。
- **ping を打つと**、宛先の上に「PING OK 200/200・1500B」(緑)/「PING NG 0/5」(赤)の
  六角プレートが立ち上がる。
- **ログ**(Loki または内部記録)は、出した装置の上に「LOG ERROR %LINK-3-UPDOWN」(赤)/
  「LOG WARN」(橙)/「RECOVER」(緑)のプレートで浮かぶ。
- **NETWORK(/24 等)**: 直近30秒に通信のあった装置の上に「NORMAL 200 kbps」/「NG ×10」の
  プレート(重ならないよう左右に振り分け)。
- **アラートの花(図9風)**: 中央の赤い「警」、周りに検知元ごとの白い花びら
  (IDS=侵入検知ルール、FW=ASA の threat-detection が送信元を shun、SYSLOG=error 以上のログ、
  NET=リンク断)、黄色の多重六角、パネルまでのコイルの柱。実在製品のロゴは使わない。
  セキュリティ観点の判定は IDS/FW のアラートだけで行う。

ライブ検証(本物の Prometheus v2.54.1 + snmp_exporter + Loki 3.1.1 を起動、
`device_log_to_loki.py` で CR1〜CR3 の `show logging` を Loki へ送り、ping でトラフィック):

![正常 ROUTER LINKS(Prometheus/Loki)](images/cyber-view-v3-ok_links.png)
![正常 /24](images/cyber-view-v3-ok_24.png)
![異常 ROUTER LINKS(LINK DOWN、Loki のエラーログ)](images/cyber-view-v3-ng_links.png)
![異常 /24(図9風のアラートの花)](images/cyber-view-v3-ng_24.png)

## link down の検知経路(STATE / SYSLOG / TRAP / POLL)(v4)

link down は、どの経路で気づいても同じ「LINK DOWN」表示になる。LINK DOWN プレートの下に
4つの検知経路のチップが並び、**点灯 = その経路が断を示している / 暗い = up を示している /
破線 = その経路のデータが無い** で、どれで気づいたか・全部一致しているかが分かる。

| 経路 | データ | 断/復旧の判定 |
|---|---|---|
| STATE | エミュレータ自身の IF 状態(ラボ内の装置のみ) | shutdown・リンク断 |
| SYSLOG | `LOKI_URL` があれば Loki(直近1時間)、無ければ内部記録 | `Interface X, changed state to (administratively) down/up`、Si-R の `X Link Down/Up` |
| TRAP | `SNMP_TRAP_LISTEN=0.0.0.0:1162` でアプリが受信した linkDown/linkUp | sysDescr(`host: IF down`)か ifDescr/ifName の varbind と送信元IPで装置・IFを特定 |
| POLL | `PROMETHEUS_URL` の snmp_exporter `ifOperStatus` | 2(down)/7(lowerLayerDown)=断、1=up、その他は判定に使わない |

リンク両端のどちらかの IF について、どれか1経路でも「最後に見た状態が down」ならリンクは断。
`/api/traffic/links` の各リンクに `evidence`(経路ごとの up/down/null)と `down_sources`、
状態ボードのネットワーク観点に「検知: CR2 ⇄ CR3 = STATE / SYSLOG / TRAP / POLL」が出る。
実機監視で trap だけ/syslog だけの環境でも、その経路のチップだけが点いて同じ LINK DOWN になる。

```bash
SNMP_TRAP_LISTEN=0.0.0.0:1162 PROMETHEUS_URL=http://127.0.0.1:9090 LOKI_URL=http://127.0.0.1:3100 python app.py
# 装置側: trap の送信先をこのアプリにする(エミュレータの Cisco)
snmp-server host 127.0.0.1 udp-port 1162 traps public
```

ライブ検証(本物の Prometheus + snmp_exporter + Loki + trap 受信): CR2 で
`snmp-server host 127.0.0.1 udp-port 1162 traps public` → `interface Gi0/0/2` → `shutdown`。
約35秒後(Prometheus の scrape 後)に4経路すべてが DOWN、`no shutdown` で全経路 up に戻り NORMAL。

![4経路すべてが断を示す LINK DOWN](images/cyber-view-v4-linkdown-4sources.png)
![復旧後](images/cyber-view-v4-recovered.png)

この作業で見つけて直した問題: syslog の重複除外(同じ出来事を実 syslog 送信と CLI 通知の両方で
拾うための1秒以内の同文除外)が、`shutdown`→`no shutdown` を1秒以内に繰り返すと最後の
「changed state to up」を重複として捨ててしまい、復旧したのに断のまま残っていた。
間に同じ IF の逆の状態変化が挟まる場合は別の出来事として残すようにした。

## 応答なし(消失)と、スキャン/総当たりの検知(v5)

### SNMP 応答なし(消失)
`PROMETHEUS_URL` があるとき、Prometheus の `up{job="cisco-snmp"}` が 0 の装置(snmp_exporter が
SNMP の応答を得られない: 電源断・到達不可・コミュニティ不一致など)を「応答なし」にする。

- ROUTER LINKS: その装置の六角柱が灰色で点滅し「NO RESPONSE 消失 / SNMP 応答なし」。
  つながるリンクは灰色の点線・「NO RESPONSE 不明」になり(量は分からないので「不明」)、
  検知経路チップの POLL が「LOST」。STATE/SYSLOG/TRAP のチップと見比べると、
  装置自体が落ちたのか SNMP だけが届かないのかが分かる(下の画像は SNMP だけ止めた例で、
  STATE・SYSLOG は up のまま)。
- NETWORK: 装置のパネル上に「NO RESPONSE 消失」。状態ボードのネットワーク観点は
  「異常 ・ 応答なし 1台 / 応答なし: CR2 (10.240.12.2)」。
- `/api/traffic/links` は `unreachable`(装置と instance)と、リンクごとの `lost`・`state: "lost"` を返す。

### Cisco IOS の login block-for(実機コマンド)
```
login block-for 60 attempts 3 within 60
login on-failure log
login on-success log
show login
```
実 SSH/Telnet リスナーでのログイン失敗を数え、閾値で Quiet Mode に入って以降のログインを拒否し、
実機と同じ `%SEC_LOGIN-4-LOGIN_FAILED` / `%SEC_LOGIN-1-QUIET_MODE_ON` /
`%SEC_LOGIN-5-QUIET_MODE_OFF` を出す(`show logging`・Loki・/cyber-view の SYSLOG 花びら)。
`show running-config` にも出て、設定は保存・復元される。

### ポートスキャン(IDS)
実機の IOS ルータにはポートスキャン(接続だけで認証しない)の標準検知は無いので、ネットワーク側の
センサーとして /cyber-view の IDS ルール `conn_scan` を追加した: 同じ送信元から装置の実リスナー
(SSH/Telnet)へ 30秒間に10回以上の TCP 接続で「短時間の大量接続(ポートスキャン/総当たりの疑い)」
(high)。アラートの花は装置の上に IDS の花びらで出る。ASA なら従来どおり
`threat-detection scanning-threat shun` が送信元を遮断し FW の花びらになる。

ライブ検証(本物の nmap 7.94 で CR2 の実 SSH リスナー 10.240.12.2:22 を攻撃):
```
$ nmap -Pn -sV -p 21-25,80,161,443,830 10.240.12.2
22/tcp  open   ssh   Paramiko Python sshd 5.1.0 (protocol 2.0)
$ nmap -Pn -p 22 --script ssh-brute --script-args userdb=users.txt,passdb=pass.txt 10.240.12.2
CR2# show login
     Router presently in Quiet-Mode.
     Will remain in Quiet-Mode for 60 seconds.
CR2# show logging
*Nov 15 23:46:57.218: %SEC_LOGIN-4-LOGIN_FAILED: Login failed [user: admin] [Source: 10.240.12.2] ...
*Nov 15 23:46:57.218: %SEC_LOGIN-1-QUIET_MODE_ON: Still timeleft for watching failures is 60 secs, ...
$ (nmap -sT -sV -p 22 を連続実行)
alert: conn_scan IDS high cr2  10.240.12.2 から 30秒間に 10 回接続 (port 22)
CR2(config)# no snmp-server community netlab-ro     → 約35秒後 up{instance="10.240.12.2"}=0 → 応答なし
```
nmap は同じホスト上から実行したので、Source が装置自身のIPになっている(ラボの都合)。
既定の SYN スキャン(-sS)は TCP 接続を完了しないため装置側の accept に現れず、数に入らない
(実機の IOS も同じく、完了しない接続はログイン失敗にならない)。

![応答なし(消失)](images/cyber-view-v5-no-response.png)
![スキャン・総当たりの検知(IDS / SYSLOG の花びらと NO RESPONSE)](images/cyber-view-v5-scan-login-block.png)

## 攻撃を受けたときの見え方(ライブ確認)

### ポートスキャン(nmap -sT -sV)

CR2(10.240.12.2)の SSH を有効にし、本物の nmap で `nmap -Pn -sT -sV -p 1-1024 10.240.12.2` を5回実行した。
実リスナーへの accept 数から IDS ルール `conn_scan` が発火する
(`cr2 10.240.12.2 から 30秒間に 10 回接続 (port 22)`)。

- CR2 の柱の上に赤い「警」六角形(IDS の花びら・PSCAN)と螺旋柱
- 状態ボード: セキュリティ「異常 / 短時間の大量接続(ポートスキャン/総当たりの疑い)」、他の3観点は正常のまま
- Host View(/32)の上部にも「警 ×1 PSCAN」

SYN スキャン(`-sS`)は接続が成立しないため数えない(実機の IOS と同じ)。

![ポートスキャン /24](images/cyber-view-v6-portscan.png)
![ポートスキャン Host View](images/cyber-view-v6-portscan-host.png)

### SSH 総当たり + login block-for

```
login block-for 60 attempts 3 within 30
login on-failure log
login on-success log
```

を入れた CR2 に `nmap -p 22 --script ssh-brute` を実行。3回失敗で `%SEC_LOGIN-1-QUIET_MODE_ON`、
`show login` は `Router presently in Quiet-Mode`、以降は正しいパスワードでも接続を即切断。
画面では CR2 上に LOG ERROR(QUIET_MODE)/ LOG WARN(LOGIN_FAILED)/ LOG(LOGIN_SUCCESS)の
プレートが立ち、状態ボードは syslog「異常」になる。

ライブ確認中に、ボードが最大10秒古いまま(ログは浮かんでいるのに「正常」)になる表示ずれを見つけた。
新しいアラートや warning 以上のログを受け取った時点でボードも即時更新するよう修正済み。

![SSH 総当たり](images/cyber-view-v7-ssh-brute.png)
