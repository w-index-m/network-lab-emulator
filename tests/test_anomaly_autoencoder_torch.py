"""
tools/anomaly_autoencoder_torch.py テスト

tools/test_anomaly_autoencoder.py(NumPy版)と同じシナリオをtorchバック
エンドで固定する。torchはrequirements-ml.txt経由の任意インストール
(CLAUDE.mdの「ML / anomaly detection notes」参照)のため、未導入の
環境ではこのファイルごとskipする。
"""

import os
import sys

import numpy as np
import pytest

torch = pytest.importorskip('torch')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.anomaly_autoencoder_torch import (  # noqa: E402
    DeviceAnomalyModelTorch, TorchAutoencoder,
)


def _normal_samples(seed=0, n=200):
    rng = np.random.default_rng(seed)
    cpu = rng.uniform(8, 25, size=n)
    bw = cpu * 40 + rng.normal(0, 30, size=n)
    return list(zip(cpu, bw))


def test_autoencoder_training_reduces_loss():
    rng = np.random.default_rng(0)
    x = torch.as_tensor(rng.normal(0, 1, size=(50, 2)), dtype=torch.float32)
    torch.manual_seed(0)
    ae = TorchAutoencoder(input_dim=2, hidden_dim=3)
    optimizer = torch.optim.SGD(ae.parameters(), lr=0.05)
    loss_fn = torch.nn.MSELoss()

    def step():
        optimizer.zero_grad()
        out = ae(x)
        loss = loss_fn(out, x)
        loss.backward()
        optimizer.step()
        return float(loss.item())

    first = step()
    for _ in range(200):
        last = step()
    assert last < first


def test_fit_requires_minimum_samples():
    model = DeviceAnomalyModelTorch()
    with pytest.raises(ValueError):
        model.fit([(10.0, 100.0), (12.0, 120.0)])


def test_normal_sample_has_low_reconstruction_error():
    model = DeviceAnomalyModelTorch(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert not model.is_anomaly(18.0, 18.0 * 40)
    assert not model.is_anomaly(12.0, 12.0 * 40)


def test_moderate_anomaly_below_rule_based_threshold_is_flagged():
    model = DeviceAnomalyModelTorch(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(55.0, 55.0 * 40)


def test_extreme_anomaly_is_flagged():
    model = DeviceAnomalyModelTorch(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(95.0, 95.0 * 40)


def test_traffic_only_anomaly_is_flagged_even_with_normal_cpu():
    model = DeviceAnomalyModelTorch(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    assert model.is_anomaly(15.0, 5000.0)


def test_score_is_monotonic_with_deviation_from_normal():
    model = DeviceAnomalyModelTorch(seed=1)
    model.fit(_normal_samples(), epochs=400, lr=0.1)
    err_normal = model.score(18.0, 18.0 * 40)
    err_moderate = model.score(55.0, 55.0 * 40)
    err_extreme = model.score(95.0, 95.0 * 40)
    assert err_normal < err_moderate < err_extreme


def test_score_before_fit_raises():
    model = DeviceAnomalyModelTorch()
    with pytest.raises(RuntimeError):
        model.score(10.0, 100.0)
