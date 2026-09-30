"""
tools/link_capacity_forecast.py テスト

線形回帰によるトレンド外挿（NumPy polyfit）が正しく動くこと、
「最有力経路（OSPFコスト最小）が近いうちに逼迫する」判定が
実際に効くことを固定する。
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.link_capacity_forecast import (
    RateSample, rate_series, forecast_seconds_to_threshold,
    current_utilization, analyze_interface,
)


def _ramp_samples(start_bps, slope_bps_per_sec, n=20, interval=10.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    cum = 0
    samples = []
    for i in range(n):
        t = i * interval
        bps = max(0.0, start_bps + slope_bps_per_sec * t + rng.normal(0, noise))
        cum += int(bps * interval / 8.0)
        samples.append(RateSample(t, cum))
    return samples


def test_rate_series_needs_at_least_two_samples():
    times, rates = rate_series([RateSample(0.0, 100)])
    assert len(times) == 0
    assert len(rates) == 0


def test_rate_series_computes_bps_from_byte_deltas():
    # 10秒で1250バイト増えた = 1000bit/10s = 100bps
    samples = [RateSample(0.0, 0), RateSample(10.0, 1250)]
    times, rates = rate_series(samples)
    assert len(rates) == 1
    assert rates[0] == pytest.approx(1000.0, rel=1e-6)


def test_rate_series_skips_counter_resets():
    """カウンタが巻き戻った区間（リセット等）は除外する。"""
    samples = [RateSample(0.0, 1000), RateSample(10.0, 200), RateSample(20.0, 400)]
    times, rates = rate_series(samples)
    # 0->10sの区間（巻き戻り）は除外され、10->20sの区間だけ残る
    assert len(rates) == 1


def test_forecast_returns_none_for_flat_trend():
    samples = _ramp_samples(start_bps=10_000, slope_bps_per_sec=0, n=10)
    times, rates = rate_series(samples)
    eta = forecast_seconds_to_threshold(times, rates, capacity_bps=100_000)
    assert eta is None


def test_forecast_returns_none_for_decreasing_trend():
    samples = _ramp_samples(start_bps=50_000, slope_bps_per_sec=-100, n=10)
    times, rates = rate_series(samples)
    eta = forecast_seconds_to_threshold(times, rates, capacity_bps=100_000)
    assert eta is None


def test_forecast_predicts_time_to_threshold_for_rising_trend():
    """毎秒1000bpsずつ増える負荷が、80kbpsの回線で80%(64kbps)に
    到達するまでの時間を線形外挿で当てられること。"""
    samples = _ramp_samples(start_bps=0, slope_bps_per_sec=1000, n=100, interval=1.0)
    times, rates = rate_series(samples)
    eta = forecast_seconds_to_threshold(times, rates, capacity_bps=80_000,
                                        target_utilization=0.8)
    assert eta is not None
    # 理論値: rate(t)=1000*t が 64000 に達するのは t=64秒。
    # 直近サンプルの時刻(約98秒)から見て、64秒はもう過ぎているはず
    # （このシナリオでは既にサンプル区間内で閾値超過している）
    assert eta == pytest.approx(0.0, abs=1.0)


def test_current_utilization_reflects_latest_sample():
    samples = [RateSample(0.0, 0), RateSample(10.0, 125_000)]  # 100kbps
    times, rates = rate_series(samples)
    util = current_utilization(times, rates, capacity_bps=200_000)
    assert util == pytest.approx(0.5, rel=1e-6)


def test_analyze_interface_recommends_raising_cost_for_busy_preferred_link():
    """OSPFコストが最小(=最有力経路)で、かつ近い将来逼迫するリンクは
    コスト見直しを推奨すべき。"""
    samples = _ramp_samples(start_bps=20_000, slope_bps_per_sec=3_000,
                            n=30, interval=10.0)
    result = analyze_interface(samples, capacity_bps=256_000, ospf_cost=1,
                               min_ospf_cost=1, target_utilization=0.8,
                               warn_within_seconds=3600.0)
    assert result['recommend_raise_cost'] is True
    assert result['seconds_to_threshold'] is not None


def test_analyze_interface_does_not_flag_backup_link():
    """同じ逼迫トレンドでも、既にコストが高く最有力経路でなければ
    OSPFがそもそも選ばない経路なので推奨しない。"""
    samples = _ramp_samples(start_bps=20_000, slope_bps_per_sec=3_000,
                            n=30, interval=10.0)
    result = analyze_interface(samples, capacity_bps=256_000, ospf_cost=50,
                               min_ospf_cost=1, target_utilization=0.8,
                               warn_within_seconds=3600.0)
    assert result['recommend_raise_cost'] is False


def test_analyze_interface_does_not_flag_stable_preferred_link():
    """最有力経路でも、負荷が増えていなければ推奨しない。"""
    samples = _ramp_samples(start_bps=20_000, slope_bps_per_sec=0,
                            n=30, interval=10.0)
    result = analyze_interface(samples, capacity_bps=256_000, ospf_cost=1,
                               min_ospf_cost=1, target_utilization=0.8,
                               warn_within_seconds=3600.0)
    assert result['recommend_raise_cost'] is False
