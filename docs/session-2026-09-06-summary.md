# セッションまとめ（2026-09-05 〜 09-06）

2日分の作業をまとめて記録する。A: コード変更の要点（コミットログベース）、
B: 実機とのやり取りで見つかった勘違い・バグの経緯、の両方を含む。

---

## A. 実装したもの（コミット順）

### 09-05（昨日）

| 時刻 | 内容 |
|---|---|
| 00:28 | `docs/routing-regression-checklist.md` 新規作成（RIP/OSPF/BGP/EIGRP 2台構成の試験項目） |
| 00:51 | OSPF router-id / RIP・BGPのnext-hop表示バグを修正 |
| 04:37 | ruffをCIのLintゲートに追加 |
| 05:34 | OSPF router-id自動選出ロジックを実装（Cisco仕様：loopback優先・最大IP） |
| 05:56〜06:33 | SNMPトラップ→Prometheus/Alertmanagerの橋渡しツールを追加。Si-Rの実SNMPコンフィグコマンド体系を実装 |
| 07:06 | Si-Rの`snmp manager`をtrap配信に接続。動的追加した装置にもSNMPエージェントを起動するよう修正 |
| 07:21 | APRESIAの`config ipif`がCLI略称展開で壊れるバグを修正 |
| 07:36〜07:49 | route_injectorのUX改善：RIPタブをデフォルトに、フィールド別の日本語エラー、「1件追加」ボタン、送信ループが経路リストを都度再読込するよう修正 |
| 11:00〜15:25 | route_injectorにトラフィック生成/測定タブを追加。実際に動かして見つけた3つの計測バグを修正。マルチコア対応。NIC表示の文字化けバグ修正。目標スループットからレート自動計算。MTU超過時の警告 |
| 23:35 | EIGRP実装（Cisco/Catalyst/Nexus）、Si-Rのリンク断復旧（`ether use off`）、802.1Qサブインタフェース |

### 09-06（今日）

| コミット | 内容 |
|---|---|
| `a1c26b0` | IPsec/IKEタイマー実装。Si-Rの`ike retry`/`ike dpd use/idle/retry`/`sessionwatch interval`、Ciscoの`crypto isakmp keepalive`を実際のDPD検知窓として機能させた |
| `782d23d` | Si-R手動鍵設定IPsec（`remote ap ipsec type manual`）を実装。暗号化しないトンネル（AH認証のみ、ESP-NULL）を含む |

---

## B. 実機とのやり取りで見つかった勘違い・バグ

### 1. route_injectorの「送信数だけ進んで届いていない」問題

宛先IPを`192.168.1.2`（存在しないIP）にしていたためARPが返らず、フレームが1つも出ていなかった。ツール側は送信APIの成功数を数えているだけなので、実際にNICから出ていなくてもカウンタが進む、という紛らわしい状態だった。正しい宛先（Si-Rの実IP）に直して解決。

### 2. TCPだと繋がらない

宛先ポートを誰も待ち受けていない状態でTCPを選ぶと、3-wayハンドシェイクが成立せず1パケットも流れない。UDPに変えて解決。

### 3. NICのリンク速度表示から物理NICが消える

日本語Windowsで「イーサネット」のような日本語アダプタ名がPowerShellの出力エンコーディングの都合で欠落し、仮想アダプタ（VMware/Wi-Fi）だけが表示されていた。UTF-8固定＋物理NIC優先ソートで修正。

### 4. Si-Rの`show ether`系コマンドが固定文字列を返していた

`ether`が略称展開で`etherchannel`に化けるバグがあり、`show ether brief`等が正規表現にマッチせず別の固定出力にフォールバックしていた。APRESIAの`config`→`configure`と同種の問題。

### 5. Si-Rには`shutdown`が無い

実機のリンク断コマンドは`ether <group> <port> use <on|off>`（マニュアル4.1.2）。`ether`→VLAN→`lan`の2段をたどって、どのlanインタフェースが落ちるかを決める必要があった。

### 6. サブインタフェースのVLANタグ不一致を検証したらOSPFで想定外の隣接が発生

タグ不一致でも隣接して見えた原因は、仮想トポロジとは無関係に動く`engine/real_ospf_agent.py`の実scapyリスナーがloopback上で別の隣接を作っていたため。検証はEIGRPに切り替えて解決（実パスと実リスナーが混線する、という以前からの既知の注意点の再発）。

### 7. IPsec実装で見つかった4つの既存バグ（Cisco）

- DPD検知窓がエンジンの共有属性に直接代入されており、keepalive設定が異なる複数装置でクロストークしていた
- `crypto map <name> interface <if>`の大文字小文字が、定義時の表記と食い違い、適用先マップが見つからず登録が無言で失敗
- WAN側インタフェースのIP特定が部分一致で、`"GigabitEthernet0/0"`が`"GigabitEthernet0/0/0"`に誤って一致
- 実機で最も一般的な構文（`interface`配下で`crypto map <name>`とだけ書く）が一切登録されず、`show crypto ipsec sa`が常に空だった

### 8. `show running-config`が2つ存在し、片方が死んでいた

`engine/rules.py`側にCisco/Si-Rのrunning-config生成コードが以前からあったが、実際のAPI経路では`handle_protocol_show`が先に`show running-config`を横取りするため一度も呼ばれていなかった。crypto関連の設定をしても表示に反映されない状態だったため、app.py側の実際の生成器に出力を追加した。

### 9. 自分のテストが既存テストとIPアドレスを衝突させた

新規テストで`203.0.113.x`・`198.51.100.21-39`を使ったところ、既存のOSPFテストと衝突し、フルスイート実行時にだけ失敗するcrosstalkを起こした。`docs/routing-regression-checklist.md`に以前書いた「サブネットを使い回すと無関係な装置が隣接して見える」の教訓どおりの再発。テスト側のIPを重複しない範囲に振り直して解消。

### 10. 「暗号化しないトンネル」はIKE側では組めない

`ipsec ike encrypt null`はPhase1（鍵交換自体）の暗号化を無くすだけで、データ面（ESP/AH）の暗号化有無を制御するコマンドは別系統（`remote ap ipsec send/receive`、手動鍵設定）にしかない。マニュアル10.2.29の表（認証/暗号アルゴリズムとプロトコルの組み合わせでSA作成可否が決まる）をそのまま実装し、AH認証のみ・ESP-NULLの両方を実現した。

---

## 運用上の注意（今回新たに判明した分）

- Si-Rの`show running-config`は「入力したコマンドをそのまま再現する」実装（`app.py _build_running_config`）。省略した引数のデフォルト補完は内部状態では正しく行われるが、表示には反映されない。
- IPsec/IKEは他プロトコル（RIP/OSPF/BGP）と違い、実パケットを送受信する「本物版」エージェントが無い。実機Si-RとこのPCの擬似Si-Rを本当にIPsecで繋ぐには、strongSwan等の別ソフトウェアが必要。

---

## 関連ドキュメント（このまとめの元になった詳細メモ）

このまとめは要点だけを抜き出したもの。実機とのやり取りの生ログや
コマンド例、機能の網羅的な一覧は以下を参照。

- **`docs/sir-g110b-real-device-comparison.md`** — 実機Si-R G110Bとの
  比較記録。09-04時点の`show running-config`突き合わせに加え、
  今回（09-05/09-06）の追記として、PC-Aからの実機RIP交換試験と
  `show ether statistics`でのトラフィック実測試験、それを踏まえて
  `ether`↔VLAN↔`lan`の対応関係を実装した経緯を記載
- **`docs/routing-regression-checklist.md`** — RIP/OSPF/BGP/EIGRP の
  2台構成試験項目と、見つかった問題・確認済み事項の一覧
  （IPsec/IKEタイマー、手動鍵設定の節もここに追記済み）
- **`docs/feature-inventory.md`** — 実装済み全機能の網羅的インベントリ。
  EIGRP・802.1Qサブインタフェース・IPsec/IKE（DPD・手動鍵含む）を
  今回追加
