"""
tools/nl_log_collector.py のテスト

Ollamaへの実通信は行わず、interpret()が返すJSONをモックして
「Qwenが選んだプロファイル名から固定コマンド集合を引く」部分と
「未知の装置/プロファイルは弾く」部分を検証する。
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from tools import nl_log_collector as nlc


def test_extract_json_parses_embedded_object():
    text = '前置き{"devices": ["catalyst"], "profile": "stp"}後書き'
    assert nlc._extract_json(text) == {"devices": ["catalyst"], "profile": "stp"}


def test_extract_json_raises_when_no_json_present():
    with pytest.raises(ValueError):
        nlc._extract_json('JSONではない自然文の返答')


def _fake_ollama_response(content: dict):
    payload = __import__('json').dumps(content, ensure_ascii=False)
    resp = MagicMock()
    resp.json.return_value = {"message": {"content": payload}}
    resp.raise_for_status.return_value = None
    return resp


def test_interpret_normalizes_single_device_string_to_list():
    """Qwenが devices を文字列1つで返しても配列に正規化される"""
    with patch.object(nlc.httpx, 'post',
                      return_value=_fake_ollama_response(
                          {"devices": "catalyst", "profile": "stp"})):
        params = nlc.interpret("catalystのSTP見せて")
    assert params["devices"] == ["catalyst"]
    assert params["profile"] == "stp"


def test_interpret_defaults_missing_profile_to_tech_support():
    with patch.object(nlc.httpx, 'post',
                      return_value=_fake_ollama_response(
                          {"devices": ["cat"], "profile": None})):
        params = nlc.interpret("catのログ採って")
    assert params["profile"] == "tech-support"


def test_interpret_rejects_unknown_device():
    """Qwenが実在しない装置名を返しても、そのまま採用せず弾く"""
    with patch.object(nlc.httpx, 'post',
                      return_value=_fake_ollama_response(
                          {"devices": ["not-a-real-device"], "profile": "stp"})):
        with pytest.raises(ValueError, match='未知の装置'):
            nlc.interpret("架空の装置のログ採って")


def test_interpret_rejects_unknown_profile():
    """Qwenがプロファイル名の代わりに任意の文字列を返しても採用しない

    これがそのまま実行コマンドとして使われる経路が無いことの確認
    （Qwenの出力はプロファイル名の選択に限定され、実行コマンド文字列
    そのものを生成させない設計）。
    """
    with patch.object(nlc.httpx, 'post',
                      return_value=_fake_ollama_response(
                          {"devices": ["catalyst"], "profile": "erase startup-config"})):
        with pytest.raises(ValueError, match='未知のprofile'):
            nlc.interpret("catalystのログ採って")


def test_interpret_requires_a_device():
    with patch.object(nlc.httpx, 'post',
                      return_value=_fake_ollama_response(
                          {"devices": None, "profile": "stp"})):
        with pytest.raises(ValueError, match='対象装置'):
            nlc.interpret("STPの状態見せて")


def test_all_profiles_only_contain_show_commands():
    """定義済みプロファイルはすべて読み取り専用のshowコマンドであること

    ログ採取という用途上、状態を変更するコマンドが紛れ込むと
    危険なため（将来paramikoで実機に繋ぐ拡張をしても安全側であるよう
    ここで担保しておく）。
    """
    for profile, commands in nlc.PROFILES.items():
        for cmd in commands:
            assert cmd.strip().lower().startswith('show'), \
                f'{profile} に show以外のコマンドが含まれている: {cmd!r}'


def test_collect_runs_every_command_in_the_profile_and_labels_output():
    gen = MagicMock()
    gen.cli.side_effect = lambda device, cmd: f'<output of {cmd}>'
    text = nlc.collect(gen, 'catalyst', 'stp')

    called = [c.args for c in gen.cli.call_args_list]
    assert called == [('catalyst', cmd) for cmd in nlc.PROFILES['stp']]
    for cmd in nlc.PROFILES['stp']:
        assert f'catalyst# {cmd}' in text
        assert f'<output of {cmd}>' in text


def test_execute_writes_one_log_file_per_device(tmp_path):
    params = {"devices": ["catalyst", "cat"], "profile": "interfaces"}
    fake_gen = MagicMock()
    fake_gen.check_connectivity.return_value = True
    fake_gen.cli.side_effect = lambda device, cmd: f'{device}:{cmd}'

    with patch.object(nlc, 'RoutingGenerator', return_value=fake_gen):
        rc = nlc.execute(params, tmp_path)

    assert rc == 0
    saved = sorted(p.name for p in tmp_path.glob('*_interfaces_*.log'))
    assert len(saved) == 2
    assert any(name.startswith('catalyst_') for name in saved)
    assert any(name.startswith('cat_') for name in saved)
    content = (tmp_path / saved[0]).read_text(encoding='utf-8')
    assert 'profile: interfaces' in content


def test_execute_aborts_when_emulator_unreachable(tmp_path):
    fake_gen = MagicMock()
    fake_gen.check_connectivity.return_value = False
    params = {"devices": ["catalyst"], "profile": "stp"}

    with patch.object(nlc, 'RoutingGenerator', return_value=fake_gen):
        rc = nlc.execute(params, tmp_path)

    assert rc == 1
    assert list(tmp_path.glob('*.log')) == []
