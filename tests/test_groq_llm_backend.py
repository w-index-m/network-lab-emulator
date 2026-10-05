"""
Groq APIをLLMバックエンドとして追加する(ユーザー依頼「groqなどのAPIを使う
アプローチで行ってみます」)。

既存のOllama連携(`app.py`の`detect_ollama`/`query_ollama`、起動時に1回
だけ自動判定して`USE_OLLAMA`を立てる仕組み)と同じ構造で、
`detect_groq`/`query_groq`を追加し、優先順位 Groq > Ollama > ルール
ベースで`LLM_BACKEND`("groq"/"ollama"/"rules")を決定する
(`app.py`のlifespan起動処理)。

Groq APIはこのサンドボックスの通信ポリシーで`api.groq.com`への
egressがブロックされている(CLAUDE.mdの`download.pytorch.org`/
`api.groq.com`と同じ扱い)ため、実際のGroq APIに繋がることまでは
このテストスイートでは検証できない——`detect_groq()`がAPIキー無し/
APIキーありだが接続失敗、の両方で例外を出さずFalseへ安全に
フォールバックすることをテストする(本番のRender環境は別の
egressポリシーなので実際に繋がる可能性が高いが、それはこの
サンドボックスから確認できない)。

`/api/status`のレスポンス形状(`mode`/`ollama_model`/`groq_model`)が
3値("groq"/"ollama"/"rules")を正しく返すことも固定する。
"""

import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module

client = TestClient(app_module.app)


class TestGroqDetectionFallback:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_false_without_network_call(self):
        old_key = app_module.GROQ_API_KEY
        try:
            app_module.GROQ_API_KEY = ''
            result = await app_module.detect_groq()
            assert result is False
        finally:
            app_module.GROQ_API_KEY = old_key

    @pytest.mark.asyncio
    async def test_invalid_key_falls_back_to_false_not_exception(self):
        """このサンドボックスはapi.groq.comへのegressがブロックされて
        いるため、鍵の有効性に関わらず接続自体が失敗する——その失敗を
        例外にせず安全にFalseへフォールバックすることを確認する。"""
        old_key = app_module.GROQ_API_KEY
        try:
            app_module.GROQ_API_KEY = 'definitely-not-a-real-key'
            result = await app_module.detect_groq()
            assert result is False
        finally:
            app_module.GROQ_API_KEY = old_key

    @pytest.mark.asyncio
    async def test_query_groq_returns_none_on_failure(self):
        old_key = app_module.GROQ_API_KEY
        try:
            app_module.GROQ_API_KEY = 'definitely-not-a-real-key'
            result = await app_module.query_groq('show version', 'system prompt')
            assert result is None
        finally:
            app_module.GROQ_API_KEY = old_key


class TestLlmBackendPriority:
    @pytest.mark.asyncio
    async def test_query_llm_dispatches_to_groq_when_backend_is_groq(self, monkeypatch):
        async def fake_query_groq(prompt, system):
            return 'from-groq'
        monkeypatch.setattr(app_module, 'query_groq', fake_query_groq)
        old_backend = app_module.LLM_BACKEND
        old_chain = app_module._LLM_CHAIN
        try:
            app_module.LLM_BACKEND = 'groq'
            app_module._LLM_CHAIN = [('groq', fake_query_groq)]
            result = await app_module.query_llm('show version', 'sys')
            assert result == 'from-groq'
        finally:
            app_module.LLM_BACKEND = old_backend
            app_module._LLM_CHAIN = old_chain

    @pytest.mark.asyncio
    async def test_query_llm_dispatches_to_ollama_when_backend_is_ollama(self, monkeypatch):
        async def fake_query_ollama(prompt, system):
            return 'from-ollama'
        monkeypatch.setattr(app_module, 'query_ollama', fake_query_ollama)
        old_backend = app_module.LLM_BACKEND
        old_chain = app_module._LLM_CHAIN
        try:
            app_module.LLM_BACKEND = 'ollama'
            app_module._LLM_CHAIN = [('ollama', fake_query_ollama)]
            result = await app_module.query_llm('show version', 'sys')
            assert result == 'from-ollama'
        finally:
            app_module._LLM_CHAIN = old_chain
            app_module.LLM_BACKEND = old_backend

    @pytest.mark.asyncio
    async def test_query_llm_returns_none_when_backend_is_rules(self):
        old_backend = app_module.LLM_BACKEND
        old_chain = app_module._LLM_CHAIN
        try:
            app_module.LLM_BACKEND = 'rules'
            app_module._LLM_CHAIN = []
            result = await app_module.query_llm('show version', 'sys')
            assert result is None
        finally:
            app_module.LLM_BACKEND = old_backend
            app_module._LLM_CHAIN = old_chain


class TestApiStatusShape:
    def test_status_reports_rules_mode_by_default(self):
        r = client.get('/api/status')
        data = r.json()
        assert data['mode'] in ('rules', 'ollama', 'groq')
        assert 'ollama_model' in data
        assert 'groq_model' in data

    def test_status_reports_groq_model_only_when_backend_is_groq(self, monkeypatch):
        old_backend = app_module.LLM_BACKEND
        try:
            app_module.LLM_BACKEND = 'groq'
            r = client.get('/api/status')
            data = r.json()
            assert data['mode'] == 'groq'
            assert data['groq_model'] == app_module.GROQ_MODEL
            assert data['ollama_model'] is None
        finally:
            app_module.LLM_BACKEND = old_backend

    def test_status_reports_mistral_model_only_when_backend_is_mistral(self):
        old_backend = app_module.LLM_BACKEND
        try:
            app_module.LLM_BACKEND = 'mistral'
            r = client.get('/api/status')
            data = r.json()
            assert data['mode'] == 'mistral'
            assert data['mistral_model'] == app_module.MISTRAL_MODEL
            assert data['groq_model'] is None
            assert data['ollama_model'] is None
        finally:
            app_module.LLM_BACKEND = old_backend


class TestMistralDetectionFallback:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_false_without_network_call(self):
        old_key = app_module.MISTRAL_API_KEY
        try:
            app_module.MISTRAL_API_KEY = ''
            result = await app_module.detect_mistral()
            assert result is False
        finally:
            app_module.MISTRAL_API_KEY = old_key

    @pytest.mark.asyncio
    async def test_query_mistral_returns_none_on_failure(self):
        old_key = app_module.MISTRAL_API_KEY
        try:
            app_module.MISTRAL_API_KEY = 'definitely-not-a-real-key'
            result = await app_module.query_mistral('show version', 'system prompt')
            assert result is None
        finally:
            app_module.MISTRAL_API_KEY = old_key


class TestQueryLlmFallbackChain:
    @pytest.mark.asyncio
    async def test_falls_through_to_mistral_when_groq_fails(self, monkeypatch):
        """GROQ_API_KEYがいっぱい(クォータ超過)等でGroqが空応答を返した
        場合、チェーンの次(Mistral)へ自動フォールバックすることを
        固定する(ユーザー依頼「GROQ_API_KEYがいっぱいの時にmistralに
        フォールバックする仕様にできる?」の実装)。"""
        async def fake_query_groq_fails(prompt, system):
            return None  # クォータ超過等で失敗
        async def fake_query_mistral_succeeds(prompt, system):
            return 'from-mistral-fallback'
        old_chain = app_module._LLM_CHAIN
        try:
            app_module._LLM_CHAIN = [
                ('groq', fake_query_groq_fails),
                ('mistral', fake_query_mistral_succeeds),
            ]
            result = await app_module.query_llm('show version', 'sys')
            assert result == 'from-mistral-fallback'
        finally:
            app_module._LLM_CHAIN = old_chain

    @pytest.mark.asyncio
    async def test_uses_groq_directly_when_it_succeeds(self, monkeypatch):
        async def fake_query_groq_succeeds(prompt, system):
            return 'from-groq'
        async def fake_query_mistral_should_not_be_called(prompt, system):
            raise AssertionError('mistral should not be reached when groq succeeds')
        old_chain = app_module._LLM_CHAIN
        try:
            app_module._LLM_CHAIN = [
                ('groq', fake_query_groq_succeeds),
                ('mistral', fake_query_mistral_should_not_be_called),
            ]
            result = await app_module.query_llm('show version', 'sys')
            assert result == 'from-groq'
        finally:
            app_module._LLM_CHAIN = old_chain

    @pytest.mark.asyncio
    async def test_returns_none_when_entire_chain_fails(self, monkeypatch):
        async def fake_fails(prompt, system):
            return None
        old_chain = app_module._LLM_CHAIN
        try:
            app_module._LLM_CHAIN = [('groq', fake_fails), ('mistral', fake_fails)]
            result = await app_module.query_llm('show version', 'sys')
            assert result is None
        finally:
            app_module._LLM_CHAIN = old_chain
