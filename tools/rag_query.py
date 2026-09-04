#!/usr/bin/env python3
"""
RAG（検索拡張生成） — docs/*.md を検索対象にしたQwen問い合わせツール

ファインチューニングの代替として実装。Hugging Face（埋め込みモデルの
配布元）がこの環境ではブロックされているため、ニューラル埋め込みでは
なく BM25（キーワードベースの検索アルゴリズム、外部モデル不要）で
関連ドキュメントを検索し、その内容をQwenへのプロンプトに埋め込んで
回答させる。

学習は一切行わない。docs/*.md の内容そのものを都度「参考資料」として
渡すことで、Qwenが実際にこのプロジェクトで起きた固有の出来事
（ハマったバグ、設定値等）を踏まえた回答をできるようにする。

使い方:
  python tools/rag_query.py "PowerDNSでIPv6のエラーが出たときの対処法は？"
  python tools/rag_query.py "Zabbixのビルドでハマった点は？" --show-context

  # チャット形式（会話履歴を保持して連続で相談する）
  python tools/rag_query.py --chat
"""

import argparse
import glob
import os
import re
import sys

import httpx
from rank_bm25 import BM25Okapi

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-netops")
DOCS_GLOB = os.path.join(os.path.dirname(__file__), '..', 'docs', '*.md')

_TOKEN_RE = re.compile(r'[a-zA-Z0-9_./#+-]+|[぀-ヿ㐀-鿿]+')


def _tokenize(text: str):
    """英数字/記号は単語単位、日本語は文字単位でトークン化する簡易分割
    (BM25は形態素解析なしでも日本語混じりの技術文書に対して十分機能する)"""
    tokens = []
    for m in _TOKEN_RE.findall(text.lower()):
        if re.match(r'[ぁ-んァ-ヶ一-龠]+', m):
            tokens.extend(list(m))
        else:
            tokens.append(m)
    return tokens


def _split_into_chunks(path: str, max_chars: int = 800):
    """Markdownの見出し(##)単位でチャンク分割する"""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    title = os.path.basename(path)
    sections = re.split(r'\n(?=##\s)', text)
    chunks = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        # 長すぎるセクションはさらに分割
        for i in range(0, len(sec), max_chars):
            chunk = sec[i:i + max_chars]
            chunks.append({'source': title, 'text': chunk})
    return chunks


def build_index():
    chunks = []
    for path in sorted(glob.glob(DOCS_GLOB)):
        chunks.extend(_split_into_chunks(path))
    corpus_tokens = [_tokenize(c['text']) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)
    return bm25, chunks


def retrieve(query: str, bm25, chunks, top_k: int = 4):
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    results = []
    for i in ranked[:top_k]:
        if scores[i] <= 0:
            continue
        results.append({**chunks[i], 'score': scores[i]})
    return results


RAG_SYSTEM = """あなたはこのネットワークラボプロジェクトの技術サポートAIです。
以下の「参考資料」は、実際にこのプロジェクトで作業した際の記録
（実機確認結果・つまずいた点・設定値など）です。

回答時のルール:
1. 参考資料に書かれている固有の内容（エラーメッセージ、設定値、
   バージョン番号、ハマった原因等）があれば、それを優先して使う
2. 参考資料に無い一般論で答えるときは、その旨を明示する
3. 簡潔に、箇条書き中心で答える
"""


def _build_context(query: str, bm25, chunks, top_k: int, show_context: bool) -> str:
    results = retrieve(query, bm25, chunks, top_k=top_k)

    if not results:
        context = "(関連する参考資料が見つかりませんでした)"
    else:
        context = "\n\n---\n\n".join(
            f"[出典: {r['source']} / score={r['score']:.1f}]\n{r['text']}" for r in results
        )

    if show_context:
        print("=" * 70)
        print("検索でヒットした参考資料:")
        print("=" * 70)
        print(context)
        print("=" * 70)

    return context


def _call_ollama(messages: list) -> str:
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json={
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1},
    }, timeout=180.0)
    r.raise_for_status()
    return r.json()["message"]["content"]


def ask(query: str, top_k: int = 4, show_context: bool = False) -> str:
    """1問1答（履歴なし）。従来どおりの挙動。"""
    bm25, chunks = build_index()
    context = _build_context(query, bm25, chunks, top_k, show_context)
    prompt = f"参考資料:\n{context}\n\n質問: {query}"
    return _call_ollama([
        {"role": "system", "content": RAG_SYSTEM},
        {"role": "user", "content": prompt},
    ])


def ask_turn(query: str, history: list, bm25, chunks,
             top_k: int = 4, show_context: bool = False) -> str:
    """チャット用の1ターン。過去の会話履歴(history)を踏まえて回答する。

    検索は毎ターンその質問文だけで行う（履歴全体で検索すると、
    過去の話題に引きずられて直近の質問と無関係な資料を拾いやすい
    ため）。参考資料は当該ターンの質問にだけ埋め込み、履歴には
    生の質問と回答だけを積む。
    """
    context = _build_context(query, bm25, chunks, top_k, show_context)
    prompt = f"参考資料:\n{context}\n\n質問: {query}"
    messages = [{"role": "system", "content": RAG_SYSTEM}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    answer = _call_ollama(messages)
    # 履歴には参考資料を含めない生の質問文だけ積む（次ターンの検索や
    # コンテキストが肥大化しないよう、埋め込んだ参考資料は使い捨てる）
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": answer})
    return answer


def run_chat(top_k: int = 4, show_context: bool = False):
    """会話履歴を保持しながら連続で質問できる対話モード。"""
    print("RAGチャット（docs/*.md検索拡張・Qwen）")
    print("終了するには exit / quit / Ctrl-D")
    print("=" * 70)
    bm25, chunks = build_index()
    history = []
    while True:
        try:
            query = input("\nあなた> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in ("exit", "quit"):
            break
        try:
            answer = ask_turn(query, history, bm25, chunks,
                              top_k=top_k, show_context=show_context)
        except httpx.HTTPError as e:
            print(f"[エラー] Ollamaへの問い合わせに失敗しました: {e}")
            continue
        print(f"\nQwen> {answer}")


def main():
    parser = argparse.ArgumentParser(description='RAG（docs/*.md検索拡張）でQwenに質問する')
    parser.add_argument('query', nargs='?', help='質問文（--chat指定時は不要）')
    parser.add_argument('--top-k', type=int, default=4)
    parser.add_argument('--show-context', action='store_true', help='検索結果も表示する')
    parser.add_argument('--chat', action='store_true',
                        help='会話履歴を保持する対話モードで起動する')
    args = parser.parse_args()

    if args.chat:
        run_chat(top_k=args.top_k, show_context=args.show_context)
        return

    if not args.query:
        parser.error('質問文を指定するか --chat を付けてください')

    answer = ask(args.query, top_k=args.top_k, show_context=args.show_context)
    print('\n=== Qwen の回答 ===')
    print(answer)


if __name__ == '__main__':
    sys.exit(main())
