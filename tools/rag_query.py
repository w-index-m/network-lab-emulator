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


def ask(query: str, top_k: int = 4, show_context: bool = False) -> str:
    bm25, chunks = build_index()
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

    prompt = f"参考資料:\n{context}\n\n質問: {query}"

    r = httpx.post(f"{OLLAMA_URL}/api/chat", json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": RAG_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }, timeout=180.0)
    r.raise_for_status()
    return r.json()["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description='RAG（docs/*.md検索拡張）でQwenに質問する')
    parser.add_argument('query', help='質問文')
    parser.add_argument('--top-k', type=int, default=4)
    parser.add_argument('--show-context', action='store_true', help='検索結果も表示する')
    args = parser.parse_args()

    answer = ask(args.query, top_k=args.top_k, show_context=args.show_context)
    print('\n=== Qwen の回答 ===')
    print(answer)


if __name__ == '__main__':
    sys.exit(main())
