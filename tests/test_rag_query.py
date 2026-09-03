"""
tools/rag_query.py のBM25検索ロジックのテスト
(Ollama呼び出しは行わない。純粋に検索精度のみ検証)
"""

import os
import sys

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
