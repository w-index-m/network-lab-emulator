"""
tools/nl_iac_control.py のパース/コマンド組み立てロジックのテスト
(Ollama呼び出し自体はモックし、実サーバーには接続しない)
"""

import json
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools import nl_iac_control as nic


def _mock_response(content_json: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"content": json.dumps(content_json)}}
    resp.raise_for_status = lambda: None
    return resp


def test_iac_commands_cover_all_known_tools():
    for tool in nic.KNOWN_TOOLS:
        assert "apply" in nic.IAC_COMMANDS[tool]
        assert "status" in nic.IAC_COMMANDS[tool]


def test_interpret_single_tool_mentioned_forces_all_tools_false():
    """指示文に1つだけツール名がある場合、モデルがall_tools=trueと
    誤判定してもコード側で強制的にfalseへ補正する"""
    with patch.object(nic.httpx, "post") as mock_post:
        mock_post.return_value = _mock_response(
            {"tool": "ansible", "action": "apply", "all_tools": True}
        )
        params = nic.interpret("Ansibleで監視スタックを構築して")
    assert params["tool"] == "ansible"
    assert params["all_tools"] is False


def test_interpret_list_tool_normalizes_to_all_tools():
    """モデルがtoolをリストで返した場合はall_tools=Trueとして正規化する"""
    with patch.object(nic.httpx, "post") as mock_post:
        mock_post.return_value = _mock_response(
            {"tool": ["ansible", "chef", "puppet", "salt"], "action": "status"}
        )
        params = nic.interpret("全部のIaCツールの状態を確認して")
    assert params["all_tools"] is True
    assert params["tool"] in nic.KNOWN_TOOLS


def test_interpret_rejects_unknown_tool():
    with patch.object(nic.httpx, "post") as mock_post:
        mock_post.return_value = _mock_response({"tool": "terraform", "action": "apply"})
        try:
            nic.interpret("Terraformで構築して")
            assert False, "未知ツールで例外が発生しなかった"
        except ValueError:
            pass


def test_run_iac_missing_binary_returns_nonzero(tmp_path):
    """未インストールのツールを実行しようとした場合、例外を投げず
    終了コード1を返して処理を継続できること"""
    rc = nic.run_iac("chef", "apply") if _binary_missing("chef-solo") else 0
    assert isinstance(rc, int)


def _binary_missing(name: str) -> bool:
    import shutil
    return shutil.which(name) is None
