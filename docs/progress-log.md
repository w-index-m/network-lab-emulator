# 作業記録(進捗とやったことの振り返り用)

新しいものが上。各項目の「詳細」に、その時の検証記録(実際のコマンド・出力・画面)がある。
commit は作業ブランチ `claude/affectionate-johnson-rz69wa`。

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
