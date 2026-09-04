# SNMP モニタリングダッシュボード

`static/snmp_dashboard.html` + `GET /api/snmp/dashboard`

## これは何か

仮想空間内の全装置を、実装済みの `engine.protocols.SnmpAgent`（MIB-II相当:
system / interfaces グループ）経由でポーリングし、モダンなダークテーマの
ダッシュボードで可視化する。装置カード・ステータスバッジ・インターフェース
一覧・トラフィックレートを一定間隔で自動更新する。

実UDP SNMPパケットではなく、`SnmpAgent._build_mib()` が持つ内部データを
直接読む「仮想SNMPポーリング」。CLIの `snmpget`/`snmpwalk`（`tools`側）が
使うのと同じデータソース。

## 使い方

```bash
python app.py
# ブラウザで開く:
#   http://localhost:8000/static/snmp_dashboard.html
```

ログインが必要な構成（`NETLAB_AUTH_DISABLE`未設定）の場合、`index.html`で
先にログインしていれば同一originのlocalStorage（`netlabToken_v1`）から
トークンを自動取得する。個別にトークンを渡す場合は
`?token=<セッショントークン>` をURLに付与する。

## 表示内容

- 上部サマリー: 装置数 / インターフェース総数 / Up数 / Down数 / 累計トラフィック
- 装置カード: hostname・種別バッジ・HEALTHY/ATTENTIONバッジ・Uptime・
  sysDescr・**CPU使用率の遷移グラフ**（Cisco系機種のみ）・
  **トラフィックの遷移グラフ**・インターフェース別のUp/Down・トラフィックレート
- 更新間隔（2秒〜30秒）と装置名フィルタをUIから変更可能

## CPU使用率（CISCO-PROCESS-MIB）

`cisco` / `catalyst` / `nexus` / `asa` の4機種は `cpmCPUTotalTable`
（OID `1.3.6.1.4.1.9.9.109.1.1.1.1.{3,4,7}.1`、5秒/1分/5分平均）相当の
CPU使用率を持つ。値は緩やかなランダムウォーク（±4%/ポーリング、2〜95%に
クランプ）で遷移し、ダッシュボードに緑/黄/赤のスパークライン付きで表示される
（80%以上で赤、60%以上で黄）。

Si-R / SR-S / APRESIA はCISCO-PROCESS-MIB非対応のため、この項目自体が
出ない（実機と同じ挙動）。

`/api/snmp/dashboard` は直近60件のサンプル（CPU・インターフェース合計
トラフィック）をサーバー側で保持し、レスポンスの `history` 配列として返す。
ポーリングのたびに1件追記されるため、ダッシュボードを開いたまま数分待つと
遷移グラフが育っていく。

## 実装で見つけて直した点

`SnmpAgent._build_mib()` の `ifOperStatus`/`ifAdminStatus` は、従来は
インターフェースにIPが設定されているかどうかだけで up/down を判定しており、
CLIで `shutdown` してもSNMP側は「up」のままになるバグがあった
（レビューで発見）。`vnet.down_interfaces` を参照するよう修正し、
`shutdown`/`no shutdown` が正しくダッシュボードに反映されるようにした
（`snmpset` による `ifAdminStatus` 上書きは従来通り優先される）。

## テスト

```bash
pytest tests/test_snmp_dashboard.py -v
# 7/7 成功
```

実サーバーを起動し、`r1` の `GigabitEthernet0/0/0` を `shutdown` →
ダッシュボードのカードが赤い ATTENTION バッジに変わることをスクリーン
ショットで確認済み。`no shutdown` で HEALTHY に復帰することも確認済み。

## 制約

- ブラウザで開いて使う想定（Claude Artifactのような外部ホスティング環境
  からは、このローカルサーバーに到達できないため使えない）
- MIB-IIのsystem/interfacesグループ + CISCO-PROCESS-MIBのCPU部分のみ対応。
  entPhysicalTable（シャーシ構成）やCISCO-MEMORY-POOL-MIB（メモリ）、
  CISCO-CDP-MIB等の他のCisco拡張MIBは未実装
- history は `/api/snmp/dashboard` の呼び出しごとにサーバーのメモリ内
  （プロセス再起動で消える）に保持。永続化はしていない
