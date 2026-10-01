"""D-049：证据的 regime 表给每一段带上衰减规则自己的分布，以及能混合的两个矩。

操作者 2026-10-01 裁定「现在就条件化」：M-010 与衰减规则改取当前 regime 的期望。实盘侧要两样东西，只能从
证据跑里来——手放在 registry 旁边的分位数会在下一次构造变更时静默过期（D-026，`WalkForwardResult.summary` 的注释）。

1. **每段的窗口 q10**。窗口就是 `walk_forward` 取 q10 的那些（样本外序列上不重叠的 30 天整窗），
   按**第一根 bar** 的标签归档。按窗口内后来的状态归档，第一周的崩盘会凭自己的结果把窗口挪进 high。
2. **每段的年化均值与波动**。Sharpe 不能按占比相加，均值与二阶矩可以。

这里按独立重算钉住两件事，并钉住「少于 10 个窗口不给 q10」。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.validation.metrics import sharpe, window_sharpes
from beidou_alpha.validation.stability import REGIME_MIN_WINDOWS, regime_split_sharpes

BPY = 8760.0
WINDOW = 720


def _series(windows: int, seed: int = 7) -> tuple[pd.Series, pd.Series]:
    """一条净收益与一条正的、有持续性的波动率；收益的均值随波动率变，这样三段的 Sharpe 真的不同。"""
    rng = np.random.default_rng(seed)
    n = windows * WINDOW + 37  # 末尾留一截不满一窗的尾巴：它不是窗口
    index = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    vol = pd.Series(np.exp(np.cumsum(rng.normal(0.0, 0.02, n)) * 0.5) * 0.7, index=index)
    net = pd.Series(rng.normal(0.0004 - 0.0003 * (vol.to_numpy() - 0.7), 0.01, n), index=index)
    return net, vol


def _independent_labels(net: pd.Series, vol: pd.Series, table: dict[str, dict]) -> dict[pd.Timestamp, str]:
    """按波动率稳定排序后依次切 bars 根——与 `test_each_row_is_the_sharpe_of_exactly_its_bars` 同一种重算。"""
    aligned = pd.concat([net, vol], axis=1, join="inner").dropna()
    order = np.argsort(aligned.iloc[:, 1].to_numpy(), kind="stable")
    labels: dict[pd.Timestamp, str] = {}
    start = 0
    for name in ("low", "mid", "high"):
        for position in order[start : start + table[name]["bars"]]:
            labels[aligned.index[position]] = name
        start += table[name]["bars"]
    return labels


def test_the_windows_are_walk_forwards_own_filed_by_their_first_bar() -> None:
    net, vol = _series(windows=45)
    table = regime_split_sharpes(net, vol, BPY, window_bars=WINDOW)
    labels = _independent_labels(net, vol, table)

    filed: dict[str, list[float]] = {"low": [], "mid": [], "high": []}
    whole = window_sharpes(net, bars_per_window=WINDOW, bars_per_year=BPY)
    for k, value in enumerate(whole):
        assert value is not None
        filed[labels[net.index[k * WINDOW]]].append(value)

    assert sum(row["windows"] for row in table.values()) == len(whole) == 45, "每个整窗恰好归进一段"
    for name, values in filed.items():
        assert table[name]["windows"] == len(values)
        assert table[name]["window_sharpe_q10"] == pytest.approx(float(np.quantile(values, 0.10)), rel=1e-12)


def test_a_tercile_short_of_ten_windows_reports_its_count_and_no_q10() -> None:
    net, vol = _series(windows=18)
    table = regime_split_sharpes(net, vol, BPY, window_bars=WINDOW)
    short = [name for name, row in table.items() if row["windows"] < REGIME_MIN_WINDOWS]
    assert short, "18 个窗口分三段，至少一段不满 10 个"
    for name, row in table.items():
        assert (row["window_sharpe_q10"] is None) == (name in short), (name, row)


def test_without_a_window_length_the_table_is_what_it_was() -> None:
    net, vol = _series(windows=12)
    table = regime_split_sharpes(net, vol, BPY)
    assert all("windows" not in row and "window_sharpe_q10" not in row for row in table.values())


def test_mixing_the_rows_at_their_own_shares_gives_back_the_whole_sample_sharpe() -> None:
    """三段按各自的 bar 占比混合，必须还原整段有标签 bar 的 Sharpe——这正是实盘 M-010 的混合式。"""
    net, vol = _series(windows=30)
    table = regime_split_sharpes(net, vol, BPY, window_bars=WINDOW)
    total = sum(row["bars"] for row in table.values())
    mean = sum(row["bars"] / total * row["mean_annual"] / BPY for row in table.values())
    second = sum(
        row["bars"] / total * ((row["vol_annual"] ** 2) / BPY + (row["mean_annual"] / BPY) ** 2)
        for row in table.values()
    )
    mixed = mean / math.sqrt(second - mean**2) * math.sqrt(BPY)
    labelled = pd.concat([net, vol], axis=1, join="inner").dropna().iloc[:, 0]
    assert mixed == pytest.approx(sharpe(labelled, BPY), rel=1e-3), "ddof 1 对总体方差，差在千分之一以内"
    for row in table.values():
        assert row["mean_annual"] / row["vol_annual"] == pytest.approx(row["sharpe"], rel=1e-12)
