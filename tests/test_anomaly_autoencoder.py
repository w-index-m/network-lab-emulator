"""
tools/anomaly_autoencoder.py テスト

固定閾値のルールベース検知（tools/ai_grafana_autopilot.py）では
見逃してしまう「装置ごとの普段と違う」を、Autoencoderの復元誤差で
拾えることを固定する。
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.anomaly_autoencoder import Autoencoder, DeviceAnomalyModel


def _normal_samples(seed=0, n=200):
    rng = np.random.default_rng(seed)
    cpu = rng.uniform(8, 25, size=n)
    bw = cpu * 40 + rng.normal(0, 30, size=n)
    return list(zip(cpu, bw))


def test_autoencoder_training_reduces_loss():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, size=(50, 2))
    ae = Autoencoder(input_dim=2, hidden_dim=3, seed=0)
    first = ae.train_step(x, lr=0.05)
    for _ in range(200):
        last = ae.train_step(x, lr=0.05)
    assert last < first


def test_fit_requires_minimum_samples():
    model = DeviceAnomalyModel()
    with pytest.raises(ValueError):
        model.fit([(10.0, 100.0), (12.0, 120.0)])


def test_normal_sample_has_low_reconstruction_error():
    model = DeviceAnomalyModel(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert not model.is_anomaly(18.0, 18.0 * 40)
    assert not model.is_anomaly(12.0, 12.0 * 40)


def test_moderate_anomaly_below_rule_based_threshold_is_flagged():
    """CPU 55%はルールベースの80%閾値を大きく下回るが、
    普段10〜25%の装置にとっては明らかに異常であるべき。"""
    model = DeviceAnomalyModel(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(55.0, 55.0 * 40)


def test_extreme_anomaly_is_flagged():
    model = DeviceAnomalyModel(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(95.0, 95.0 * 40)


def test_traffic_only_anomaly_is_flagged_even_with_normal_cpu():
    """CPUは正常域でも、CPUと相関しない突発的な通信量は異常として拾う。"""
    model = DeviceAnomalyModel(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(15.0, 5000.0)


def test_score_is_monotonic_with_deviation_from_normal():
    model = DeviceAnomalyModel(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    err_normal = model.score(18.0, 18.0 * 40)
    err_moderate = model.score(55.0, 55.0 * 40)
    err_extreme = model.score(95.0, 95.0 * 40)
    assert err_normal < err_moderate < err_extreme


def test_score_before_fit_raises():
    model = DeviceAnomalyModel()
    with pytest.raises(RuntimeError):
        model.score(10.0, 100.0)


def test_different_devices_learn_different_baselines():
    """普段CPUが高い装置(コアスイッチ等)では、同じ55%でも異常にならない。"""
    busy_samples = [(cpu, cpu * 40) for cpu in
                     np.random.default_rng(2).uniform(45, 65, size=200)]
    busy_model = DeviceAnomalyModel(seed=1)
    busy_model.fit(busy_samples, epochs=400, lr=0.1)
    assert not busy_model.is_anomaly(55.0, 55.0 * 40)

    idle_model = DeviceAnomalyModel(seed=1)
    idle_model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert idle_model.is_anomaly(55.0, 55.0 * 40)
