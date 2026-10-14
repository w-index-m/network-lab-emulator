"""
tools/prometheus_exporter.py テスト

- build_metrics_text() が Prometheus text exposition format の
  「同一メトリック名のサンプルは連続していなければならない」という
  仕様を満たしているか（初回実装ではこれを破っており、prometheus_client
  のパーサーで585個の別々のfamilyに分解されてしまうバグがあった）
"""

import sys
import os
import json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.prometheus_exporter import build_metrics_text, _load_watchlist, build_strix_metrics_text


def _sample_dashboard():
    return {
        'polled_at': 1000.0,
        'devices': [
            {
                'device_id': 'r1', 'hostname': 'R1', 'type': 'cisco',
                'sys_uptime_ticks': 12345, 'cpu_percent': 22, 'route_count': 5,
                'interfaces': [
                    {'index': 1, 'descr': 'Gi0/0', 'speed': 1000000000,
                     'admin_status': 1, 'oper_status': 1,
                     'in_octets': 100, 'out_octets': 200},
                    {'index': 2, 'descr': 'Gi0/1', 'speed': 1000000000,
                     'admin_status': 2, 'oper_status': 2,
                     'in_octets': 0, 'out_octets': 0},
                ],
            },
            {
                'device_id': 'sir-a', 'hostname': 'Router-A', 'type': 'sir',
                'sys_uptime_ticks': 500, 'cpu_percent': None, 'route_count': 2,
                'interfaces': [
                    {'index': 1, 'descr': 'lan0', 'speed': 1000000000,
                     'admin_status': 1, 'oper_status': 1,
                     'in_octets': 5, 'out_octets': 5},
                ],
            },
        ],
    }


def test_metric_samples_are_grouped_by_name_not_interleaved():
    """Prometheus text formatの仕様: 同一メトリック名のサンプルは連続していること"""
    text = build_metrics_text(_sample_dashboard())
    lines = [l for l in text.splitlines() if l and not l.startswith('#')]
    seen_names = []
    for line in lines:
        name = line.split('{')[0].split(' ')[0]
        if not seen_names or seen_names[-1] != name:
            seen_names.append(name)
    # 同じ名前が2回以上「離れて」出現しない = ユニーク名の出現順=グループ化されている
    assert len(seen_names) == len(set(seen_names)), \
        f'metric names are not grouped: {seen_names}'


def test_parses_with_prometheus_client_into_expected_families():
    prometheus_client = pytest.importorskip('prometheus_client')
    from prometheus_client.parser import text_string_to_metric_families
    text = build_metrics_text(_sample_dashboard())
    families = {f.name: f for f in text_string_to_metric_families(text)}
    assert families['netlab_device_up'].type == 'gauge'
    assert len(families['netlab_device_up'].samples) == 2
    assert len(families['netlab_interface_admin_status'].samples) == 3
    assert len(families['netlab_route_count'].samples) == 2
    assert {s.value for s in families['netlab_route_count'].samples} == {5, 2}
    # cpu_percentがNoneの装置(sir-a)は出力されない
    assert len(families['netlab_cpu_percent'].samples) == 1


def test_cpu_metric_omitted_entirely_when_no_device_has_it():
    data = _sample_dashboard()
    for d in data['devices']:
        d['cpu_percent'] = None
    text = build_metrics_text(data)
    assert 'netlab_cpu_percent' not in text


def test_label_values_are_escaped():
    data = _sample_dashboard()
    data['devices'][0]['hostname'] = 'R1 "weird" name'
    text = build_metrics_text(data)
    assert 'R1 \\"weird\\" name' in text


def test_watchlist_metric_omitted_when_empty():
    text = build_metrics_text(_sample_dashboard())
    assert 'netlab_watchlist_target' not in text


def test_watchlist_metric_emitted_for_matching_interface():
    watchlist = {('r1', 'Gi0/0')}
    text = build_metrics_text(_sample_dashboard(), watchlist)
    assert 'netlab_watchlist_target{device_id="r1"' in text
    assert 'interface="Gi0/0"} 1' in text
    # Gi0/1 (同じ装置の別IF)は監視対象に含まれていないので出力されない
    assert 'interface="Gi0/1"' not in text.split('netlab_watchlist_target')[1]


def test_watchlist_metric_ignores_nonexistent_interface():
    # nl_monitor_control.py が実在しないIFを登録してしまった場合でも、
    # ダッシュボードに存在しないインターフェースは出力しない(誤検知防止)
    watchlist = {('r1', 'Gi9/9')}
    text = build_metrics_text(_sample_dashboard(), watchlist)
    assert 'netlab_watchlist_target' not in text


def test_load_watchlist_missing_file_returns_empty_set(tmp_path):
    assert _load_watchlist(str(tmp_path / 'nonexistent.json')) == set()


def test_load_watchlist_parses_device_and_interface_pairs(tmp_path):
    p = tmp_path / 'watchlist.json'
    p.write_text(json.dumps([
        {'device_id': 'catalyst', 'interface': 'GigabitEthernet1/0/1', 'ip': '10.9.9.1'},
        {'device_id': 'asa', 'interface': 'GigabitEthernet0/0'},
    ]), encoding='utf-8')
    assert _load_watchlist(str(p)) == {
        ('catalyst', 'GigabitEthernet1/0/1'),
        ('asa', 'GigabitEthernet0/0'),
    }


def test_load_watchlist_malformed_json_returns_empty_set(tmp_path):
    p = tmp_path / 'broken.json'
    p.write_text('{not valid json', encoding='utf-8')
    assert _load_watchlist(str(p)) == set()


# ── build_strix_metrics_text() ──
# ユーザー提案「それを、GrafanaやPrometheusでステータスを取ったらどうなるかな」
# への対応。/pentest操作パネルと同じ/api/pentest/runsのレスポンス形状を
# Prometheusメトリクスにする。GITHUB_TOKEN未設定等でエンドポイント自体が
# 失敗している場合は空文字列を返し(SNMP側のメトリクス公開は止めない)、
# 回帰テストはネットワーク非依存のまま成立する。

def test_ok_false_returns_empty_string():
    assert build_strix_metrics_text({'ok': False, 'error': 'GITHUB_TOKENが設定されていません'}) == ''


def test_no_runs_yet_produces_zero_counts_no_crash():
    text = build_strix_metrics_text({'ok': True, 'runs': []})
    assert 'netlab_strix_last_run_success 0' in text
    assert 'netlab_strix_last_run_in_progress 0' in text
    assert 'netlab_strix_recent_runs_total{conclusion="success"} 0' in text
    assert 'netlab_strix_recent_runs_total{conclusion="failure"} 0' in text


def test_latest_run_success_is_reflected():
    data = {'ok': True, 'runs': [
        {'id': 2, 'status': 'completed', 'conclusion': 'success'},
        {'id': 1, 'status': 'completed', 'conclusion': 'failure'},
    ]}
    text = build_strix_metrics_text(data)
    assert 'netlab_strix_last_run_success 1' in text
    assert 'netlab_strix_last_run_in_progress 0' in text
    assert 'netlab_strix_recent_runs_total{conclusion="success"} 1' in text
    assert 'netlab_strix_recent_runs_total{conclusion="failure"} 1' in text
    assert 'netlab_strix_run_info{run_id="2",status="completed",conclusion="success"} 1' in text
    assert 'netlab_strix_run_info{run_id="1",status="completed",conclusion="failure"} 1' in text


def test_in_progress_run_is_reflected():
    data = {'ok': True, 'runs': [{'id': 3, 'status': 'in_progress', 'conclusion': None}]}
    text = build_strix_metrics_text(data)
    assert 'netlab_strix_last_run_in_progress 1' in text
    assert 'netlab_strix_last_run_success 0' in text
    assert 'netlab_strix_run_info{run_id="3",status="in_progress",conclusion="none"} 1' in text


# ── netlab_strix_last_success_age_seconds ──
# 「それを、GrafanaやPrometheusでステータスを取ったらどうなるかな」への
# 回答として提案した「失敗だけでなく長期停滞にも気づけるstalenessメトリ
# クス」。成功run無し = -1、成功runあり = 実際の経過秒数、を固定する。

def test_no_successful_run_ever_reports_minus_one():
    data = {'ok': True, 'runs': [{'id': 1, 'status': 'completed', 'conclusion': 'failure'}]}
    text = build_strix_metrics_text(data)
    assert 'netlab_strix_last_success_age_seconds -1' in text


def test_no_runs_at_all_reports_minus_one():
    text = build_strix_metrics_text({'ok': True, 'runs': []})
    assert 'netlab_strix_last_success_age_seconds -1' in text


def test_recent_success_reports_a_small_positive_age():
    import datetime
    recent = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')
    data = {'ok': True, 'runs': [
        {'id': 2, 'status': 'completed', 'conclusion': 'success', 'created_at': recent},
    ]}
    text = build_strix_metrics_text(data)
    for line in text.splitlines():
        if line.startswith('netlab_strix_last_success_age_seconds '):
            age = int(line.split()[1])
            assert 25 <= age <= 60
            break
    else:
        pytest.fail('netlab_strix_last_success_age_seconds 行が見つからない')


def test_malformed_created_at_falls_back_to_minus_one():
    data = {'ok': True, 'runs': [
        {'id': 2, 'status': 'completed', 'conclusion': 'success', 'created_at': 'not-a-date'},
    ]}
    text = build_strix_metrics_text(data)
    assert 'netlab_strix_last_success_age_seconds -1' in text


def test_updated_at_is_preferred_over_created_at():
    """createdはDispatch時刻のまま変わらないため、completedになった時点
    でも更新されるupdated_atを優先して使うことの固定(実行時間が長い
    workflowだとcreated_at基準では経過秒数を実際より長く見積もって
    しまうバグを修正した際に追加)。"""
    import datetime
    old_created = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    recent_updated = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')
    data = {'ok': True, 'runs': [
        {'id': 2, 'status': 'completed', 'conclusion': 'success',
         'created_at': old_created, 'updated_at': recent_updated},
    ]}
    text = build_strix_metrics_text(data)
    for line in text.splitlines():
        if line.startswith('netlab_strix_last_success_age_seconds '):
            age = int(line.split()[1])
            assert 25 <= age <= 60, f'created_atが使われている(age={age})'
            break
    else:
        pytest.fail('netlab_strix_last_success_age_seconds 行が見つからない')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
