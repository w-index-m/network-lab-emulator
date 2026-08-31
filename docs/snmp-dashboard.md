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
  sysDescr・インターフェース別のUp/Down・トラフィックレート
- 更新間隔（2秒〜30秒）と装置名フィルタをUIから変更可能

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
# 4/4 成功
```

実サーバーを起動し、`r1` の `GigabitEthernet0/0/0` を `shutdown` →
ダッシュボードのカードが赤い ATTENTION バッジに変わることをスクリーン
ショットで確認済み。`no shutdown` で HEALTHY に復帰することも確認済み。

## 制約

- ブラウザで開いて使う想定（Claude Artifactのような外部ホスティング環境
  からは、このローカルサーバーに到達できないため使えない）
- MIB-IIのsystem/interfacesグループのみ（ルーティングテーブル等は未対応）
