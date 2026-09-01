"""
tools/prometheus_exporter.py テスト

- build_metrics_text() が Prometheus text exposition format の
  「同一メトリック名のサンプルは連続していなければならない」という
  仕様を満たしているか（初回実装ではこれを破っており、prometheus_client
  のパーサーで585個の別々のfamilyに分解されてしまうバグがあった）
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from tools.prometheus_exporter import build_metrics_text


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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
