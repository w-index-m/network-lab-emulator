# 機種 × プロトコル 個別試験の一覧

1行 = 1シナリオ = 1つの専用環境。各リンク先に、構成(自動割り当てのアドレス)・投入コマンド・段階ごとの show 出力/ログ/アラート/状態ボード/画面をまとめてある。

<!-- results:start -->
最終更新: 2026-11-16 04:58

| 機種 | STP | RIP | OSPF | BGP |
|---|---|---|---|---|
| Cisco Catalyst | [✅ 10/10](catalyst-stp.md) | [✅ 14/14](catalyst-rip.md) | [✅ 14/14](catalyst-ospf.md) | [✅ 14/14](catalyst-bgp.md) |
| Fujitsu SR-S | [✅ 10/10](srs-stp.md) | [✅ 14/14](srs-rip.md) | [✅ 14/14](srs-ospf.md) | [✅ 14/14](srs-bgp.md) |
| Fujitsu Si-R | [✅ 10/10](sir-stp.md) | [✅ 14/14](sir-rip.md) | [✅ 14/14](sir-ospf.md) | [✅ 14/14](sir-bgp.md) |
<!-- results:end -->

再実行: `python3 tools/verification/scenario_report.py all [--devices N]`(1シナリオずつエミュレータを立ち上げ直して実行。ライブのエミュレータや全体テストとは同時に動かさない)

台数を変えた例: [Catalyst × OSPF 4台](catalyst-ospf-4dev.md)(`--devices 4`。アドレスとリンクは台数から自動で割り当て)

## 各レポートの見方

1. **構成** — 台数から自動で決めたトポロジとアドレス(`10.<機種>.<プロトコル*40+リンク番号>.0/24`、LAN は +30/+31)
2. **投入したコマンド** — 装置ごとに折りたたみ。そのまま実機風の手順として読める
3. **確認項目** — 正常 / 障害 / 復旧 / フラップ の判定(✅ / ❌)
4. **画面** — 正常時と障害時の ROUTER LINKS
5. **段階ごとの記録** — 状態ボード、リンクと検知経路(STATE / SYSLOG / TRAP / POLL)、show 出力、
   /cyber-view が見ているログ、アラート、経路の変化(情 INFO)

## 試験の流れ(1シナリオ)

| 段階 | ルーティング(RIP/OSPF/BGP) | STP |
|---|---|---|
| 正常 | R1 が RN の LAN を学習 / リンク全部 NORMAL / 情 ROUTE ADD | リングのどこか1台が Blocking / MAC フラップが出ない |
| 障害 | R(N-1) の RN 向け IF を落とす → LINK DOWN(STATE+SYSLOG)/ ボード「異常」/ 経路が消える / 情 ROUTE DEL | R1 の R2 向け IF を落とす → LINK DOWN / Blocking 解除(迂回) |
| 復旧 | 戻す → 経路が戻る / LINK DOWN 解除 | 戻す → LINK DOWN 解除 / ふたたび Blocking |
| フラップ | 落とす/戻すを2回 → 注 WARN IF FLAP / ボードの syslog に「フラップ」 | — |

## 記録から気づいたこと(未修正)

- **Si-R の既定アドレスが全台同じ**: 新規の Si-R は `lan0 192.168.1.1/24` と `wan1 203.0.113.1/30` を持っていて、
  3台とも同じ。OSPF のルータIDがどれも `203.0.113.1` になる(`show ip ospf neighbor` で確認できる)。
  経路の学習や障害時の動作は正しかったが、実機ならルータID重複で問題になる構成。
- **Si-R の OSPF ログ**: リンクを落とした直後に、落ちた相手について `neighbor … state: Full` のログが1行出る
  (`sir-ospf.md` の障害段階のログ)。
- Si-R / SR-S の STP ログが Cisco 形式(`%SPANTREE-…`)と Si-R 形式で二重に出る。
