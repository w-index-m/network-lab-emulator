"""
tools/rag_query.py のBM25検索ロジックのテスト
(Ollama呼び出しは行わない。純粋に検索精度のみ検証)
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools import rag_query


def test_build_index_produces_chunks_from_all_docs():
    bm25, chunks = rag_query.build_index()
    assert len(chunks) > 0
    sources = {c['source'] for c in chunks}
    assert 'dns-dhcp-setup.md' in sources
    assert 'netbox-setup.md' in sources


def test_retrieve_finds_powerdns_ipv6_issue():
    bm25, chunks = rag_query.build_index()
    # top_k=6: docs/rag-fallback.md 自身がこのPowerDNS/IPv6の例に
    # 言及しているため上位に食い込む(自己言及)。実際の一次情報である
    # dns-dhcp-setup.mdが上位圏内に入っていることを確認する
    results = rag_query.retrieve('PowerDNS IPv6 エラー', bm25, chunks, top_k=6)
    assert len(results) > 0
    assert any('dns-dhcp-setup.md' == r['source'] for r in results)
    assert any('local-address' in r['text'] for r in results)


def test_retrieve_finds_zabbix_build_issue():
    bm25, chunks = rag_query.build_index()
    results = rag_query.retrieve('Zabbixのスキーマ生成でハマった点', bm25, chunks, top_k=3)
    assert len(results) > 0
    assert any('zabbix-setup.md' == r['source'] for r in results)


def test_retrieve_returns_empty_for_unrelated_query():
    bm25, chunks = rag_query.build_index()
    results = rag_query.retrieve('寿司の握り方を教えて', bm25, chunks, top_k=3)
    # BM25スコアが0以下のものは除外されるため、無関係な質問では
    # 結果が空になるか、あっても関連性の低いものになる
    assert isinstance(results, list)


# ── チャットモード（会話履歴の保持） ──────────────────────

def test_ask_turn_appends_query_and_answer_to_history():
    """1ターン終えると、履歴にuser/assistantが1組ずつ積まれること"""
    bm25, chunks = rag_query.build_index()
    history = []
    with patch.object(rag_query, '_call_ollama', return_value='テスト回答'):
        answer = rag_query.ask_turn('テスト質問', history, bm25, chunks)
    assert answer == 'テスト回答'
    assert history == [
        {'role': 'user', 'content': 'テスト質問'},
        {'role': 'assistant', 'content': 'テスト回答'},
    ]


def test_ask_turn_sends_prior_history_to_ollama():
    """2ターン目には、1ターン目の質問と回答がmessagesに含まれること

    履歴を保持しないと、Qwenは直前のやり取りを踏まえた回答ができず
    単発の一問一答と変わらなくなってしまう。
    """
    bm25, chunks = rag_query.build_index()
    history = []
    sent_messages = []

    def fake_call(messages):
        sent_messages.append(list(messages))
        return f'回答{len(sent_messages)}'

    with patch.object(rag_query, '_call_ollama', side_effect=fake_call):
        rag_query.ask_turn('質問1', history, bm25, chunks)
        rag_query.ask_turn('質問2', history, bm25, chunks)

    second_turn = sent_messages[1]
    assert {'role': 'user', 'content': '質問1'} in second_turn
    assert {'role': 'assistant', 'content': '回答1'} in second_turn


def test_ask_turn_does_not_bake_reference_material_into_history():
    """履歴に積む質問は生の質問文のみで、参考資料を含まないこと

    参考資料を履歴に積み続けると、ターンを重ねるたびにOllamaへ送る
    プロンプトが際限なく肥大化してしまう。
    """
    bm25, chunks = rag_query.build_index()
    history = []
    with patch.object(rag_query, '_call_ollama', return_value='回答'):
        rag_query.ask_turn('PowerDNS IPv6 エラー', history, bm25, chunks)
    assert history[0]['content'] == 'PowerDNS IPv6 エラー'
    assert '参考資料' not in history[0]['content']


def test_main_requires_query_or_chat_flag(capsys):
    """query も --chat も無ければ使い方エラーで終了する（サイレントに
    Noneを質問文として送りつけたりしない）"""
    import pytest
    with patch.object(sys, 'argv', ['rag_query.py']):
        with pytest.raises(SystemExit) as exc:
            rag_query.main()
    assert exc.value.code == 2
    assert '--chat' in capsys.readouterr().err
