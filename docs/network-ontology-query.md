# ネットワーク版オントロジー質問応答（Fabric IQの発想を適用）

`tools/network_ontology_query.py`

## これは何か

Microsoft Fabric IQ（Fabric側で定義した業務語彙=オントロジーに、
Agentが自然言語で問い合わせる仕組み。自然言語→NL2Ontology→
Ontology(Entity Type/Property/Relationship)→Data Binding→OneLake、
という流れ）の発想を、このエミュレータの複数API
（トポロジー・LogicMonitorのAlert・Grafana Lokiの生ログ）に対して
適用したもの。

Fabric IQ本体（汎用グラフエンジン、KQL/GQLの自動振り分け等）を
再現するものではなく、「自然言語の質問を決まった形の構造化クエリに
変換してから、実データに振り分けて答える」という中核のアイデアだけを、
筋が通る最小限のエンティティに絞って実装した。

## オントロジー定義

```
Device
  - id, hostname, type
  - connects_to -> Device[]   （CDP/LLDPで発見したリンク。
                                 docs/topology-diagram.mdのAPIを使う）
  - raises_alert -> Alert[]   （LogicMonitorに登録されたDeviceのAlert）

Alert
  - id, severityLabel, resourceTemplateName, instanceName, detail
  - belongs_to -> Device
  - backed_by_logs -> LogLine[]  （Lokiに実際に届いた生のsyslog本文。
    Alert自体はログの中身を持たず、都度LogQLで問い合わせて取って
    くる = Fabric IQの"コピーせずにデータ源に紐づける"と同じ発想）
```

自然言語の質問は次の3種類の構造化クエリのいずれかに変換してから、
実際のAPIに振り分けて実行する:

```json
{"entity": "Alert", "filters": {"severityLabel": "critical"}}
{"entity": "Alert", "filters": {}, "backed_by_logs": true}
{"entity": "Device", "relationship": "connects_to", "of": "<device_id>"}
```

### Alert⇔ログの紐付け（LogicMonitorとLokiという別々のAPIをまたぐ）

`backed_by_logs`を指定すると、まずLogicMonitorのDevice一覧を引いて
`deviceId`から`name`（実機のLogicMonitorの慣習でここに監視対象IPを
入れる）を求め、そのIPと`{job="netlab-syslog", source_ip="<ip>"}`と
いうLogQLでLokiに問い合わせ、Alert発生時刻（`startEpoch`）の前後
（既定: 前120秒〜後60秒）のログを取ってくる。LogicMonitorのAlertと
Lokiの生ログという**別々の製品のAPI**を、装置のIPという共通の鍵で
オントロジー的につなぐ部分。

## NL2Ontology（自然言語→構造化クエリ）

Ollamaがあれば使い、無ければキーワードベースの最小フォールバックに
落ちる（`docs/ai-grafana-autopilot.md`と同じ設計方針）。

```python
def nl_to_query(question: str) -> tuple[dict | None, str]:
    q = nl_to_query_via_ollama(question)
    if q:
        return q, 'ollama'
    q = nl_to_query_by_keyword(question)
    if q:
        return q, 'keyword-fallback'
    return None, 'none'
```

このサンドボックス環境ではOllamaは動いていないため、実際の動作確認は
すべてキーワードフォールバック経由（`method: 'keyword-fallback'`）で
行った。Ollamaが使える環境では`OLLAMA_URL`/`OLLAMA_MODEL`環境変数で
接続先を指定すれば、プロンプトで渡したオントロジー定義に沿って
より柔軟な質問にも対応できるはずだが、これは未検証。

### 見つかった不具合（実装中）

Pythonの正規表現`\w`は既定でUnicode文字（ひらがな等）にもマッチする。
装置IDを抜き出す正規表現を`[\w.\-]+`にしたところ、
「core-cはどこに繋がっている？」という質問で
`core-cはどこに`という助詞込みの文字列を装置IDとして誤抽出して
しまった。装置IDはASCII英数字・ハイフン・ドットだけに限定する
（`[A-Za-z0-9.\-]+`）ことで修正し、回帰テストで固定した。

## 実際に動かして確認した結果

エミュレータを実際に起動し、3台の装置（Onto-A/Onto-B/Onto-Core）を
リンクさせ、Onto-AをLogicMonitorに登録した上でインタフェースを
`shutdown`して実際にAlertを発生させ、3種類の質問を投げた:

```
$ python tools/network_ontology_query.py --emulator-url http://127.0.0.1:8096 "アラートが出ている装置は？"
[NL2Ontology: keyword-fallback] {"entity": "Alert", "filters": {}}
2件のAlertが見つかりました:
  - [critical] Onto-A: Interface Status/GigabitEthernet1/0/1 (GigabitEthernet1/0/1 is down (expected up))
  - [critical] Onto-A: Interface Status/GigabitEthernet1/0/3 (GigabitEthernet1/0/3 is err-disabled (expected up))

$ python tools/network_ontology_query.py --emulator-url http://127.0.0.1:8096 "重大度がcriticalなアラートは？"
[NL2Ontology: keyword-fallback] {"entity": "Alert", "filters": {"severityLabel": "critical"}}
2件のAlertが見つかりました: (同上)

$ python tools/network_ontology_query.py --emulator-url http://127.0.0.1:8096 "onto-cはどこに繋がっている？"
[NL2Ontology: keyword-fallback] {"entity": "Device", "relationship": "connects_to", "of": "onto-c"}
2台に接続しています:
  - onto-b (Onto-B, catalyst)
  - onto-a (Onto-A, catalyst)
```

いずれも実際に発生させたAlert・実際に張ったリンクの通りに正しく
答えられていることを確認した。

### `backed_by_logs` — 本物のLokiサーバーで確認

`docs/loki-setup.md`で検証済みのLoki本体（`loki-linux-amd64`）を
実際に起動し、`tools/syslog_to_loki.py`のブリッジも実際に動かした
状態で、装置のインタフェースを`shutdown`して**本物のsyslog UDP
パケット**を発生させ、Lokiに実際に取り込ませた上で質問した:

```
$ python tools/network_ontology_query.py --emulator-url http://127.0.0.1:8097 \
    --loki-url http://localhost:3100 "アラートの根拠となるログは？"
[NL2Ontology: keyword-fallback] {"entity": "Alert", "filters": {}, "backed_by_logs": true}
2件のAlertが見つかりました:
  - [critical] Onto-Log: Interface Status/GigabitEthernet1/0/1 (GigabitEthernet1/0/1 is down (expected up))
      ログ: Sep 19 07:58:25 Onto-Log %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down
  - [critical] Onto-Log: Interface Status/GigabitEthernet1/0/3 (GigabitEthernet1/0/3 is err-disabled (expected up))
      ログ: Sep 19 07:58:25 Onto-Log %LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to down
```

LogicMonitorのAlert（インタフェース状態から動的に組み立てたもの）と、
Lokiに実際に届いた生のsyslog本文が、装置のIP（`127.0.0.1`）を鍵に
正しく紐付いていることを確認した。

## 使い方

```bash
python tools/network_ontology_query.py --emulator-url http://localhost:8000 \
    "アラートが出ている装置は？"

# Loki連携あり
python tools/network_ontology_query.py --emulator-url http://localhost:8000 \
    --loki-url http://localhost:3100 "アラートの根拠となるログは？"
```

## テスト

```bash
pytest tests/test_network_ontology_query.py -v
# 16/16 成功
```

固定している内容：
1. キーワードフォールバックが正しい構造化クエリを組み立てること
   （critical/warning/汎用アラート、`connects_to`関係、`backed_by_logs`）
2. **装置IDの抽出がASCII文字だけに限定され、助詞を飲み込まないこと**
   （見つけた不具合の回帰テスト）
3. 認識できない質問は`None`を返すこと
4. `resolve_query`が実際のLogicMonitor Alert API/トポロジーAPIから
   正しい結果を取得すること（TestClientをurllib越しに叩く形で検証）
5. Alert/Device双方について、結果が空の場合と不明なエンティティの
   場合の文言
6. `loki_query_range`が正しいLogQL（`source_ip`ラベル）でLokiに
   問い合わせ、ログ本文だけを取り出せること（Loki互換モックサーバー）
7. `backed_by_logs: true`のときだけAlertに`logs`が付き、
   `--loki-url`を指定しなければLokiには一切問い合わせないこと
   （このエミュレータ以外の外部URLを勝手に叩かないことの固定）

## 制約・今後の拡張余地

- エンティティはDevice/Alertの2種類のみ（+Alertに付随するログ）。
  Vulnerability（Nexpose）やVNI（EVPN）等への拡張は今回のスコープ外
- Ollama経由のNL2Ontologyはこの環境で実行できず未検証（プロンプトの
  設計のみ）。フォールバックのキーワードマッチも「アラート」
  「繋がっている」「ログ/根拠/証拠」等ごく限られたパターンしか
  認識しない
- グラフの多段の辿り（「装置Xに繋がっている装置のAlertは？」のような
  複合クエリ）には対応していない。Fabric IQのような汎用グラフ
  エンジンではなく、3種類の固定クエリ形しか扱えない
- `backed_by_logs`の時間窓（前120秒〜後60秒）は固定値で、
  質問から動的に調整することはできない
- Lokiのログ本文自体は解釈しない（キーワード検索や要約はしない、
  単に時間窓内の生ログをそのまま返すだけ）
- 結果の自然言語での要約（Fabric IQでいう回答生成部分）はテンプレート
  ベースの簡易な文言で、Ollamaによる要約はしていない
