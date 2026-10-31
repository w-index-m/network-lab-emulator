#!/usr/bin/env python3
"""
LLM-as-judge型の試験補助ラッパー。

既存のshow出力ベース検証テスト(文字列in判定)はそのままに、
「厳密な文字列一致では表現しづらい意味的な正しさ」をLLMに判定させる
薄いラッパーを追加する。新しいLLM呼び出し経路は作らず、
`tools/network_ontology_query.py`の`_chat_completion()`
(Ollama→Groq→Mistralの順にベストエフォートで問い合わせる既存のヘルパー)
をそのまま再利用する(`tools/syslog_anomaly_detector.py`が
`summarize_logs_via_ollama`を直接importしているのと同じ
reuse-not-duplicateの方針)。

**フェイクの判定を作らない**: LLMバックエンドが一つも使えない場合は
`verdict=None`(「不明」)を返す。呼び出し側(pytest)はこれを
`pytest.skip`に倒すべきで、TrueにもFalseにも倒してはならない
——これはこのプロジェクト全体で繰り返し明文化されている
「生成/判定のみ行い、フェイクの成功を作らない」という方針
(`/api/generate_playbook`のdocstring等)をそのまま踏襲する。

使い方(スタンドアロン):
    python tools/llm_judge.py \
        --output "Neighbor 192.0.2.1  Full  00:00:38" \
        --assertion "OSPFネイバーがFull状態で確立している"

使い方(pytestから、既存のshow出力ベース検証テストへの薄い追加として):
    from tools.llm_judge import judge, skip_if_unavailable

    def test_ospf_neighbor_full_llm_judge():
        out = _cli(dev_id, 'show ip ospf neighbor')
        assert 'FULL' in out  # 既存の厳密な文字列検証はそのまま残す
        result = judge(out, "OSPFネイバーがFull/DR-Otherなど正常な"
                             "確立状態で、Down/ExStart等の異常状態ではない")
        skip_if_unavailable(result)
        assert result.verdict is True, result.reasoning
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.network_ontology_query import _chat_completion  # noqa: E402


@dataclass
class JudgeResult:
    """LLM判定の結果。

    verdict: True/False(LLMが実際に判定した)、None(LLMバックエンドが
        一つも使えなかった、またはLLM応答を判定として解釈できなかった
        ——どちらの場合も「不明」であり、Falseとして扱ってはならない)。
    reasoning: 判定理由(人間向けの短い説明文)。
    available: LLMバックエンドから何らかの応答が得られたかどうか。
        False の場合、verdict は常に None。
    raw_response: LLMの生応答(デバッグ用。応答が無かった場合は空文字)。
    """
    verdict: Optional[bool]
    reasoning: str
    available: bool
    raw_response: str = ""


def _strip_markdown_fence(text: str) -> str:
    """app.pyの_strip_markdown_fenceと同じロジック(標準ライブラリのみの
    tools/配下スクリプトでapp.pyをimportするのは重すぎるため、この小さな
    ヘルパーだけ複製する——他のtools/*.pyスクリプトも同じ理由でapp.pyを
    importしていない)。"""
    text = text.strip()
    if not text.startswith('```'):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith('```'):
        lines = lines[:-1]
    return '\n'.join(lines).strip()


_SYSTEM_PROMPT = (
    "あなたはネットワーク機器のCLI出力を検証するテスト judge です。\n"
    "与えられた「CLI出力」が「判定したい主張」を満たしているかどうかを、"
    "厳密な文字列一致ではなく意味的に判断してください。\n"
    "出力は必ず次のJSON形式のみ(他の文章やMarkdownのコードフェンスを"
    "一切付けずに)で返してください:\n"
    '{"verdict": true または false, "reasoning": "判定理由を1〜2文で"}\n'
    "主張が満たされていればverdictはtrue、満たされていない・矛盾する・"
    "判断できない場合はfalseにしてください。"
)


def judge(output: str, assertion: str, context: str = "",
          temperature: float = 0.1, timeout: float = 15.0) -> JudgeResult:
    """`output`(CLI出力など)が`assertion`(自然文の主張)を満たしているかを
    LLMに判定させる。`context`は任意の補足情報(装置種別・コマンド名等)。

    LLMバックエンドが一つも使えない、または応答がJSONとして解釈できない
    場合は`verdict=None`/`available=False`相当の結果を返す
    (フェイクの判定を作らない)。
    """
    user_prompt = (
        (f"補足情報: {context}\n\n" if context else "")
        + f"CLI出力:\n{output}\n\n"
        + f"判定したい主張: {assertion}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    raw = _chat_completion(messages, temperature=temperature, timeout=timeout)
    if raw is None:
        return JudgeResult(verdict=None, reasoning="LLMバックエンド(Ollama/Groq/Mistral)が一つも利用できません",
                            available=False, raw_response="")

    cleaned = _strip_markdown_fence(raw)
    try:
        parsed = json.loads(cleaned)
        verdict = parsed.get("verdict")
        if not isinstance(verdict, bool):
            raise ValueError("verdict is not a bool")
        reasoning = str(parsed.get("reasoning", "")).strip()
        return JudgeResult(verdict=verdict, reasoning=reasoning, available=True, raw_response=raw)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return JudgeResult(
            verdict=None,
            reasoning=f"LLM応答をJSON判定として解釈できませんでした: {cleaned[:200]!r}",
            available=True,
            raw_response=raw,
        )


def skip_if_unavailable(result: JudgeResult) -> None:
    """pytestから使うヘルパー。LLM判定が使えなかった場合にskipする
    (importはここでのみ行い、pytest非依存のスタンドアロン利用を妨げない)。"""
    if result.verdict is None:
        import pytest
        pytest.skip(result.reasoning)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True, help="検証対象のCLI出力")
    ap.add_argument("--assertion", required=True, help="判定したい主張(自然文)")
    ap.add_argument("--context", default="", help="補足情報(装置種別・コマンド名等)")
    args = ap.parse_args()

    result = judge(args.output, args.assertion, context=args.context)
    if not result.available:
        print("UNAVAILABLE:", result.reasoning)
        return 2
    print("VERDICT:", result.verdict)
    print("REASONING:", result.reasoning)
    return 0 if result.verdict else 1


if __name__ == "__main__":
    sys.exit(main())
