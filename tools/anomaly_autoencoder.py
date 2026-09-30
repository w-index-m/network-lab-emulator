#!/usr/bin/env python3
"""
装置ごとの「普段のふるまい」を学習するAutoencoderベースの異常検知。

`tools/ai_grafana_autopilot.py` の AnomalyDetector は
CPU >= 80% のような固定閾値のルールベースで、閾値未満の異常
（例：普段10〜25%で推移している装置がある日から55%に張り付く）は
検知できない。これは装置ごとに「正常」が違うため、全装置共通の
閾値では表現しきれない。

このモジュールは装置ごとにAutoencoder（入力を一度圧縮してから
復元するニューラルネット）を学習し、正常な状態は復元誤差が小さく、
学習していない未知のパターンは復元誤差が大きくなる、という
性質を利用して「普段と違う」を検知する。

PyTorch/TensorFlowは使っていない。このサンドボックス環境では
`pip install torch`がCUDA関連パッケージ込みでディスク容量を
超過し、CPU専用ビルドの配布元（download.pytorch.org）はegress
ポリシーでブロックされているため、標準ライブラリ相当の
NumPyのみで前方伝播・誤差逆伝播を手書きした（依存を増やさない
という以上に、この環境では他に選択肢が無かった）。
"""

from __future__ import annotations

import numpy as np


class Autoencoder:
    """1隠れ層のAutoencoder。活性化はtanh、出力層は線形、損失はMSE。

    すべて手書きの前方伝播・逆伝播（バッチ勾配降下）。
    """

    def __init__(self, input_dim: int, hidden_dim: int, seed: int = 0):
        rng = np.random.default_rng(seed)
        # Xavier初期化: 各層の分散を入力次元に応じて揃え、
        # 学習初期に勾配が消失/爆発しないようにする
        scale1 = np.sqrt(2.0 / input_dim)
        scale2 = np.sqrt(2.0 / hidden_dim)
        self.w1 = rng.normal(0, scale1, size=(input_dim, hidden_dim))
        self.b1 = np.zeros(hidden_dim)
        self.w2 = rng.normal(0, scale2, size=(hidden_dim, input_dim))
        self.b2 = np.zeros(input_dim)

    def forward(self, x: np.ndarray):
        z1 = x @ self.w1 + self.b1
        h = np.tanh(z1)
        out = h @ self.w2 + self.b2
        return h, out

    def train_step(self, x: np.ndarray, lr: float) -> float:
        """xを1バッチとして1回分の勾配降下を行い、MSE損失を返す。"""
        h, out = self.forward(x)
        err = out - x
        loss = float(np.mean(err ** 2))

        n = x.shape[0]
        d_out = 2.0 * err / n
        d_w2 = h.T @ d_out
        d_b2 = d_out.sum(axis=0)
        d_h = d_out @ self.w2.T
        d_z1 = d_h * (1.0 - h ** 2)  # tanh'(z1) = 1 - tanh(z1)^2
        d_w1 = x.T @ d_z1
        d_b1 = d_z1.sum(axis=0)

        self.w1 -= lr * d_w1
        self.b1 -= lr * d_b1
        self.w2 -= lr * d_w2
        self.b2 -= lr * d_b2
        return loss

    def reconstruction_error(self, x: np.ndarray) -> np.ndarray:
        """サンプルごとの復元誤差（MSE）を返す。x: (n, input_dim)"""
        _, out = self.forward(x)
        return np.mean((out - x) ** 2, axis=1)


class DeviceAnomalyModel:
    """装置1台ぶんの「正常」を学習し、異常度を判定するラッパー。

    特徴量は [cpu_percent/100, 正規化した通信量] の2次元。
    通信量は装置ごとにスケールが違うため、学習データのmin/maxで
    0〜1に正規化してからAutoencoderに渡す。
    """

    def __init__(self, hidden_dim: int = 3, seed: int = 0):
        self.hidden_dim = hidden_dim
        self.seed = seed
        self.model: Autoencoder | None = None
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
        平均 + threshold_k * 標準偏差。大きいほど「普段のブレ」を
        誤検知しにくくなる代わりに、小さな異常を見逃しやすくなる。
        """
        arr = np.asarray(samples, dtype=float)
        if arr.shape[0] < 4:
            raise ValueError('学習には最低4サンプル必要')
        self._byte_min = float(arr[:, 1].min())
        self._byte_max = float(arr[:, 1].max())
        x = self._normalize(arr)

        self.model = Autoencoder(input_dim=2, hidden_dim=self.hidden_dim,
                                  seed=self.seed)
        losses = []
        for _ in range(epochs):
            losses.append(self.model.train_step(x, lr))

        train_errors = self.model.reconstruction_error(x)
        mean_err = float(np.mean(train_errors))
        std_err = float(np.std(train_errors))
        self._threshold = mean_err + threshold_k * std_err
        self._final_loss = losses[-1]
        self._first_loss = losses[0]

    def score(self, cpu_percent: float, bytes_per_interval: float) -> float:
        """復元誤差を返す（大きいほど「普段と違う」）。"""
        if self.model is None:
            raise RuntimeError('fit()を先に呼ぶこと')
        x = self._normalize(np.array([[cpu_percent, bytes_per_interval]]))
        return float(self.model.reconstruction_error(x)[0])

    def is_anomaly(self, cpu_percent: float, bytes_per_interval: float) -> bool:
        return self.score(cpu_percent, bytes_per_interval) > self._threshold

    @property
    def threshold(self) -> float:
        return self._threshold


def _demo() -> None:
    """普段10〜25%で推移する装置に、ルールベースの閾値(80%)を
    大きく下回る55%の"隠れた異常"が起きたケースを再現するデモ。
    """
    rng = np.random.default_rng(42)
    # 正常時: CPU 8〜25%、通信量はCPUにゆるく相関するトラフィック
    normal_cpu = rng.uniform(8, 25, size=200)
    normal_bytes = normal_cpu * 40 + rng.normal(0, 30, size=200)
    samples = list(zip(normal_cpu, normal_bytes))

    model = DeviceAnomalyModel(hidden_dim=3, seed=1)
    model.fit(samples, epochs=500, lr=0.1, threshold_k=4.0)

    print(f'学習: loss {model._first_loss:.5f} -> {model._final_loss:.5f}')
    print(f'異常判定の閾値(復元誤差): {model.threshold:.6f}')
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
