# 作業記録(進捗とやったことの振り返り用)

新しいものが上。各項目の「詳細」に、その時の検証記録(実際のコマンド・出力・画面)がある。
commit は作業ブランチ `claude/affectionate-johnson-rz69wa`。

## 2026-11-17(深夜)— WORLD 地球儀(クラウド・Radar・IHR・MAWI)

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 03:05 | 「インターネットのトラフィックも出せる？」→ /cyber-view に **WORLD**。AWS / GCP のリージョン別流量(公開 IP 帯)、Cloudflare Radar、IIJ IHR、MAWI の pcap 再生 | 本物の AWS IP 帯(10758)でラボの ping が東京・バージニア等の弧に。Radar / IHR / MAWI / GCP はこの環境でブロック → 理由を表示、見本モードを用意。MAWI の匿名化アドレスが AWS 帯に当たって数えられていたのを除外、IHR の同じ障害の重複、弧が画面外に飛ぶのを直した | [world-view.md](world-view.md) |

## 2026-11-17(夜)— 通信が多いときの表示 / サイバー演出 FX / L2 ループ検知

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 00:30 | 本家 NIRVANA改の写真を見て「通信増えたらこうなる？」→ DEMO + 8並列の ping で負荷をかけて確認し、重なりを直した | 同じ結果を「×N」にまとめ、1か所3枚まで、プレートどうし・メニューと重ねない。HUD を避ける判定が fixed 要素で効いていなかった(offsetParent が null)のも直した | [cyber-view-3d.md](cyber-view-3d.md)(末尾) |
| 01:10 | 「cyber 感増しで」→ **FX**(六角グリッド・データの雨・警報中の火の粉・光の尾・走査線・グリッチ) | FX オフ 16.7 fps → オン 15.0 fps。火の粉を画像の貼り付けにして軽くした。全体テスト前に `saved_config.json` を退避しないと前回の装置が混ざって落ちる(既知)ことを再確認 | 同上 |
| 02:15 | **L2 ループ検知**(`engine/l2_loop.py`)。輪の検知・嵐・警報 LOOP・ROUTER LINKS の赤い輪・storm-control / BPDU guard / errdisable recovery / APRESIA loopdetect・RESPONSE のループ遮断 | 3台の Catalyst で使用率 92%、storm-control action shutdown で2秒で止まり、30秒ごとに recovery → 再ループ → 再停止(実機と同じ)。以前は輪ができると storm-control 未設定のポートにも偽の ERR_DISABLE を出し、しかも止めていなかったのを直した | [l2-loop-detection.md](l2-loop-detection.md) |

## 2026-11-17(昼)— ニルヴァーナで見る監視スタックの IaC

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 03:00 | `iac/nirvana-monitoring/`(snmp_exporter + ヴァルハラ + Loki + syslog 中継 + ニルヴァーナ)。エミュレータの装置が本物の sFlow(NX-OS / Arista)・NetFlow v5(IOS)を送るように | 本物の docker で5コンテナ起動、NetFlow 56 + sFlow 25 件をニルヴァーナが受け、ヴァルハラの流量・Loki のログ・本物の ping の回線チェックが揃った。Nexus は mgmt0 で SNMP を受ける・フローが「PING OK undefined」と出る・CPU の注が「WARN FLAP」を直した | [nirvana-monitoring-iac.md](nirvana-monitoring-iac.md) |

## 2026-11-17(朝)— A〜E: 遮断・隔離 / ATT&CK / 複数拠点 / 回線チェック / フロー・外部センサー

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 02:30 | IT Leaders / BIGLOBE / 日本ラッドの資料から要素を拾い、A〜E をまとめて実装。RESPONSE(access-class / Null0 / shutdown、解除、自動、pcap)、ATT&CK ビュー、ネクサス、WAN、NetFlow/sFlow、他社 syslog、メール | 本物の SSH 総当たり → ログイン → 探索 → 持ち出しが ATT&CK で「窃取まで」、2拠点の同じ攻撃元を中央が見つけて返し、RESPONSE で押すと実 SSH が本当に切られた。Null0 が直結に負ける・RESPONSE のボタンが消える・試験用 sFlow の詰め忘れ・スタンドアロン版の ping 依存を直した | [nirvana-response-attack-nexus-wan-flow.md](nirvana-response-attack-nexus-wan-flow.md) |

## 2026-11-17 — ダークネット観測(NICTER のエッセンス)

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 01:50 | /cyber-view に **DARKNET**。使われていない帯に届いた通信をスキャン / DDoS の跳ね返り / 設定ミス / リフレクション攻撃の準備に分類、送信元からの放物線の矢(プロトコル別の色)、日ごとの観測統計。本物のパケットは lo に引き込んで受け身に観測、ラボの ping も記録 | 本物の nmap のスキャン、送信元を偽装した SYN-ACK(跳ね返り)、DNS/NTP/SSDP/memcached の探索、同じアドレスへの繰り返しが、それぞれ分類と警報(花びら DARKNET)になった。README を日英に、先頭に動画 | [darknet-nicter.md](darknet-nicter.md) |

## 2026-11-16(深夜2)— 経路と CPU を SNMP で定期取得して「情」「注」

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 16:20 | エミュレータの SNMP に inetCidrRouteTable / ipCidrRouteTable / 経路数。スタンドアロン版が SNMP で経路表と CPU を直接定期取得 → 情・注。ラボ版も CPU の情・注(大きな ping で経路上の CPU が上がる) | 本物の snmpwalk で経路表を確認。スタンドアロン版でスタティック経路の追加・削除が情、CPU 高負荷が注。cpmCPUTotal5minRev の OID が1つずれていた(.7 → .8)のを直した | [snmp-route-cpu-info.md](snmp-route-cpu-info.md) |

## 2026-11-16(深夜)— ニルヴァーナに Grafana の機能を

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 13:10 | /cyber-view に **BOARD / EXPLORE / ALERTS**: ダッシュボード(8種のパネル・変数・時間範囲・自動更新・注釈・編集・取り込み/書き出し)、問い合わせ、アラートルールと webhook 通知。内部の時系列と PromQL の基本部分を自前で持ち、Prometheus が無くても動く | 3台 OSPF のリンク断でルールが発報 → 本物の webhook に firing/resolved が届き、警報の花に RULES の花びら。Grafana 用の `cisco-traffic.json` をそのまま取り込んで本物の Prometheus のデータで表示できた。3D が透けて読みにくい・スマホでパネルが重なる等を直した | [nirvana-board.md](nirvana-board.md) |
| 12:25 | 当て字(爾琉婆亜那 / 刃流覇羅)を試したが、依頼で取り消し | — | — |
| 12:15 | BGM(ジャズ / レゲエ / アンビエント)を DEMO 以外でも | — | [cyber-view-3d.md](cyber-view-3d.md) |

## 2026-11-16(夜)— ヴァルハラ × ニルヴァーナ

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 11:55 | ① `/metrics`(ニルヴァーナの判断をヴァルハラへ)+警報ルール、② 記録からの 3D 再生、③ 予測と「いつもと違う」、④ OSPF-MIB(エミュレータの SNMP → snmp_exporter → スタンドアロン版の OSPF 表示)、⑤ ヴァルハラの警報を VALHALLA の花びらで | 本物の Prometheus/snmp_exporter で確認。リンク断で `NirvanaLinkDown` が発報 → 花に取り込み、再生に断と復旧が出た。予測値が 100% を超える・予測プレートが隣と重なる・再生中の IF 名が「?」を直した | [valhalla-nirvana.md](valhalla-nirvana.md) |

## 2026-11-16(夕方)— Cyber View の OSPF モード

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 08:55 | 画面上の Prometheus の呼び名を **ヴァルハラ(VALHALLA)** に(表示だけ。`PROMETHEUS_URL` や API 項目名は変えない) | 標準版・スタンドアロン版とも | [cyber-view-3d.md](cyber-view-3d.md) の「ヴァルハラ」 |
| 08:30 | **CI の立て直し**: lint の未使用 import 3件、BIG-IP シナリオを本物の http.server で、装置削除時に http.server も止める、CI を作業ブランチでも実行 | main の CI は lint と BIG-IP シナリオで失敗が続いていた | `.github/workflows/` |
| 07:45 | **背景の切り替え**(標準 / 金魚鉢 / 富士山、右の操作ボタン列の一番下。選択は記憶、`?bg=` でも指定) | Canvas で描画(画像ファイル無し)。スマホでは富士山が状態表示に隠れたので地平線を下げた | [cyber-view-3d.md](cyber-view-3d.md) の「背景の切り替え」 |
| 07:05 | **OSPF ボタン**: 隣接ごとの状態と Dead タイマーの残り、エリアの色分けと ABR、宛先を選ぶと最短経路ツリー(矢印)、SPF 再計算の波紋 | 3台の三角形で、片側 shutdown → 経路の迂回 → 対向が Dead タイマー(40秒)切れで Down → 復旧まで画面で追えた | [cyber-view-3d.md](cyber-view-3d.md) の「OSPF モード」 |

## 2026-11-16(午後)— Cyber View を実機の監視環境へ持ち出せる単体版

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 06:10 | **`cyber-view-standalone/`**: 画面はラボ版と同じ、データは Prometheus+snmp_exporter / Loki / Zabbix から読む単体版(FastAPI、設定は YAML、docker 対応) | 本物の Prometheus・Loki・Zabbix 7.0 を立ててライブ確認。Prometheus+Loki 構成・Zabbix だけの構成の両方で、流量・LINK DOWN(SYSLOG/POLL)・状態ボード・警報が出た | [cyber-view-standalone.md](cyber-view-standalone.md) |
| 06:00 | Zabbix: 管理者 shutdown した IF はテンプレートの自動検出で「無効化」される → down として読むよう修正 | 最初は Zabbix が Link down を出しているのに画面は up のままだった | 同上 |
| 06:05 | 画面: DEMO の無いサーバーでは DEMO ボタンを隠す、`?token=`、データ元表示を実際の取得元に | ラボ版にも反映 | 同上 |

## 2026-11-16 — Cyber View(モックNIRVANA)の監視機能の仕上げと機種別試験

| 時刻 | やったこと | 結果 / 見つけたこと | 詳細 |
|---|---|---|---|
| 05:05 | OSPF の `network` 文を広い範囲で書くと経路が入らない不具合を修正(デモ作成中に発見) | 範囲をそのまま広告していた → 範囲内の IF サブネットを広告 | `tests/test_ospf_network_range_and_demo.py` |
| 04:40〜 | **機種 × プロトコルの個別試験**(1パターン = 1つの専用環境、台数から自動でアドレス割り当て) | 各パターンの構成・投入コマンド・段階ごとの show 出力/ログ/アラート/画面をパターン別のマークダウンに書き出し | [verification/README.md](verification/README.md) |
| 04:40〜 | **デモモード**(DEMO ボタン: 自動シナリオ + ブラウザ内で合成する音楽 + 視点の自動巡回) | 音源ファイル不要・著作権の心配なし | [cyber-view-3d.md](cyber-view-3d.md) の「デモモード」。90秒周期のシナリオで、障害時に R2 経由へ迂回することまで確認 |
| 04:40 | フラップ判定: 最初に見た状態(設定時の no shutdown)を基準にし、変化として数えない | 障害で1回落として戻しただけで「フラップ」と出ていたのを修正 | `tests/test_flap_rules.py` |
| 04:23 | Catalyst / SR-S / Si-R × STP / RIP / OSPF / BGP を一括で検証(144項目) | 全OK。途中で STP の不具合3件・Si-R BGP の不具合1件・ROUTER LINKS の重なりを修正 | [vendor-protocol-matrix-cyber-view.md](vendor-protocol-matrix-cyber-view.md) |
| 03:38 | 流量率アニメーション(多いほど速く・密に・大きく、色は使用率) | NETWORK 画面にも常時の流れを追加 | [cyber-view-3d.md](cyber-view-3d.md) |
| 03:36 | 「情」(INFO)・「注」(警告)の六角マーク | 赤の「警」と合わせて3段階 | 同上 |
| 03:25 | ログからのフラップ検知(Cisco/Catalyst/Si-R ごとのルール)、経路フラップ、視点操作(ピンチ・平行移動・待機中の回転) | MAC フラップは IOS 判定済みなので1回で警告 | 同上「v8」 |
| 03:18 | 経路の追加/削除/変更を INFO 表示(3秒ごとにルーティングテーブルを比較) | OSPF で学習した経路も実際の次ホップで表示 | 同上 |
| 03:11 | 攻撃時の見え方(実 nmap のポートスキャン / ssh-brute + login block-for) | 状態ボードが最大10秒古いままになる表示ずれを修正 | 同上「攻撃を受けたときの見え方」 |

## 2026-11-15 — Cyber View の土台

| 時刻 | やったこと | 詳細 |
|---|---|---|
| 23:36〜00:10 | LINK DOWN の検知経路を統合(STATE / SYSLOG / TRAP / POLL)、SNMP 応答なし(消失)、IOS login block-for、IDS 接続スキャン検知 | [cyber-view-3d.md](cyber-view-3d.md) v4・v5 |
| 20:08 | Grafana / Loki の表示部分を Cyber View で(Prometheus・Loki から読む)、立体プレート、図9風アラートの花 | 同上 v3 |
| 17:48〜18:13 | NIRVANA改風 3D 画面(/cyber-view)、ROUTER LINKS、状態ボード | 同上 |
| 14:51〜17:33 | Cisco トラフィック監視 IaC(Prometheus + snmp_exporter + Grafana)、/traffic-flow、侵害検知とトラフィック再現 | [cisco-traffic-monitoring-iac.md](cisco-traffic-monitoring-iac.md)、[traffic-flow-visualization.md](traffic-flow-visualization.md) |

## Prometheus との関係(よく聞かれること)

Cyber View は `PROMETHEUS_URL` を設定すると Prometheus から次を読む(未設定ならエミュレータ内部の値):

- リンクの流量: `rate(ifHCOutOctets{job="cisco-snmp"}[1m]) * 8`(方向ごと。片側だけ監視なら `source: mixed`)
- IF の状態: `ifOperStatus`(2/7 = 断 → LINK DOWN の検知経路「POLL」)
- 収集できているか: `up{job="cisco-snmp"}`(0 = 「NO RESPONSE 消失」)

`LOKI_URL` を設定するとログ(syslog 観点・浮かぶログ)も Loki から読む。Prometheus / snmp_exporter / Loki の
起動は `iac/cisco-traffic-monitoring/deploy.sh up`(詳細は cisco-traffic-monitoring-iac.md)。

## まだ残っていること

- Si-R の `show spanning-tree` の表示のずれ(ルートの装置に Root ポートが出る等。動作自体は正しい)
- SR-S に Si-R 形式の `lan 0 ip address` を打つと `% Invalid input` なのに running-config に残る
- 直結サブネット内の誰も持たない IP への ping が成功する(既存テストが仕様として固定しているため判断待ち)
- Si-R の SCP サーバー機能(実機のコマンド確認待ち)
