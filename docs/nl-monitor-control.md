# 自然言語 監視対象コントロールツール

`tools/nl_monitor_control.py`

## これは何か

「192.168.1.1のGi1/1を監視対象に追加して」のような自然言語の指示を
解析し、IPアドレスから仮想ラボの実装置を特定した上で、監視対象リスト
（watchlist）に追加/削除する。

```
自然言語コマンド
   │
   ▼
NLExtractor（Ollamaがあれば使用、無ければ正規表現ベースにフォールバック）
   │  → {ip, interface, action}
   ▼
IPアドレスから装置を特定
   │  各装置に `show ip interface brief` を実投入して照合
   ▼
watchlist（tools/monitor_watchlist.json）に反映
```

## 使い方

```bash
# 追加
python tools/nl_monitor_control.py "192.168.1.1のGi1/1を監視対象に追加して"

# 削除
python tools/nl_monitor_control.py "10.4.1.1のGigabitEthernet1/0/1を監視対象から外して"

# 現在の監視対象一覧
python tools/nl_monitor_control.py --list

# Ollamaを使わず正規表現ベースのみで解析
python tools/nl_monitor_control.py --no-ai "..."
```

## 実機統合確認（このリポジトリ内でのテスト）

実際にエミュレーターを起動し、`catalyst`に`10.9.9.1`をIP設定した状態で:

```
$ python tools/nl_monitor_control.py "10.9.9.1のgi1/0/1を監視対象に追加して"
💬 指示: "10.9.9.1のgi1/0/1を監視対象に追加して"
🔍 解析結果: IP=10.9.9.1, Interface=gi1/0/1, Action=add
✅ 装置特定: catalyst（10.9.9.1 は GigabitEthernet1/0/1）
✅ 監視対象に追加しました: catalyst / GigabitEthernet1/0/1 / 10.9.9.1

$ python tools/nl_monitor_control.py "10.9.9.1のgi1/0/1を監視対象に追加して"  # 再実行
ℹ️  既に監視対象に登録済みです: catalyst / GigabitEthernet1/0/1

$ python tools/nl_monitor_control.py "10.9.9.1のgi1/0/1を監視対象から外して"
✅ 監視対象から削除しました: catalyst / GigabitEthernet1/0/1
```

短縮形（`gi1/0/1`）と正式名（`GigabitEthernet1/0/1`）が同一インターフェースとして
正しく認識され、重複登録も防げることを確認済み。

## 実装過程で見つけて直したバグ

- **`\b`（正規表現の単語境界）が日本語直後で機能しないバグ**:
  Pythonの`re`はUnicode文字を`\w`として扱うため、「192.168.10.1のVlan10」の
  ように数字の直後に日本語（の）が続くと、`\b`が境界として成立せず
  IPアドレス抽出が失敗していた（レビューで発見）。`\b`を除去して修正。
- ログメッセージの「IPは〜」表記が実際にはインターフェース名を指していた
  ラベル間違いも修正。

## 制約

- **仮想ラボの装置のみ対象**。実機（本当の192.168.1.1等）を監視したい場合は、
  この仕組みではなく実SNMPポーリング（`docs/prometheus-grafana-windows.md`の
  実機向け構成）が必要
- IPアドレスが複数装置で重複している場合、`show ip interface brief`を
  照合した最初の装置がヒットする（曖昧性の解消はしない）
- watchlist自体は現状、他のツール（`ai_grafana_autopilot.py`等）から
  自動で参照される仕組みにはなっていない（今後の拡張ポイント）

## テスト

```bash
pytest tests/test_nl_monitor_control.py -v
# 9/9 成功
```
