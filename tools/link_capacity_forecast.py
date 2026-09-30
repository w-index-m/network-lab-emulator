#!/usr/bin/env python3
"""
インタフェースの実トラフィックカウンタから、帯域逼迫までの時間を
線形回帰で見積もり、OSPFコストの見直しが要る低速リンクを提案する。

`docs/anomaly-autoencoder.md` のAutoencoderが「普段と違う」を検知する
のに対し、このモジュールは「このまま増え続けたらいつ閾値を超えるか」
という**トレンドの外挿**を扱う。両方ともNumPyのみで実装している。

データソースは `engine/protocols.py` の `dp_engine.get_counter()` —
実際にICMP/OSPF Hello等のデータプレーントラフィックがそのIFを
通過するたびに増える、本物の累積カウンタ（`/api/snmp/dashboard`の
`history[].bytes` や ifInOctets/ifOutOctets として外部から見えるもの
と同じ値）。合成データではなく、実際に転送されたバイト数を使う。

想定用途: WANリンクのような低速インタフェース（Si-Rのモデム回線等）
で、負荷が右肩上がりのままだとOSPFの優先経路として使い続けるのは
まずい、という判断材料。1Gbpsのラボ用LANでは事実上飽和しないので、
容量に対して意味のある値が出るのは低速リンクの想定。
"""

from __future__ import annotations

import numpy as np


class RateSample:
    """時刻 `t` における累積バイト数 `cum_bytes` の1サンプル。"""

    __slots__ = ('t', 'cum_bytes')

    def __init__(self, t: float, cum_bytes: int):
        self.t = t
        self.cum_bytes = cum_bytes


def rate_series(samples: list[RateSample]) -> tuple[np.ndarray, np.ndarray]:
    """累積カウンタのサンプル列から、区間ごとの瞬間レート(bps)を作る。

    戻り値: (区間中央の時刻, その区間の平均レートbps) の2本のndarray。
    サンプルが2点未満なら空配列を返す。カウンタが巻き戻る
    （リセットされた等）区間は負のレートになるため除外する。
    """
    if len(samples) < 2:
        return np.array([]), np.array([])
    ts = np.array([s.t for s in samples], dtype=float)
    cum = np.array([s.cum_bytes for s in samples], dtype=float)
    dt = np.diff(ts)
    dbytes = np.diff(cum)
    valid = dt > 0
    rate_bps = np.zeros_like(dt)
    rate_bps[valid] = (dbytes[valid] * 8.0) / dt[valid]
    mid_t = ts[:-1] + dt / 2.0
    keep = valid & (dbytes >= 0)
    return mid_t[keep], rate_bps[keep]


def forecast_seconds_to_threshold(times: np.ndarray, rates_bps: np.ndarray,
                                   capacity_bps: float,
                                   target_utilization: float = 0.8,
                                   now: float | None = None) -> float | None:
    """レートの1次線形トレンドを外挿し、閾値到達までの残り秒数を返す。

    トレンドが横ばい/下降（傾き<=0）なら None
    （このまま増え続けても閾値に到達しない）。
    既に閾値を超えている場合は 0.0 を返す。
    """
    if len(times) < 2:
        return None
    slope, intercept = np.polyfit(times, rates_bps, 1)
    # 完全に横ばいのデータでも浮動小数点誤差でわずかに非ゼロの傾きが
    # 出ることがある（例: 1e-13 bps/秒）。そのまま使うと
    # (threshold-intercept)/slope が天文学的な秒数になってしまうため、
    # 「1秒あたり1bps未満の傾き」は横ばい扱いにする。
    if slope <= 1e-6:
        return None
    threshold = capacity_bps * target_utilization
    t_cross = (threshold - intercept) / slope
    ref_now = now if now is not None else times[-1]
    remaining = t_cross - ref_now
    return max(0.0, float(remaining))


def current_utilization(times: np.ndarray, rates_bps: np.ndarray,
                         capacity_bps: float) -> float:
    """直近サンプルの利用率（0.0〜1.0超もありうる）。"""
    if len(rates_bps) == 0 or capacity_bps <= 0:
        return 0.0
    return float(rates_bps[-1] / capacity_bps)


def analyze_interface(samples: list[RateSample], capacity_bps: float,
                       ospf_cost: int, min_ospf_cost: int = 1,
                       target_utilization: float = 0.8,
                       warn_within_seconds: float = 3600.0) -> dict:
    """1インタフェースぶんの判定結果をまとめる。

    「今 最有力経路（コストが最小相当）に選ばれているのに、
    このまま増え続けると近いうちに逼迫する」場合に
    `recommend_raise_cost=True` を返す。OSPFは最小コスト経路を
    選ぶため、逼迫しそうなリンクのコストを上げれば
    トラフィックを別経路に逃がせる、という前提。
    """
    times, rates = rate_series(samples)
    util = current_utilization(times, rates, capacity_bps)
    eta = forecast_seconds_to_threshold(times, rates, capacity_bps,
                                        target_utilization)
    is_preferred = ospf_cost <= min_ospf_cost
    will_saturate_soon = eta is not None and eta <= warn_within_seconds
    return {
        'current_utilization': util,
        'seconds_to_threshold': eta,
        'is_preferred_path': is_preferred,
        'recommend_raise_cost': is_preferred and will_saturate_soon,
    }


def _demo() -> None:
    """256kbpsのWAN回線を想定し、負荷が右肩上がりのケースを再現する。"""
    rng = np.random.default_rng(0)
    capacity = 256_000  # 256kbps
    t0 = 0.0
    interval = 10.0  # 10秒おきにサンプリング
    n = 30

    # 累積バイト数: 毎区間平均40kbpsぶん転送量が増え、さらに
    # 時間とともに増加傾向（ランプアップ）が乗る
    cum = 0
    samples = []
    for i in range(n):
        t = t0 + i * interval
        ramp_bps = 20_000 + i * 3_000  # 徐々に増える負荷
        noisy_bps = max(0.0, ramp_bps + rng.normal(0, 5_000))
        cum += int(noisy_bps * interval / 8.0)
        samples.append(RateSample(t, cum))

    result = analyze_interface(samples, capacity_bps=capacity, ospf_cost=1,
                               min_ospf_cost=1, target_utilization=0.8,
                               warn_within_seconds=600.0)
    print(f'現在の利用率: {result["current_utilization"]*100:.1f}%')
    if result['seconds_to_threshold'] is not None:
        print(f'80%到達まで: 約{result["seconds_to_threshold"]:.0f}秒後'
              f'（現在のトレンドを線形外挿）')
    else:
        print('80%到達の見込みなし（トレンドが横ばい/下降）')
    print(f'最有力経路(OSPFコスト最小)か: {result["is_preferred_path"]}')
    print(f'コスト見直しを推奨: {result["recommend_raise_cost"]}')


if __name__ == '__main__':
    _demo()
