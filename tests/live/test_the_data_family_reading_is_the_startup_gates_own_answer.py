"""日报「数据族 parity」一节的判定，就是实盘启动门的判定（WP-A1，M-PR06）。

读数是：每个候选数据族（`METRICS_COLUMNS` 的每一列，加 spot），实盘记录下的 bars 除以启动门对它的读者要的 bars。
这个数有用的前提是它和门说的是同一件事。报告说「够」而门拒绝启动，或者反过来，这一节就成了第二份意见，
而且会随两边各自改动而漂开。所以这里不重述门的算式，而是把门本身跑起来对答案：

一、门本身。同一个合成 store、同一个模型，`LiveEngine.startup()` 拒绝启动，当且仅当这一节说「不够」；
    门的拒绝消息里的两个数（覆盖多少 bars、要多少 bars）就是这一节印的两个数。请求窗口分两种情形各量一次：
    由 `market_data.history_bars` 这个下限决定，与由读者自己的 warmup 决定。最薄的那个币只在 `leaving` 里，
    所以这一节少数了 `leaving` 也会在这里变红。
二、停掉的 probe book 带走它的读者，和引擎启动时一样。
三、没有在跑的读者时，这一节按挖掘叶的 lookback 给门算一个数。算式里抄了 `AlphaModel.warmup_bars` 的
    「取大再加一」一行，这里拿一个真的、多挂了一个读者的 `AlphaModel` 把它钉住。
四、store 不在读成「不可读」，不读成 0，也不崩。没有模型同样。
五、一列有空洞时，括号里只数这一列有值的桶，判定仍按门的桶数。实盘里 long/short 列就是这样：
    2026-09-12 才开始记，比 open interest 晚五天。
六、日报印这一节，但告警一条不多。
七、请求窗口的下限按 `live_config` 的读法读 profile。`ReplayInputs` 里那一行是抄的，这里钉住。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.backtest import CostModel
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import MAIN_BOOK, StrategyEntry
from beidou_alpha.signals.tsmom import TsmomParams
from beidou_data.metrics_snapshot import live_coverage_bars
from beidou_data.store import MetricsStore
from beidou_live.composition import load_registry
from beidou_live.config import live_config
from beidou_live.engine import LiveConfig, LiveEngine
from beidou_live.execution_fidelity import ReplayInputs
from beidou_live.guards import GuardParams
from beidou_live.inputs import required_history
from beidou_live.rebalancer import RebalanceParams
from beidou_live.report_data import _data_family_lines, data_family_parity
from beidou_live.reports import daily_alerts, daily_markdown, daily_payload
from beidou_live.state import LiveState, StateStore
from beidou_shared.config import load_yaml
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")  # 都在 `DEFAULT_RULES` 里，启动时一个不掉
LEAVING = "BNBUSDT"
BUCKETS_PER_BAR = 12  # 5m 的桶，1h 的 bar
FLOOR = 300
START_MS = 1_788_000_000_000 - 1_788_000_000_000 % 3_600_000


def _model(*, reader_window: int | None, min_history: int = 0, reader_book: str = MAIN_BOOK) -> AlphaModel:
    """一个小 tsmom（warmup 52）；`reader_window` 给了就再挂一个读 metrics 的 `lsr_timing`。"""
    tsmom = TsmomParams(vol_window=100).__dict__ | {
        "horizons": [5, 20, 50],
        "horizon_weights": [0.2, 0.3, 0.5],
        "entry_threshold": 0.05,
    }
    entries = [StrategyEntry("tsmom", params=tsmom)]
    if reader_window is not None:
        params = {"window": reader_window, "entry_threshold": 0.2}
        entries.append(StrategyEntry("lsr_timing", params=params, book=reader_book))
    return AlphaModel(
        entries=tuple(entries),
        portfolio=PortfolioParams(covariance_halflife=48, vol_halflife=24, max_weight=0.15, max_gross=0.6),
        interval="1h",
        min_history_bars=min_history,
        books={} if reader_book == MAIN_BOOK else {reader_book: 0.5},
    )


def _record(root: Path, bars: int, *, symbols: Sequence[str] = SYMBOLS, long_short_from_bar: int = 0) -> None:
    """每个币 `bars` 根 bar 的桶；long/short 列在 `long_short_from_bar` 之前是 NaN（晚开始记的那一列）。"""
    store = MetricsStore(root, kind="metrics_snapshot")
    n = bars * BUCKETS_PER_BAR
    for symbol in symbols:
        frame = pd.DataFrame(
            {
                "open_time": [START_MS + 300_000 * i for i in range(n)],
                "symbol": symbol,
                "sum_open_interest": 1.0e6,
                "count_long_short_ratio": [
                    np.nan if i < long_short_from_bar * BUCKETS_PER_BAR else 2.0 for i in range(n)
                ],
            }
        )
        store.append(symbol, frame)


def _state(directory: Path, *, leaving: Sequence[str] = (), stopped: Sequence[str] = ()) -> StateStore:
    store = StateStore(directory)
    universe = [symbol for symbol in SYMBOLS if symbol not in leaving]
    store.save(LiveState(universe=universe, leaving=list(leaving), stopped_books={book: {} for book in stopped}))
    return store


def _fidelity(model: AlphaModel | None, root: Path, *, floor: int = FLOOR) -> ReplayInputs:
    return ReplayInputs(model, CostModel(), None, None, str(root), None, history_bars=floor)


def _rows(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assert block["readable"], block
    return {row["column"]: row for row in block["families"]}


def _engine(panel: Panel, tmp_path: Path, model: AlphaModel) -> LiveEngine:
    """引擎读 `tmp_path/live` 里的状态，与这一节读的那份分开放：两边各自从自己的输入走到同一个数。"""
    cursor = 400
    market = FakeMarketData(panel, cursor)
    config = LiveConfig(
        interval="1h",
        history_bars=FLOOR,
        universe=SYMBOLS,
        leverage=2,
        rebalance=RebalanceParams(no_trade_band=0.002),
        guards=GuardParams(),
        kill_switch_path=tmp_path / "KILL_SWITCH",
        strategy_weights={entry.id: 1.0 for entry in model.entries},
        poll_interval_seconds=0.0,
        grace_seconds=1.0,
    )
    return LiveEngine(
        config,
        model=model,
        market=market,
        venue=FakeVenue(balance=10_000.0),
        clock=FakeClock(market.bar_open_ms(cursor) + 5_000),
        store=StateStore(tmp_path / "live"),
        metrics_store=MetricsStore(tmp_path / "data", kind="metrics_snapshot"),
    )


# --- 一、门本身 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reader_window", "short_by"),
    [(3, 1), (3, 0), (400, 1), (400, 0)],
    ids=[
        "floor-sets-the-window-short",
        "floor-sets-the-window-enough",
        "reader-sets-it-short",
        "reader-sets-it-enough",
    ],
)
async def test_the_section_says_not_enough_exactly_when_the_startup_gate_refuses(
    august_panel: Panel, tmp_path: Path, reader_window: int, short_by: int
) -> None:
    model = _model(reader_window=reader_window)
    engine = _engine(august_panel, tmp_path, model)
    asked = engine.history_bars  # 门交给 `metrics_refusal` 的 required_bars
    assert asked == (FLOOR if reader_window == 3 else reader_window + 1), "两种情形真的各由下限与读者决定"
    held = asked - short_by
    _record(tmp_path / "data", held + 5, symbols=[s for s in SYMBOLS if s != LEAVING])
    _record(tmp_path / "data", held, symbols=[LEAVING])  # 最薄的币只在 `leaving` 里

    state = _state(tmp_path / "report-state", leaving=[LEAVING])
    rows = _rows(data_family_parity(state, tmp_path / "data", _fidelity(model, tmp_path / "data")))

    for column in ("sum_open_interest", "count_long_short_ratio"):
        row = rows[column]
        assert (row["held_bars"], row["required_bars"]) == (held, asked), row
        assert row["readers"] == {"lsr_timing": reader_window}, "读者按 registry 参数取 warmup"
        assert row["enough"] is (short_by == 0), row
    assert held == live_coverage_bars(
        engine.metrics_store, engine.managed_symbols(), interval_ms=engine.config.interval_ms
    )
    if short_by:
        with pytest.raises(RuntimeError, match="need metrics") as refused:
            await engine.startup()
        assert f"covers {held} of the {asked} bars" in str(refused.value), "门的两个数就是这一节的两个数"
        assert "lsr_timing" in str(refused.value)
    else:
        await engine.startup()  # 门放行：这一节说够，门也说够


# --- 二、停掉的 book ----------------------------------------------------------------------------------------


def test_a_stopped_book_takes_its_reader_with_it_as_the_engine_does(august_panel: Panel, tmp_path: Path) -> None:
    model = _model(reader_window=400, reader_book="probe")  # 挂着时窗口 401；停掉之后回到下限 300
    _state(tmp_path / "live", stopped=["probe"])
    engine = _engine(august_panel, tmp_path, model)
    _record(tmp_path / "data", 350)

    state = _state(tmp_path / "report-state", stopped=["probe"])
    rows = _rows(data_family_parity(state, tmp_path / "data", _fidelity(model, tmp_path / "data")))

    assert engine.strategies_needing_metrics() == [] and engine.history_bars == FLOOR
    for column in ("sum_open_interest", "count_long_short_ratio"):
        assert (rows[column]["readers"], rows[column]["required_bars"]) == ({}, engine.history_bars), rows[column]
        assert rows[column]["enough"] is True


# --- 三、没有在跑的读者 --------------------------------------------------------------------------------------


def test_with_no_running_reader_the_leaf_is_priced_as_the_gate_would_price_it(tmp_path: Path) -> None:
    """lookback 由挖掘叶给（open interest 169、long/short 168、basis 1），门的算式由一个真模型给。"""
    model = _model(reader_window=None, min_history=720)
    _record(tmp_path, 500)

    block = data_family_parity(_state(tmp_path / "state"), tmp_path, _fidelity(model, tmp_path, floor=400))
    rows = _rows(block)

    assert block["request_window"] == required_history(model, 400) == 772
    for column, leaf in (("sum_open_interest", 169), ("count_long_short_ratio", 168)):
        row = rows[column]
        assert (row["readers"], row["leaf_lookback"]) == ({}, leaf), row
        # 同一个 lookback 的读者真挂进模型：`lsr_timing` 的 warmup 就是它的 window。
        real = replace(model, entries=(*model.entries, StrategyEntry("lsr_timing", params={"window": leaf})))
        assert row["required_bars"] == required_history(real, 400) == 720 + leaf + 1, row
        assert (row["held_bars"], row["enough"]) == (500, False), row
    spot = rows["spot_close"]
    assert (spot["held_bars"], spot["column_bars"], spot["enough"]) == (0, None, False), "实盘循环没有 spot 源"
    assert spot["required_bars"] == 772, "basis 只要 1 根，抬不动模型自己的窗口"
    lines = _data_family_lines(block)
    assert all("没有在跑的读者" in lines[column] for column in rows), lines
    assert "实盘循环没有 spot 源" in lines["spot_close"] and "0.00，不够" in lines["spot_close"], lines


# --- 四、读不到 ------------------------------------------------------------------------------------------


def test_a_missing_store_reads_unreadable_rather_than_zero(tmp_path: Path) -> None:
    state = _state(tmp_path / "state")
    block = data_family_parity(state, tmp_path / "nowhere", _fidelity(_model(reader_window=None), tmp_path))
    rows = _rows(block)

    for column in ("sum_open_interest", "count_long_short_ratio"):
        assert (rows[column]["held_bars"], rows[column]["ratio"], rows[column]["enough"]) == (None, None, None)
        assert "metrics_snapshot" in rows[column]["why"], rows[column]
        assert _data_family_lines(block)[column].startswith("覆盖不可读（没有 "), _data_family_lines(block)

    blind = data_family_parity(state, tmp_path, None)
    assert blind == {"readable": False, "why": "没有模型：调用方没有传 registry 与 profile"}
    assert _data_family_lines(blind)["status"] == "不可读"
    broken = replace(_fidelity(None, tmp_path), problem="KeyError: 'x'")
    assert data_family_parity(state, tmp_path, broken)["why"] == "KeyError: 'x'", "from_profile 说了为什么，照印"
    (tmp_path / "corrupt").mkdir()
    (tmp_path / "corrupt" / "state.json").write_text("{", encoding="utf-8")
    corrupt = data_family_parity(
        StateStore(tmp_path / "corrupt"), tmp_path, _fidelity(_model(reader_window=3), tmp_path)
    )
    assert corrupt["readable"] is False and "state.json" in corrupt["why"], "没有 universe 就没有最薄的币可数"


# --- 五、一列有空洞 ----------------------------------------------------------------------------------------


def test_a_column_that_started_late_reads_its_own_bars_beside_the_gates_count(tmp_path: Path) -> None:
    model = _model(reader_window=3)  # 窗口 300
    _record(tmp_path, 320, long_short_from_bar=60)

    rows = _rows(data_family_parity(_state(tmp_path / "state"), tmp_path, _fidelity(model, tmp_path)))

    assert (rows["sum_open_interest"]["held_bars"], rows["sum_open_interest"]["column_bars"]) == (320, 320)
    long_short = rows["count_long_short_ratio"]
    assert (long_short["held_bars"], long_short["column_bars"]) == (320, 260), "门数桶，不看这一列有没有值"
    assert long_short["enough"] is True, "判定是门的：门在这里会放行，这一节不另立一道更严的门"


# --- 六、日报 ----------------------------------------------------------------------------------------------


def test_the_daily_report_prints_it_and_never_pages_on_it(tmp_path: Path) -> None:
    _record(tmp_path / "data", 10)
    payload = daily_payload(
        _state(tmp_path / "state"),
        "2026-09-28",
        data_root=tmp_path / "data",
        fidelity=_fidelity(_model(reader_window=None), tmp_path / "data"),
    )

    markdown = daily_markdown(payload)
    assert "## 数据族 parity（M-PR06，只报告）" in markdown
    assert "| count_long_short_ratio | 覆盖 10（本列有值 10）/ 需要 300 bars = 0.03，不够；没有在跑的读者；" in markdown
    without = {key: value for key, value in payload.items() if key != "data_family_parity"}
    assert daily_alerts(payload) == daily_alerts(without), "只报告：不够也不推送"


# --- 七、请求窗口的下限 ------------------------------------------------------------------------------------


def test_the_window_floor_is_read_the_way_the_loop_reads_it(tmp_path: Path) -> None:
    profile = load_yaml("config/live.demo.yaml")
    registry = load_registry(profile["registry"])
    market = {key: value for key, value in profile["market_data"].items() if key != "history_bars"}
    for variant in (
        profile,
        {**profile, "market_data": market},
        {**profile, "market_data": {**market, "history_bars": 2000}},
    ):
        inputs = ReplayInputs.from_profile(variant, registry, tmp_path)
        assert inputs.problem is None, inputs.problem
        assert inputs.history_bars == live_config(variant, [], registry, dry_run=True).history_bars
