"""
tools/strix_status_to_loki.py テスト

ユーザー提案「GrafanaアラートやGrafana Lokiと連携してほしい」への対応。
ネットワーク非依存のロジック(どのrunを新規に転送対象とするか/ログ行の
整形)のみを固定する——push_to_loki()自体(実際のLoki書き込み)は
tools/device_log_to_loki.pyの既存テストと同じ方針でテスト対象外。
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.strix_status_to_loki import new_completed_runs, format_log_line


def _run(id_, status, conclusion):
    return {'id': id_, 'status': status, 'conclusion': conclusion,
            'html_url': f'https://github.com/x/y/actions/runs/{id_}'}


def test_completed_run_not_yet_seen_is_returned():
    runs = [_run(2, 'completed', 'success')]
    assert new_completed_runs(runs, seen_ids=set()) == runs


def test_already_seen_run_is_not_returned_again():
    runs = [_run(2, 'completed', 'success')]
    assert new_completed_runs(runs, seen_ids={2}) == []


def test_in_progress_run_is_not_returned():
    runs = [_run(3, 'in_progress', None)]
    assert new_completed_runs(runs, seen_ids=set()) == []


def test_queued_run_is_not_returned():
    runs = [_run(4, 'queued', None)]
    assert new_completed_runs(runs, seen_ids=set()) == []


def test_multiple_new_completed_runs_are_returned_oldest_first():
    # /api/pentest/runsは新しい順(id降順)で返るため、転送順は古い順に
    # 反転されることを固定する。
    runs = [_run(3, 'completed', 'failure'), _run(2, 'completed', 'success')]
    result = new_completed_runs(runs, seen_ids=set())
    assert [r['id'] for r in result] == [2, 3]


def test_format_log_line_includes_conclusion_and_url():
    line = format_log_line(_run(5, 'completed', 'success'))
    assert 'run #5' in line
    assert 'conclusion=success' in line
    assert 'runs/5' in line


def test_format_log_line_handles_missing_conclusion():
    line = format_log_line(_run(6, 'completed', None))
    assert 'conclusion=unknown' in line
