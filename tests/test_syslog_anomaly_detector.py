"""
tools/syslog_anomaly_detector.py の単体テスト。

Lokiへの実問い合わせ部分(fetch_syslog_entries)はネットワークI/Oなので、
バケット化・異常検知ロジック(bucket_counts/detect_spikes/
LogRateAnomalyModel)だけを合成データで固定する。実際のLokiへの
問い合わせと組み合わせたエンドツーエンドの検証はdocs/を参照。
"""
import os
import sys

import numpy as np
import pytest

os.environ.setdefault('NETLAB_AUTH_DISABLE', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.syslog_anomaly_detector import (  # noqa: E402
    LogRateAnomalyModel, bucket_counts, detect_spikes,
)


class TestLogRateAnomalyModel:
    def test_flags_count_far_above_baseline(self):
        model = LogRateAnomalyModel()
        model.fit(np.array([2.0, 3.0, 2.0, 3.0, 2.0, 3.0]), threshold_k=4.0)
        assert model.is_anomaly(50.0)

    def test_does_not_flag_normal_fluctuation(self):
        model = LogRateAnomalyModel()
        model.fit(np.array([2.0, 3.0, 2.0, 3.0, 2.0, 4.0]), threshold_k=4.0)
        assert not model.is_anomaly(3.0)

    def test_zero_variance_baseline_falls_back_to_excess_over_mean(self):
        model = LogRateAnomalyModel()
        model.fit(np.array([1.0, 1.0, 1.0, 1.0]), threshold_k=4.0)
        # 標準偏差0のときはスコア=超過量そのもの
        assert model.score(1.0) == 0.0
        assert model.score(10.0) == pytest.approx(9.0)

    def test_fit_requires_at_least_two_buckets(self):
        model = LogRateAnomalyModel()
        with pytest.raises(ValueError):
            model.fit(np.array([1.0]))


class TestBucketCounts:
    def test_counts_entries_into_correct_time_buckets(self):
        start = 1000.0
        entries = [
            {'ts': 1000.5, 'source_ip': '10.0.0.1', 'line': 'a'},
            {'ts': 1005.0, 'source_ip': '10.0.0.1', 'line': 'b'},
            {'ts': 1065.0, 'source_ip': '10.0.0.1', 'line': 'c'},
        ]
        result = bucket_counts(entries, start, start + 120, bucket_seconds=60)
        assert list(result['10.0.0.1']) == [2.0, 1.0, 0.0]

    def test_separates_by_source_ip(self):
        start = 0.0
        entries = [
            {'ts': 1.0, 'source_ip': 'a', 'line': 'x'},
            {'ts': 1.0, 'source_ip': 'b', 'line': 'y'},
        ]
        result = bucket_counts(entries, start, start + 60, bucket_seconds=60)
        assert result['a'][0] == 1.0
        assert result['b'][0] == 1.0


class TestDetectSpikes:
    def test_detects_a_real_spike_in_the_recent_half(self):
        # 前半(バケット0-4)は平常、後半(5-9)の最後だけ急増
        counts = np.array([2, 3, 2, 3, 2, 2, 3, 2, 3, 50], dtype=float)
        result = detect_spikes({'10.0.0.1': counts}, baseline_fraction=0.5,
                                threshold_k=4.0)
        assert result['10.0.0.1']['is_anomaly'] is True
        assert result['10.0.0.1']['peak_count'] == 50.0
        assert result['10.0.0.1']['peak_bucket_offset'] == 9

    def test_no_spike_stays_unflagged(self):
        counts = np.array([2, 3, 2, 3, 2, 2, 3, 2, 3, 3], dtype=float)
        result = detect_spikes({'10.0.0.1': counts}, baseline_fraction=0.5,
                                threshold_k=4.0)
        assert result['10.0.0.1']['is_anomaly'] is False

    def test_devices_with_too_few_buckets_are_skipped(self):
        result = detect_spikes({'10.0.0.1': np.array([1.0])})
        assert '10.0.0.1' not in result

    def test_multiple_devices_evaluated_independently(self):
        quiet = np.array([1, 1, 1, 1, 1, 1], dtype=float)
        spiking = np.array([1, 1, 1, 1, 1, 30], dtype=float)
        result = detect_spikes({'quiet-dev': quiet, 'spiking-dev': spiking})
        assert result['quiet-dev']['is_anomaly'] is False
        assert result['spiking-dev']['is_anomaly'] is True
