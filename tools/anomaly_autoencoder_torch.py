#!/usr/bin/env python3
"""
tools/anomaly_autoencoder.py のPyTorch版。

NumPy版は「装置ごとの普段のふるまい」を学習するAutoencoderを
手書きの前方伝播・逆伝播で実装しており、このエミュレータが出す
2次元(CPU%, 通信量)程度のデータ量ではそれで十分動く。

このモジュールは、torchが実際に使える環境(requirements-ml.txt、
CLAUDE.mdの「ML / anomaly detection notes」参照)になったことを受けて、
「技術スタックとしてtorchを使う／将来もっと複雑なモデル(LSTM等の
時系列モデル、多層ネットワーク)を試す土台にする」という目的で、
NumPy版と同じ公開APIをtorchの`nn.Module`/`optim`で作り直したもの。

NumPy版を置き換えるものではない(そちらは今も動くし、torch無しでも
動く唯一の実装として残す)。torchが使える環境でだけ、こちらを
選択的に使う想定。
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn

# torch.optim.SGD (any torch optimizer, really) lazily imports torch._dynamo
# on its first call. torch._dynamo's module-level init walks sys.modules and
# inspects each module's __file__; if that import is deferred until after
# something else in the same process has registered an unusual module in
# sys.modules (observed in this repo's full test suite: a grpc/protobuf-heavy
# test module collected between this module's import and the first fit()
# call), it can crash with `AttributeError: type object '__file__' has no
# attribute 'endswith'` - a pytorch/protobuf interaction bug, not anything
# wrong with the two-line NumPy-style model below. Triggering the import
# here, at this module's own import time (i.e. as early as possible, before
# other test modules get collected), sidesteps it: once torch._dynamo is
# cached in sys.modules it's never re-imported.
import torch._dynamo  # noqa: F401


class TorchAutoencoder(nn.Module):
    """1隠れ層のAutoencoder。activationはtanh、出力層は線形。

    tools/anomaly_autoencoder.py の Autoencoder(手書きNumPy版)と
    同じ構造(1隠れ層・tanh)をtorch.nnで表現したもの。
    """

    def __init__(self, input_dim: int, hidden_dim: int):
        super().__init__()
        self.encoder = nn.Linear(input_dim, hidden_dim)
        self.decoder = nn.Linear(hidden_dim, input_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = torch.tanh(self.encoder(x))
        return self.decoder(h)


class DeviceAnomalyModelTorch:
    """tools/anomaly_autoencoder.py の DeviceAnomalyModel と同じ公開API
    (fit/score/is_anomaly/threshold)をtorchバックエンドで実装したもの。

    特徴量・正規化・しきい値(平均+k*標準偏差)の考え方はNumPy版と同一。
    """

    def __init__(self, hidden_dim: int = 3, seed: int = 0):
        self.hidden_dim = hidden_dim
        self.seed = seed
        self.model: TorchAutoencoder | None = None
        self._byte_min = 0.0
        self._byte_max = 1.0
        self._threshold = float('inf')

    def _normalize(self, samples: np.ndarray) -> np.ndarray:
        cpu = samples[:, 0] / 100.0
        span = max(self._byte_max - self._byte_min, 1e-9)
        bw = (samples[:, 1] - self._byte_min) / span
        return np.stack([cpu, np.clip(bw, 0.0, 1.5)], axis=1)

    def fit(self, samples: list[tuple[float, float]],
            epochs: int = 400, lr: float = 0.05,
            threshold_k: float = 4.0) -> None:
        """samples: [(cpu_percent, bytes_per_interval), ...] の正常データ。

        threshold_k: 異常判定の閾値 = 学習データの復元誤差の
        平均 + threshold_k * 標準偏差。
        """
        arr = np.asarray(samples, dtype=float)
        if arr.shape[0] < 4:
            raise ValueError('学習には最低4サンプル必要')
        self._byte_min = float(arr[:, 1].min())
        self._byte_max = float(arr[:, 1].max())
        x_np = self._normalize(arr)

        torch.manual_seed(self.seed)
        self.model = TorchAutoencoder(input_dim=2, hidden_dim=self.hidden_dim)
        optimizer = torch.optim.SGD(self.model.parameters(), lr=lr)
        loss_fn = nn.MSELoss()

        x = torch.as_tensor(x_np, dtype=torch.float32)
        losses = []
        for _ in range(epochs):
            optimizer.zero_grad()
            out = self.model(x)
            loss = loss_fn(out, x)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.item()))

        with torch.no_grad():
            train_errors = ((self.model(x) - x) ** 2).mean(dim=1).numpy()
        mean_err = float(np.mean(train_errors))
        std_err = float(np.std(train_errors))
        self._threshold = mean_err + threshold_k * std_err
        self._final_loss = losses[-1]
        self._first_loss = losses[0]

    def score(self, cpu_percent: float, bytes_per_interval: float) -> float:
        """復元誤差を返す（大きいほど「普段と違う」）。"""
        if self.model is None:
            raise RuntimeError('fit()を先に呼ぶこと')
        x_np = self._normalize(np.array([[cpu_percent, bytes_per_interval]]))
        x = torch.as_tensor(x_np, dtype=torch.float32)
        with torch.no_grad():
            out = self.model(x)
            err = ((out - x) ** 2).mean(dim=1)
        return float(err.item())

    def is_anomaly(self, cpu_percent: float, bytes_per_interval: float) -> bool:
        return self.score(cpu_percent, bytes_per_interval) > self._threshold

    @property
    def threshold(self) -> float:
        return self._threshold


def _demo() -> None:
    """tools/anomaly_autoencoder.py._demo() と同じシナリオをtorch版で再現。"""
    rng = np.random.default_rng(42)
    normal_cpu = rng.uniform(8, 25, size=200)
    normal_bytes = normal_cpu * 40 + rng.normal(0, 30, size=200)
    samples = list(zip(normal_cpu, normal_bytes))

    model = DeviceAnomalyModelTorch(hidden_dim=3, seed=1)
    model.fit(samples, epochs=500, lr=0.1, threshold_k=4.0)

    print(f'[torch] 学習: loss {model._first_loss:.5f} -> {model._final_loss:.5f}')
    print(f'[torch] 異常判定の閾値(復元誤差): {model.threshold:.6f}')
    print()

    cases = [
        ('正常 (CPU=18%, 通常トラフィック)', 18.0, 18.0 * 40),
        ('ルールベースなら見逃す異常 (CPU=55%, 80%閾値未満)', 55.0, 55.0 * 40),
        ('明白な異常 (CPU=95%)', 95.0, 95.0 * 40),
        ('CPUは正常域だが通信量だけ異常', 15.0, 5000.0),
    ]
    for label, cpu, bw in cases:
        err = model.score(cpu, bw)
        flag = '🚨 異常' if model.is_anomaly(cpu, bw) else '✅ 正常'
        print(f'{flag}  {label}  復元誤差={err:.6f}')


if __name__ == '__main__':
    _demo()
