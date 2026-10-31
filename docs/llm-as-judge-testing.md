# LLM-as-judge型の試験自動化

## これは何か

このセッションで複数回提案していた改善項目のうち、未着手だった
「LLM-as-judge型の試験自動化」(既存のshow出力ベース検証テストに
LLM判定を足す薄いラッパー)をユーザー依頼により実装した。

新設`tools/llm_judge.py`の`judge(output, assertion, context="")`が、
CLI出力(`output`)が自然文の主張(`assertion`)を満たしているかを
LLMに判定させる。新しいLLM呼び出し経路は追加していない——
`tools/network_ontology_query.py`の`_chat_completion()`
(Ollama→Groq→Mistralの順にベストエフォートで問い合わせる既存の
ヘルパー)をそのまま再利用する(`tools/syslog_anomaly_detector.py`が
`summarize_logs_via_ollama`を直接importしているのと同じ
reuse-not-duplicateの方針)。

## スコープ: 既存の厳密な文字列検証を「置き換える」ものではない

このテストスイートの既存のshow出力検証(`assert 'FULL' in out`の
ような文字列in判定)は、確定的で速く、CI上で安定している——
これをLLM判定に置き換えるつもりはない。`judge()`は、文字列一致では
表現しづらい「意味的な正しさ」(例: 複数行のshow出力全体が、矛盾なく
期待した状態を表しているか)を**追加で**確認する薄いレイヤーとして
位置づける。既存テストへの組み込みは「既存のassertの後に追記する」
形を想定しており、既存のassertを削除・置換することはしない。

## フェイクの判定を作らない

このプロジェクト全体で繰り返し明文化されている方針
(`/api/generate_playbook`のdocstring「生成のみ行い、実行はしない
——フェイクの実行成功を作らない」等)をそのまま踏襲する:

- LLMバックエンドが一つも使えない場合(`_chat_completion()`が`None`
  を返す) → `JudgeResult(verdict=None, available=False)`
- LLM応答が得られたがJSON判定として解釈できない場合
  (システムプロンプトを無視した自由文での応答等)
  → `JudgeResult(verdict=None, available=True)`
- どちらの場合も`verdict`は`None`(「不明」)であり、**Falseには倒さない**
  (「判定できなかった」を「不合格だった」と混同しない)。
- pytest側のヘルパー`skip_if_unavailable(result)`が、`verdict is None`
  の場合に`pytest.skip()`する——Pass/Failを偽装せず、テストレポート上も
  「判定不能」であることが明示される。

## 判定プロンプトの形

```
system: あなたはネットワーク機器のCLI出力を検証するテストjudgeです。
        与えられた「CLI出力」が「判定したい主張」を満たしているかを
        意味的に判断し、{"verdict": true/false, "reasoning": "..."}
        のJSON形式のみで返してください。
user:   補足情報: device_type=cisco, command='show ip route'
        CLI出力: <show出力そのまま>
        判定したい主張: <自然文の主張>
```

応答は`app.py`の`_strip_markdown_fence()`と同じロジック(標準ライブラリ
のみの`tools/`配下スクリプトでapp.pyを直接importするのは重すぎるため、
この10行程度の小さなヘルパーだけ複製している)でMarkdownフェンスを
剥がした上で`json.loads()`する。

## 使い方

スタンドアロン(手動確認用):

```bash
python tools/llm_judge.py \
    --output "Neighbor 192.0.2.1  Full  00:00:38" \
    --assertion "OSPFネイバーがFull状態で確立している"
```

既存のpytestテストへの追加例(`tests/test_llm_judge.py`の
`TestWrapExistingShowOutputTest`に実装済み):

```python
from tools.llm_judge import judge, skip_if_unavailable

def test_static_route_reflected_in_show_ip_route():
    out = _cli(dev_id, 'show ip route')
    assert 'S' in out and '203.0.113.0/24' in out   # 既存の厳密な検証はそのまま残す

    result = judge(out, "203.0.113.0/24宛てのスタティックルートが、"
                         "ネクストホップ192.0.2.254経由として正しく反映されている",
                   context="device_type=cisco, command='show ip route'")
    skip_if_unavailable(result)
    assert result.verdict is True, result.reasoning
```

## このサンドボックスでのライブ検証について(正直な記載)

`api.groq.com`/`api.mistral.ai`/ローカルOllamaのいずれにもこの
サンドボックスから到達できない(CLAUDE.mdの「Groq API」節に既出の
egressポリシーと同じ制約)。そのため:

- `judge()`自体のロジック(JSON解析・Markdownフェンス除去・
  `verdict`がbool以外の場合の扱い・バックエンド全滅時の
  `verdict=None`フォールバック等)は、`_chat_completion`をmonkeypatchで
  モック化して検証した(他のLLM依存機能のテストと同じ既存パターン)。
- `tests/test_llm_judge.py::TestWrapExistingShowOutputTest`は実際に
  このサンドボックス上で実行し、`skip_if_unavailable`によって
  `pytest.skip`で安全にskipされることを確認した(フェイクのPASSに
  ならないことの確認)——実際のLLM応答を使った合否判定そのものは、
  Groq/Mistral/Ollamaのいずれかに到達できる環境(本番Render環境等)で
  改めて確認する必要がある。

## テスト

`tests/test_llm_judge.py`(11 tests)が、`judge()`のJSON解析/
Markdownフェンス除去/非bool verdictの扱い/バックエンド全滅時の
`verdict=None`フォールバック/プロンプトへのcontext反映、
`skip_if_unavailable`のskip挙動、既存show出力検証テストへの
実際の組み込み例(スタティックルート反映確認、スキップされることの
確認込み)、CLIエントリポイントの`UNAVAILABLE`終了コードを固定する。
