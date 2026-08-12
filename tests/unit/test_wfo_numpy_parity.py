import math
import random

import numpy as np

from beidou_research.mining.runner import _compute_ic, _compute_ic_np, _compute_sharpe, _compute_sharpe_np


def _series(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(n)]


def test_ic_numpy_matches_python() -> None:
    for seed in range(5):
        preds = _series(200, seed)
        rets = _series(200, seed + 100)
        py = _compute_ic(preds, rets)
        npv = _compute_ic_np(np.asarray(preds, dtype=np.float64), np.asarray(rets, dtype=np.float64))
        assert math.isclose(py, npv, rel_tol=1e-12, abs_tol=1e-12), (seed, py, npv)


def test_sharpe_numpy_matches_python() -> None:
    for seed in range(5):
        rets = _series(200, seed)
        py = _compute_sharpe(rets)
        npv = _compute_sharpe_np(np.asarray(rets, dtype=np.float64))
        assert math.isclose(py, npv, rel_tol=1e-12, abs_tol=1e-12), (seed, py, npv)


def test_numpy_handles_degenerate_inputs() -> None:
    # 全零方差 → 与纯 Python 相同返回 0.0
    zeros = np.zeros(50, dtype=np.float64)
    assert _compute_ic_np(zeros, zeros) == 0.0
    assert _compute_sharpe_np(zeros) == 0.0
