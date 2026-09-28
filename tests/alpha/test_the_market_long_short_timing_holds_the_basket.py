"""择时书 `lsr_timing`：全市场多空比的偏离定一个方向，整个篮子一起持有。

2026-09-28 预登记（`docs/RESEARCH_LOG.md`「预登记：全市场多空比择时 lsr_timing」）。#199 把 LS 叶那 4 个正形状
判成「只在赌方向：择时」，#200 拆开只有 BTC 的那 10 个月，有横截面的那段也在。这里钉住预登记第 3 项写下的
信号，每条对应一句：

1. **M 是挖掘叶自己的读数**：每个币的 `lsr(window)`（多空比对自己过去 window 根均值的偏离）在当时的 population
   上等权平均，逐位相同。population 外的币不进 M，自己没有多空比的币也不进。
2. **反着人群走**：账户比平时更偏多是空，更偏空是多。
3. **只在强读数上翻面**：`|tanh(-M / scale)|` 够 `entry_threshold` 才换方向，其余时候拿着上一个方向，M 读不出
   也拿着。第一个强读数之前没有方向、不持仓，但已经在做决定：分数是原始读数（在线下），回测从 M 有值的那根起算，
   所以网格的各格从同一处起算（validate 只在各格共同的区间上打分）。
4. **整个 population 同一个方向**，包括自己没有多空比的币；两次强读数之间才进 population 的币当即拿到这个方向。
   population 外的币没有分数。
5. **只有方向穿过定仓**：分数的大小在 vol targeting 里被除掉。
6. **与挖掘候选同一条入场线**，网格就是那 4 个形状。

读数只来自过去这一条，由 `test_signal_suite` 与 `test_reference_population` 对每个登记的信号各测一次，这里不重复。
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.mining.expr import Const, ExprError, LongShortRatio, Mul, Squash
from beidou_alpha.mining.search import Candidate, to_signal
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, vol_targeted
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals import SIGNALS
from beidou_alpha.signals.lsr_timing import LsrTimingParams, held_side, lsr_timing_scores, market_deviation
from beidou_cli.research_grids import DEFAULT_GRIDS

BARS = pd.date_range("2024-01-01", periods=480, freq="h", tz="UTC")
SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
WINDOW = 24
PARAMS = LsrTimingParams(window=WINDOW)


def _model(**params: object) -> AlphaModel:
    """The entry research builds (`_entry` merges the signal's defaults), so the model's line is the signal's."""
    entry = StrategyEntry(id="lsr_timing", params={**SIGNALS["lsr_timing"].default_params, **params})
    return AlphaModel(entries=(entry,), portfolio=PortfolioParams(), interval="1h", min_history_bars=0)


def _panel(ratio: pd.DataFrame | None, seed: int = 5) -> Panel:
    """Four correlated perpetuals; the account ratio, when given, is the only metrics column."""
    rng = np.random.default_rng(seed)
    common = rng.normal(0.0, 0.01, len(BARS))
    frames = {}
    for symbol in SYMBOLS:
        close = 100.0 * np.exp(np.cumsum(0.7 * common + 0.3 * rng.normal(0.0, 0.01, len(BARS))))
        frames[symbol] = pd.DataFrame(
            {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1_000.0},
            index=BARS,
        )
    metrics = None if ratio is None else {"count_long_short_ratio": ratio}
    return Panel.from_frames(frames, "1h", metrics=metrics)


def _ratio(levels: dict[int, float], noise: float = 0.0, seed: int = 6) -> pd.DataFrame:
    """Every symbol's account ratio follows one crowd level, ``{first bar: level from then on}``."""
    path = np.full(len(BARS), np.nan)
    for start, level in sorted(levels.items()):
        path[start:] = level
    rng = np.random.default_rng(seed)
    values = path[:, None] * np.exp(rng.normal(0.0, noise, size=(len(BARS), len(SYMBOLS))))
    return pd.DataFrame(values, index=BARS, columns=list(SYMBOLS))


def _membership(symbols: Iterable[str], joins: dict[str, int] | None = None) -> pd.DataFrame:
    mask = pd.DataFrame(False, index=BARS, columns=list(SYMBOLS))
    mask[list(symbols)] = True
    for symbol, bar in (joins or {}).items():
        mask.loc[BARS[bar:], symbol] = True
    return mask


def _members(panel: Panel, symbols: Iterable[str], joins: dict[str, int] | None = None) -> Panel:
    return panel.with_reference(_membership(symbols, joins))


def test_a_crowd_more_long_than_usual_is_a_short_and_every_member_holds_the_same_side() -> None:
    panel = _members(_panel(_ratio({0: 2.0, 200: 3.0, 320: 1.2})), SYMBOLS[:3])
    scores = lsr_timing_scores(panel, PARAMS)
    members = list(SYMBOLS[:3])

    assert scores[members].iloc[:200].isna().all().all()  # a flat ratio reads exactly 0: NaN, not an exit
    assert (scores[members].iloc[200:320] < 0).all().all()  # accounts jumped long: short
    assert (scores[members].iloc[320:] > 0).all().all()  # and fell below their week: long
    assert scores[members].iloc[200:].nunique(axis=1).eq(1).all()
    assert scores["DDDUSDT"].isna().all()  # outside the population: no score at all


def test_the_side_changes_only_on_a_strong_reading_and_is_kept_through_weak_or_missing_ones() -> None:
    raw = pd.Series([np.nan, 0.1, 0.3, 0.1, -0.19, np.nan, -0.25, 0.05, 0.2])
    expected = pd.Series([np.nan, np.nan, 0.3, 0.3, 0.3, 0.3, -0.25, -0.25, 0.2])
    pd.testing.assert_series_equal(held_side(raw, 0.2), expected)


def test_the_deviation_is_the_mining_leafs_own_reading_averaged_over_the_population() -> None:
    ratio = _ratio({0: 2.0}, noise=0.1)
    ratio["DDDUSDT"] = 2.0 * np.exp(np.linspace(0.0, 1.0, len(BARS)))  # far off the others once it is counted
    panel = _members(_panel(ratio), SYMBOLS[:2], joins={"DDDUSDT": 300})
    deviation, names = market_deviation(panel, WINDOW)

    leaf = LongShortRatio(WINDOW).evaluate(panel)
    expected = pd.concat(
        [
            leaf[["AAAUSDT", "BBBUSDT"]].mean(axis=1).iloc[:300],
            leaf[["AAAUSDT", "BBBUSDT", "DDDUSDT"]].mean(axis=1).iloc[300:],
        ]
    )
    pd.testing.assert_series_equal(deviation, expected, check_exact=True, check_names=False)
    assert names.iloc[WINDOW - 1 : 300].eq(2).all() and names.iloc[300:].eq(3).all()


def test_before_the_first_strong_reading_the_book_is_decided_flat_and_every_cell_starts_together() -> None:
    """A NaN there would read as warmup, and validate scores a grid on the range its cells share."""
    panel = _panel(_ratio({0: 2.0, 200: 3.0}, noise=0.02))
    members = list(SYMBOLS[:3])
    quiet = lsr_timing_scores(_members(panel, members), PARAMS)[members]
    assert quiet.iloc[: WINDOW - 1].isna().all().all()
    assert quiet.iloc[WINDOW - 1 : 200].notna().all().all()
    assert quiet.iloc[WINDOW - 1 : 200].abs().lt(PARAMS.entry_threshold).all().all()

    starts = {}
    for scale in (0.5, 1.0, 4.0):  # at 4.0 no reading is ever strong enough
        targets = _model(window=WINDOW, scale=scale).strategy_targets(panel, _membership(members))["lsr_timing"][
            members
        ]
        assert targets.iloc[WINDOW - 1 : 200].eq(0.0).all().all()
        starts[scale] = targets.notna().any(axis=1).idxmax()
    assert set(starts.values()) == {BARS[WINDOW - 1]}


def test_a_member_with_no_ratio_of_its_own_holds_the_side_the_others_set_and_moves_nothing() -> None:
    ratio = _ratio({0: 2.0, 200: 3.0}, noise=0.02)
    without = ratio.copy()
    without["CCCUSDT"] = np.nan
    scores = lsr_timing_scores(_members(_panel(without), SYMBOLS[:3]), PARAMS)
    reference = lsr_timing_scores(_members(_panel(ratio.drop(columns="CCCUSDT")), ["AAAUSDT", "BBBUSDT"]), PARAMS)

    assert scores["CCCUSDT"].iloc[200:].notna().all()
    pd.testing.assert_series_equal(scores["CCCUSDT"], scores["AAAUSDT"], check_names=False)
    pd.testing.assert_series_equal(scores["AAAUSDT"], reference["AAAUSDT"])


def test_a_name_that_joins_between_two_strong_readings_is_held_at_once_not_at_the_next_one() -> None:
    """The reason the side is held here rather than by the model's per-name hold (the module docstring)."""
    panel = _panel(_ratio({0: 2.0, 200: 3.0}))
    targets = _model(window=WINDOW).strategy_targets(panel, _membership(SYMBOLS[:3], joins={"DDDUSDT": 260}))[
        "lsr_timing"
    ]

    assert targets["DDDUSDT"].iloc[:260].isna().all()
    assert targets["DDDUSDT"].iloc[260:].lt(0).all()  # the last strong reading was at bar 200
    pd.testing.assert_series_equal(targets["DDDUSDT"].iloc[260:], targets["AAAUSDT"].iloc[260:], check_names=False)


def test_no_ratio_in_the_population_is_no_score_and_no_archive_at_all_is_an_error() -> None:
    ratio = _ratio({0: 2.0, 200: 3.0})
    ratio[list(SYMBOLS[:3])] = np.nan  # only DDD has a ratio, and DDD is not a member
    assert lsr_timing_scores(_members(_panel(ratio), SYMBOLS[:3]), PARAMS).isna().all().all()
    with pytest.raises(ExprError, match="does not carry"):
        lsr_timing_scores(_members(_panel(None), SYMBOLS[:3]), PARAMS)


def test_only_the_side_reaches_the_weights() -> None:
    panel = _members(_panel(_ratio({0: 2.0, 200: 3.0, 320: 1.2}, noise=0.02)), SYMBOLS)
    scores = lsr_timing_scores(panel, PARAMS)
    held = scores.iloc[200:].stack()  # from the first strong reading; before it the model reads the score as no action
    assert held.abs().min() >= PARAMS.entry_threshold and held.abs().max() < 1.0  # a size to divide out

    params = PortfolioParams()
    sized = vol_targeted(scores, panel.close, panel.bars_per_year, params).iloc[200:]
    signed = vol_targeted(np.sign(scores), panel.close, panel.bars_per_year, params).iloc[200:]
    assert (sized.iloc[-1] != 0.0).all()
    pd.testing.assert_frame_equal(sized, signed, rtol=1e-12, atol=0.0)


def test_it_reads_metrics_and_carries_the_four_shapes_and_their_entry_line() -> None:
    spec = SIGNALS["lsr_timing"]
    assert spec.needs_metrics is not None and spec.needs_metrics({})
    assert spec.warmup_for({"window": 72}) == 72
    assert spec.canonical_params({}) == {"window": 168, "scale": 1.0, "entry_threshold": 0.2}
    # squash(-lsr(w), s) for w in {72, 168} and s in {0.5, 1}: the four shapes #199 ruled on, and no others
    assert DEFAULT_GRIDS["lsr_timing"] == {"window": [72, 168], "scale": [0.5, 1.0]}
    governing = to_signal(Candidate.of(Squash(Mul(Const(-1.0), LongShortRatio(168)), 1.0)))
    assert governing.id == "mined_8a838550a686852d"
    assert spec.default_params["entry_threshold"] == governing.default_params["entry_threshold"]
