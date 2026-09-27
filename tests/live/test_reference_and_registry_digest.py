"""P1-01 live wiring + DL-Q0: the loop declares its population, and says which registry it loaded.

Two defects, one restart:

* P1-01 - the engine handed the model whatever symbols it had fetched, so the
  cross-sectional population was an accident of the request rather than the
  universe.  ``run_cycle`` must pass ``reference_symbols`` explicitly.
* DL-Q0 / KILL-Q15 - the process builds its model once at startup and never
  reloads it, so editing ``config/alpha_registry.yaml`` changes what the *file*
  says without changing what the loop *trades*.  On 2026-09-04 that gap opened
  for real: the loop started at 17:21Z, the commit that re-enabled the funding
  crowding modifier landed at 20:03Z, and for the next 93 cycles the registry
  said ``crowding_window: 72`` while the process ran 0 - with no instrument
  anywhere able to say so.  Every cycle must record the digest of the registry
  it is actually running.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beidou_alpha.panel import Panel
from beidou_governance.policy import policy_digest
from beidou_live.engine import LiveEngine
from beidou_live.report_common import _cycles
from beidou_live.state import StateStore
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import SYMBOLS, _config, _model, _prices


def _engine(panel: Panel, tmp_path: Path, cursor: int = 400) -> tuple[LiveEngine, FakeMarketData, StateStore]:
    market = FakeMarketData(panel, cursor)
    venue = FakeVenue(balance=10_000.0, prices=_prices(panel, cursor))
    clock = FakeClock(market.bar_open_ms(cursor) + 5_000)
    store = StateStore(tmp_path / "live")
    engine = LiveEngine(_config(tmp_path), model=_model(), market=market, venue=venue, clock=clock, store=store)
    return engine, market, store


class _RecordingModel:
    """Wraps the real model and remembers what population the engine declared."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.seen: list[list[str] | None] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def targets(self, bars, funding, previous=None, funding_history=None, reference_symbols=None):  # type: ignore[no-untyped-def]
        self.seen.append(None if reference_symbols is None else list(reference_symbols))
        return self.inner.targets(
            bars, funding, previous=previous, funding_history=funding_history, reference_symbols=reference_symbols
        )


@pytest.mark.asyncio
async def test_cycle_declares_the_managed_universe_as_the_population(august_panel: Panel, tmp_path: Path) -> None:
    """P1-01: the population is the universe the loop manages, not the symbols it happened to fetch."""
    engine, market, _store = _engine(august_panel, tmp_path)
    recording = _RecordingModel(engine.model)
    engine.model = recording  # type: ignore[assignment]
    await engine.startup()

    await engine.run_cycle(market.bar_open_ms(399))

    assert recording.seen == [SYMBOLS]


@pytest.mark.asyncio
async def test_cycle_records_the_registry_digest_it_is_running(august_panel: Panel, tmp_path: Path) -> None:
    """DL-Q0: a cycle says which registry it loaded, so a divergence is visible in the record."""
    engine, market, store = _engine(august_panel, tmp_path)
    await engine.startup()

    record = await engine.run_cycle(market.bar_open_ms(399))

    assert isinstance(record["registry"], str) and len(record["registry"]) == 12
    heartbeat = store.read_heartbeat()
    assert heartbeat is not None and heartbeat["registry"] == record["registry"]


@pytest.mark.asyncio
async def test_the_digest_changes_when_the_running_parameters_change(august_panel: Panel, tmp_path: Path) -> None:
    """The instrument has to be load-bearing: a different configuration is a different digest.

    KILL-Q15's exact shape - ``crowding_window`` 0 against 72 - must produce two
    different digests, or the record cannot tell the two apart.
    """
    from dataclasses import replace

    from beidou_live.engine import registry_digest

    engine, _market, _store = _engine(august_panel, tmp_path)
    base = engine.model
    entry = base.entries[0]
    switched = replace(base, entries=(replace(entry, params={**entry.params, "crowding_window": 72}),))

    assert registry_digest(base) != registry_digest(switched)
    assert registry_digest(base) == registry_digest(base)


@pytest.mark.asyncio
async def test_a_cycle_records_which_governance_thresholds_it_is_running(august_panel: Panel, tmp_path: Path) -> None:
    """R9: KILL-Q15's instrument, pointed at the rules instead of at the registry.

    KILL-Q15 was a registry edited on disk while the loop held the old model for 96 cycles.  A policy
    edited on disk would be the same failure with promotions attached, and the difference is that
    nobody would be looking - so the digest goes in the record before anything acts on the policy,
    not after.  Recorded, never read by the loop.
    """
    engine, market, _store = _engine(august_panel, tmp_path)
    await engine.startup()

    record = await engine.run_cycle(market.bar_open_ms(399))

    assert record["governance"] == policy_digest()
    assert record["governance"] != record["registry"], "two different instruments must not print one number"


@pytest.mark.asyncio
async def test_a_cycle_records_the_construction_a_report_could_describe(august_panel: Panel, tmp_path: Path) -> None:
    """DL-G9: the evidence-side digest every cycle, and the full construction once per process.

    Once per process is once per possible change - a construction can only move at startup - and it is
    what the Phase 0 replay was missing when it found six construction changes and could attribute none.
    """
    engine, market, _store = _engine(august_panel, tmp_path)
    await engine.startup()

    first = await engine.run_cycle(market.bar_open_ms(399))
    second = await engine.run_cycle(market.bar_open_ms(400))

    assert isinstance(first["evidence_construction"], str) and len(first["evidence_construction"]) == 16
    assert second["evidence_construction"] == first["evidence_construction"]
    assert "construction_full" in first, "the payload has to be written at least once, or it is unrecoverable"
    assert "construction_full" not in second, "writing it every cycle would bloat the record for no gain"
    assert first["construction_full"]["digest"] == first["construction"]


@pytest.mark.asyncio
async def test_a_first_cycle_that_fails_after_building_its_row_does_not_spend_the_payload(
    august_panel: Panel, tmp_path: Path
) -> None:
    """DL-G9's "once per process" is once in the RECORD, not once per row the cycle assembled.

    The flag used to go up the moment `run_cycle` had built the row, before the orders, `_quarantine`,
    `_summarize` and `_finish_cycle`.  A raise in any of those wrote an ERROR row - which copies only
    `soak.DECISION_KEYS` - and left a flag saying the payload was written, so no later cycle of the
    process wrote it and the construction it ran had no payload anywhere in `cycles.jsonl`.

    The payload belongs on the next completed row, not on the ERROR row: `report_common._cycles` drops
    rows without equity, so `_liquidity_to_close` would price this process's closes on the knobs of the
    previous process's payload.
    """
    engine, market, store = _engine(august_panel, tmp_path)
    await engine.startup()
    reached: list[int] = []

    async def boom(_reports: object) -> list[str]:
        reached.append(len(store.read_jsonl(store.cycles_path)))
        raise RuntimeError("venue went away between the fills and the quarantine sweep")

    engine._quarantine = boom  # type: ignore[method-assign]
    await engine.guarded_cycle(market.bar_open_ms(399))
    del engine._quarantine
    for _ in range(2):
        market.cursor += 1
        await engine.guarded_cycle(market.bar_open_ms(market.cursor - 1))

    rows = store.read_jsonl(store.cycles_path)
    assert reached == [0], "precondition: the first cycle reached `_quarantine` before any row was written"
    assert rows[0]["phase"] == "ERROR" and len(rows) == 3
    carrying = [row for row in rows if "construction_full" in row]
    assert len(carrying) == 1, f"{len(carrying)} rows carry the payload; once per process is one row"
    assert carrying[0] in _cycles(store), "on a row the report reads, not on the ERROR row it filters out"
    assert carrying[0]["construction_full"]["digest"] == carrying[0]["construction"]


@pytest.mark.asyncio
async def test_a_payload_already_on_disk_is_not_written_again(august_panel: Panel, tmp_path: Path) -> None:
    """The other direction: the flag goes up when the row is on disk, not when the cycle returns.

    `_finish_cycle` writes the heartbeat after the row.  A flag raised only once the cycle came back
    would miss a heartbeat that raised after the append, and the next cycle would write the payload a
    second time.
    """
    engine, market, store = _engine(august_panel, tmp_path)
    await engine.startup()
    heartbeat = store.heartbeat
    failures = ["No space left on device"]

    def flaky(payload: dict[str, object]) -> None:
        if payload.get("phase") == "OK" and failures:
            raise OSError(failures.pop())
        heartbeat(payload)

    store.heartbeat = flaky  # type: ignore[method-assign]
    await engine.guarded_cycle(market.bar_open_ms(399))
    market.cursor += 1
    await engine.guarded_cycle(market.bar_open_ms(400))

    rows = store.read_jsonl(store.cycles_path)
    assert not failures and [row.get("phase") for row in rows] == [None, "ERROR", None], (
        "precondition: the first cycle's row was written, then its heartbeat failed"
    )
    assert [("construction_full" in row) for row in rows] == [True, False, False]
