"""
`tools/llm_judge.py`(LLM-as-judge型の試験補助ラッパー)の回帰テスト。

ユーザー依頼「LLM-as-judge型の試験自動化 — 既存show出力ベース検証
テストへのLLM判定ラッパー追加」への対応。

1. `judge()`自体のロジックを、他のLLM依存機能のテスト
   (`tests/test_generate_playbook_endpoint.py`等)と同じ
   monkeypatchパターンで検証する(`_chat_completion`をモック化——
   このサンドボックスからは実際のGroq/Mistral/Ollama APIに到達できない
   ため、実LLM呼び出しそのものはここでは検証しない。これは既存の
   Groq/Strixフォールバック等と同じ、プロジェクト全体で確立済みの
   方針)。
2. `TestWrapExistingShowOutputTest`で、既存のshow出力ベース検証テスト
   (`tests/test_ospf_neighbor_fidelity.py`相当のOSPFネイバー確立確認)に
   実際にLLM判定を薄く追加する例を示す——このサンドボックスでは
   LLMバックエンドが無いため`skip_if_unavailable`で安全にskipされる
   ことまでを固定する(フェイクの判定を作らないことの確認)。
"""

import json
import os
import sys

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from fastapi.testclient import TestClient

import app as app_module
import tools.llm_judge as llm_judge

client = TestClient(app_module.app)


def _cli(device_id, cmd):
    return client.post('/api/cli', json={'device_id': device_id, 'command': cmd}).json()['output']


def _dev(id_, type_):
    client.post('/api/device', json={'id': id_, 'type': type_, 'hostname': id_})


class TestJudgeLogicWithMockedBackend:
    def test_no_backend_available_returns_unknown_not_false(self, monkeypatch):
        """バックエンド全滅時はverdict=None(不明)であり、Falseではない
        (フェイクの不合格判定を作らない)。"""
        monkeypatch.setattr(llm_judge, '_chat_completion', lambda *a, **k: None)
        result = llm_judge.judge("some output", "some assertion")
        assert result.verdict is None
        assert result.available is False

    def test_well_formed_true_verdict_is_parsed(self, monkeypatch):
        monkeypatch.setattr(
            llm_judge, '_chat_completion',
            lambda *a, **k: json.dumps({"verdict": True, "reasoning": "妥当"}))
        result = llm_judge.judge("Neighbor 192.0.2.1  Full  00:00:38",
                                  "OSPFネイバーがFull状態")
        assert result.verdict is True
        assert result.available is True
        assert result.reasoning == "妥当"

    def test_well_formed_false_verdict_is_parsed(self, monkeypatch):
        monkeypatch.setattr(
            llm_judge, '_chat_completion',
            lambda *a, **k: json.dumps({"verdict": False, "reasoning": "Downのまま"}))
        result = llm_judge.judge("Neighbor 192.0.2.1  Down  -",
                                  "OSPFネイバーがFull状態")
        assert result.verdict is False
        assert result.available is True

    def test_markdown_fenced_json_is_stripped_before_parsing(self, monkeypatch):
        fenced = "```json\n" + json.dumps({"verdict": True, "reasoning": "ok"}) + "\n```"
        monkeypatch.setattr(llm_judge, '_chat_completion', lambda *a, **k: fenced)
        result = llm_judge.judge("out", "assertion")
        assert result.verdict is True

    def test_malformed_json_response_is_unknown_not_false(self, monkeypatch):
        """LLMがJSONとして解釈できない応答を返した場合もverdict=None
        (Falseに倒してフェイクの不合格判定を作らない)。"""
        monkeypatch.setattr(llm_judge, '_chat_completion', lambda *a, **k: "そうですね、たぶん大丈夫です")
        result = llm_judge.judge("out", "assertion")
        assert result.verdict is None
        assert result.available is True  # 応答は得られたが解釈できなかった

    def test_response_with_non_bool_verdict_field_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            llm_judge, '_chat_completion',
            lambda *a, **k: json.dumps({"verdict": "yes", "reasoning": "..."}))
        result = llm_judge.judge("out", "assertion")
        assert result.verdict is None

    def test_context_is_included_in_the_prompt_sent_to_the_backend(self, monkeypatch):
        captured = {}

        def fake_chat_completion(messages, **kwargs):
            captured['messages'] = messages
            return json.dumps({"verdict": True, "reasoning": "ok"})

        monkeypatch.setattr(llm_judge, '_chat_completion', fake_chat_completion)
        llm_judge.judge("out", "assertion", context="device_type=cisco, command=show ip ospf neighbor")
        user_msg = captured['messages'][-1]['content']
        assert "device_type=cisco" in user_msg
        assert "out" in user_msg
        assert "assertion" in user_msg


class TestSkipIfUnavailable:
    def test_skips_when_verdict_is_none(self):
        unavailable = llm_judge.JudgeResult(verdict=None, reasoning="no backend", available=False)
        with pytest.raises(Exception) as exc_info:
            llm_judge.skip_if_unavailable(unavailable)
        assert 'Skipped' in type(exc_info.value).__name__

    def test_does_not_skip_when_verdict_is_present(self):
        available = llm_judge.JudgeResult(verdict=True, reasoning="ok", available=True)
        llm_judge.skip_if_unavailable(available)  # raises nothing


class TestWrapExistingShowOutputTest:
    """既存のshow出力ベース検証テストに、LLM判定を薄く追加する具体例。

    OSPF/BGPのような実ネイバー確立は実ソケット(scapyの生ソケット等、
    このサンドボックスでは`SCAPY_AVAILABLE=False`)に依存しスキップ
    され得るため、デモには単一装置のスタティックルート設定という
    決定的で依存の無いケースを使う(厳密な文字列検証
    `assert 'S' in out`はそのまま残し、LLM判定はあくまで追加の
    レイヤーとして扱う)——このサンドボックスはどのLLMバックエンドにも
    到達できないため、`skip_if_unavailable`で安全にskipされる
    (フェイクの合格を作らない)ことまでを固定する。
    """

    def test_static_route_reflected_in_show_ip_route_llm_judge_wrapper(self):
        dev_id = 'llmjudge-route-1'
        _dev(dev_id, 'cisco')
        _cli(dev_id, 'configure terminal')
        _cli(dev_id, 'interface GigabitEthernet0/0/0')
        _cli(dev_id, 'ip address 192.0.2.1 255.255.255.0')
        _cli(dev_id, 'no shutdown')
        _cli(dev_id, 'exit')
        _cli(dev_id, 'ip route 203.0.113.0 255.255.255.0 192.0.2.254')
        _cli(dev_id, 'end')

        out = _cli(dev_id, 'show ip route')
        # 既存の厳密な文字列検証(regression防止、これはLLM判定に置き換えない)
        assert 'S' in out and '203.0.113.0/24' in out

        # ここから追加するLLM判定レイヤー。利用可能なLLMバックエンドが
        # 無ければ安全にskipされ、Pass/Failを偽装しない。
        result = llm_judge.judge(
            out,
            "203.0.113.0/24宛てのスタティックルートが、ネクストホップ"
            "192.0.2.254経由として正しく反映されている",
            context="device_type=cisco, command='show ip route'",
        )
        llm_judge.skip_if_unavailable(result)
        assert result.verdict is True, result.reasoning


class TestCliEntrypoint:
    def test_cli_reports_unavailable_when_no_backend(self, monkeypatch, capsys):
        monkeypatch.setattr(llm_judge, '_chat_completion', lambda *a, **k: None)
        monkeypatch.setattr(sys, 'argv', ['llm_judge.py', '--output', 'x', '--assertion', 'y'])
        rc = llm_judge.main()
        assert rc == 2
        assert 'UNAVAILABLE' in capsys.readouterr().out
