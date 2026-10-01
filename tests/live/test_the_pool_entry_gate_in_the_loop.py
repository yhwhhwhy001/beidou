"""The pool entry gate's live half: at the daily refresh the loop asks which candidates the book could open.

Operator ruling 2026-09-30 ("faithful" option).  The book's risk per name comes from the newest cycle row decided
before the refresh day, each candidate's sigma from its closed bars before it - the same bar research reads for
the same refresh (`beidou_cli.research_pool_gate`).  A candidate whose would-be weight ``risk / sigma`` misses
`entry_line` is left out of the selection, and the reading goes into the cycle's ``universe_update``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, asset_vol, per_name_risk
from beidou_data.pool import UniverseUpdate
from beidou_live.engine import LiveEngine
from beidou_live.state import StateStore
from beidou_shared.types import InstrumentRules
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import SYMBOLS, _config, _model, _prices


class GatedPool:
    """Stands in for `LivePool.select`: every candidate is measured, and the engine's gate is asked about all."""

    def __init__(self, candidates: Sequence[str]) -> None:
        self.candidates = list(candidates)
        self.asked: list[list[str]] = []

    async def select(
        self,
        previous: Sequence[str],
        rules: Mapping[str, InstrumentRules],
        *,
        gate: Callable[[list[str]], Awaitable[Collection[str]]] | None = None,
    ) -> UniverseUpdate:
        blocked: set[str] = set()
        if gate is not None:
            self.asked.append(sorted(self.candidates))
            blocked = set(await gate(sorted(self.candidates)))
        symbols = tuple(symbol for symbol in self.candidates if symbol not in blocked)
        before, after = set(previous), set(symbols)
        gated = tuple(sorted(blocked))
        return UniverseUpdate(symbols, tuple(sorted(after - before)), tuple(sorted(before - after)), 1, {}, gated)


def _engine(august_panel: Panel, tmp_path: Path, pool: GatedPool, gate: bool) -> tuple[LiveEngine, FakeMarketData, int]:
    last = next(p for p in range(350, len(august_panel.index)) if pd.Timestamp(august_panel.index[p]).hour == 23)
    market = FakeMarketData(august_panel, last + 1)
    config = _config(tmp_path, universe_refresh=True, portfolio=PortfolioParams(pool_entry_gate=gate))
    engine = LiveEngine(
        config,
        model=_model(),
        market=market,
        venue=FakeVenue(balance=10_000.0, prices=_prices(august_panel, last + 1)),
        clock=FakeClock(market.bar_open_ms(last + 1) + 5_000),
        store=StateStore(tmp_path / "live"),
        pool=pool,
    )
    return engine, market, last


async def test_the_refresh_leaves_out_a_name_whose_would_be_weight_misses_the_line(
    august_panel: Panel, tmp_path: Path
) -> None:
    pool = GatedPool(SYMBOLS)
    engine, market, last = _engine(august_panel, tmp_path, pool, gate=True)
    await engine.startup()

    # 23:00: nothing decided before this day yet, so the gate cannot read a book - keep the universe (T-P05).
    first = await engine.run_cycle(market.bar_open_ms(last))
    assert "no cycle row" in str(first["universe_update"].get("error")), first["universe_update"]
    assert list(engine.universe) == SYMBOLS

    # Place the entry line between the two smallest would-be weights, read the way research reads them.
    row = [r for r in engine.store.read_jsonl(engine.store.cycles_path) if r.get("book_weights")][-1]
    stamp = pd.DatetimeIndex([pd.Timestamp(int(row["as_of_ms"]), unit="ms", tz="UTC")])
    risk = per_name_risk(
        pd.DataFrame([row["book_weights"]["main"]], index=stamp), pd.DataFrame([row["asset_vol"]], index=stamp)
    )
    close = august_panel.close[SYMBOLS].iloc[: last + 1].tail(engine.history_bars)
    sigma = asset_vol(close, engine.config.portfolio, august_panel.bars_per_year).iloc[-1]
    would_be = (float(risk.iloc[0]) / sigma).sort_values()
    line = float(would_be.iloc[:2].mean())
    engine.config = replace(engine.config, portfolio=replace(engine.config.portfolio, no_trade_band=line))

    market.cursor = last + 2
    second = await engine.run_cycle(market.bar_open_ms(last + 1))  # 00:00, the new day's first cycle
    update = second["universe_update"]
    reading = update["gate"]
    assert pool.asked[-1] == sorted(SYMBOLS), "every measured candidate is judged"
    assert reading["book_risk"] == pytest.approx(float(risk.iloc[0]))
    assert reading["line"] == pytest.approx(line)
    for symbol, value in would_be.items():
        assert reading["would_be"][symbol] == pytest.approx(value, rel=0.02), symbol
    missed = sorted(symbol for symbol, value in reading["would_be"].items() if value < line)
    assert missed and update["gated"] == missed, "exactly the names below the line are left out"
    assert set(engine.universe) == set(SYMBOLS) - set(missed)


async def test_off_the_pool_is_never_handed_a_gate(august_panel: Panel, tmp_path: Path) -> None:
    pool = GatedPool(SYMBOLS)
    engine, market, last = _engine(august_panel, tmp_path, pool, gate=False)
    await engine.startup()
    record: dict[str, Any] = await engine.run_cycle(market.bar_open_ms(last))
    assert pool.asked == [] and "gate" not in record["universe_update"]
    assert list(engine.universe) == SYMBOLS


def test_the_ledger_and_the_loop_record_the_gate_only_when_it_is_on() -> None:
    """Off, the ledger's construction digest and the loop's fingerprint read as they did before the field existed.

    On, both move, with no alias: the gate decides which names the book holds, which is a construction change.
    """
    from types import SimpleNamespace

    from beidou_cli.research_ledger_io import _construction_digest
    from beidou_live.engine import construction_fingerprint, evidence_construction_of
    from tests.live.helpers_construction import live_config_for_profile

    cost = SimpleNamespace(fee_bps=5.0, slippage_bps=2.0)
    off, on = PortfolioParams().__dict__, PortfolioParams(pool_entry_gate=True).__dict__
    without = {key: value for key, value in off.items() if key != "pool_entry_gate"}
    assert _construction_digest(off, cost, "next_open") == _construction_digest(without, cost, "next_open")
    assert _construction_digest(on, cost, "next_open") != _construction_digest(without, cost, "next_open")

    shipped = live_config_for_profile()
    gated = replace(shipped, portfolio=replace(shipped.portfolio, pool_entry_gate=True))
    assert "pool_entry_gate" not in construction_fingerprint(shipped)["portfolio"]
    assert construction_fingerprint(gated)["portfolio"]["pool_entry_gate"] is True
    assert construction_fingerprint(gated)["digest"] != construction_fingerprint(shipped)["digest"]
    assert evidence_construction_of(gated) != evidence_construction_of(shipped)
