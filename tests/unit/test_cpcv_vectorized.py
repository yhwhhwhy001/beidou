"""CPCV 向量化等价性验证。

`_purge_and_embargo_mask` 与 `_default_ic` 的 numpy 实现必须与
纯 Python 参考实现语义严格等价(逐元素一致,浮点允许 ulp 级误差)。
参考实现为测试专用副本,独立于生产代码。
"""

from __future__ import annotations

import math
import random

import numpy as np

from beidou_research.mining.evaluation.cpcv import (
    _default_ic,
    _purge_and_embargo_mask,
)


# ================================================================
# 纯 Python 参考实现(测试专用,不得 import 生产代码)
# ================================================================


def _ref_barrier_excluded(
    train_indices: list[int],
    test_indices: list[int],
    purge_bars: int,
    embargo_bars: int,
) -> list[bool]:
    test_set = set(test_indices)

    def _overlaps(index: int) -> bool:
        return (purge_bars > 0 and any(abs(index - t) <= purge_bars for t in test_set)) or (
            embargo_bars > 0 and any(0 < index - t <= embargo_bars for t in test_set)
        )

    return [not _overlaps(i) for i in train_indices]


def _ref_ic(predictions: list[float], returns: list[float]) -> float:
    n = min(len(predictions), len(returns))
    if n < 3:
        return 0.0
    p = predictions[:n]
    r = returns[:n]
    mp = sum(p) / n
    mr = sum(r) / n
    cov = sum((p[i] - mp) * (r[i] - mr) for i in range(n)) / (n - 1)
    sp = (sum((x - mp) ** 2 for x in p) / (n - 1)) ** 0.5
    sr = (sum((x - mr) ** 2 for x in r) / (n - 1)) ** 0.5
    if sp == 0 or sr == 0:
        return 0.0
    return cov / (sp * sr)


# ================================================================
# 等价性测试
# ================================================================


def _random_indices(seed: int, n: int, lo: int, hi: int) -> list[int]:
    rng = random.Random(seed)
    return sorted(rng.sample(range(lo, hi), n))


def test_purge_embargo_mask_matches_reference():
    cases = [
        # (train, test, purge, embargo)
        (_random_indices(1, 200, 0, 800), _random_indices(2, 40, 0, 800), 2, 2),
        (_random_indices(3, 150, 0, 500), [0, 1, 100, 101, 102], 1, 0),
        (_random_indices(4, 100, 0, 500), [0, 1, 100, 101, 102], 0, 3),
        (_random_indices(5, 100, 0, 500), [0, 1, 100, 101, 102], 0, 0),
        ([i for i in range(300)], [i for i in range(0, 300, 7)], 5, 5),
        ([0, 1, 2, 3, 4], [2], 1, 0),
        ([0, 1, 2, 3, 4], [2], 0, 2),
        ([], [2], 1, 1),
        ([10, 20, 30], [], 1, 1),
    ]
    for train, test, purge, embargo in cases:
        mask = _purge_and_embargo_mask(train, test, purge, embargo)
        assert isinstance(mask, np.ndarray)
        assert mask.dtype == bool
        assert mask.tolist() == _ref_barrier_excluded(train, test, purge, embargo), (
            train,
            test,
            purge,
            embargo,
        )


def test_default_ic_matches_reference_on_random_data():
    rng = random.Random(99)
    for _ in range(20):
        n = rng.randint(3, 200)
        p = [rng.gauss(0.0, 1.0) for _ in range(n)]
        r = [rng.gauss(0.0, 1.0) for _ in range(n)]
        got = _default_ic(p, r)
        want = _ref_ic(p, r)
        assert math.isclose(got, want, rel_tol=1e-12, abs_tol=1e-12)


def test_default_ic_edge_cases_unchanged():
    # 样本不足
    assert _default_ic([1.0, 2.0], [2.0, 1.0]) == 0.0
    # 零方差 → 0.0
    assert _default_ic([1.0] * 10, [float(i) for i in range(10)]) == 0.0
    assert _default_ic([float(i) for i in range(10)], [1.0] * 10) == 0.0
    # 完美正相关 → 1.0
    x = [float(i) for i in range(50)]
    assert math.isclose(_default_ic(x, x), 1.0, rel_tol=1e-12)
    # 完美负相关 → -1.0
    assert math.isclose(_default_ic(x, [-v for v in x]), -1.0, rel_tol=1e-12)
    # 长度不一致时截断到较短者
    got = _default_ic([0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2])
    want = _ref_ic([0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2])
    assert math.isclose(got, want, rel_tol=1e-12)
    # NaN 传播(调用方以 _is_finite 过滤)
    got_nan = _default_ic([float("nan"), 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert math.isnan(got_nan)
