"""Probe books (D-019): a sleeve the operator runs as a bounded demo experiment with an automatic stop rule.

A probe book is enabled on a *book-level* ACCEPT (``beidou research book``) rather than a
signal-level PASS, so it exists to earn out-of-sample evidence, not because the evidence is in.
Two things bound it: ``stop`` closes the book automatically when its trailing mark-to-market P&L
falls below ``-max_loss`` of equity (realised attributed income until the 2026-10-03 batch, see
`probe_status`), and ``review_after_days`` flags the review date in the daily report.  Pure
functions over the record's rows; the engine does the I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from beidou_alpha.registry import MAIN_BOOK, Registry
from beidou_live.attribution import ATTRIBUTION_BASIS

DAY_MS = 86_400_000


@dataclass(frozen=True)
class ProbeParams:
    book: str
    strategy: str
    window_days: int = 30
    max_loss: float = 0.01  # trailing mark-to-market P&L <= -max_loss x equity stops the book (0 = any loss)
    review_after_days: int = 90
    accepted_on: str = ""
    halts: bool = True
    """Whether firing removes the book from the model, or only reports.

    §3 gives the two transitions different channels on purpose: `probe -> retired` is "快：
    `stopped_books`" and `main -> probe` is not.  A probe is an unproven sleeve and halting it is the
    control; the main book carries `fraction` 1.0, so halting it is switching the strategy off, and
    §3 says a stopped main is DEMOTED and recounts - it never says it stops trading.

    So the main book's stop is computed, recorded and alerted every cycle, and moves the lifecycle;
    it does not empty the book.  Turning that into a halt is one field, taken through a governance
    transaction by a person, which is the right shape for a decision of that size.
    """

    def __post_init__(self) -> None:
        if self.window_days <= 0 or self.review_after_days <= 0:
            raise ValueError("probe windows must be positive")
        if not 0 <= self.max_loss <= 1:
            raise ValueError("probe max_loss must be in [0, 1]")

    @classmethod
    def from_entry(cls, book: str, strategy: str, probe: Mapping[str, Any], *, halts: bool = True) -> ProbeParams:
        raw_stop = probe.get("stop")
        stop: Mapping[str, Any] = raw_stop if isinstance(raw_stop, Mapping) else {}
        return cls(
            book=book,
            strategy=strategy,
            window_days=int(stop.get("window_days", 30)),
            max_loss=float(stop.get("max_loss", 0.01)),
            review_after_days=int(probe.get("review_after_days", 90)),
            accepted_on=str(probe.get("accepted_on", "") or ""),
            # The registry may ask for a halt explicitly; otherwise the book decides (see `halts`).
            halts=bool(stop.get("halts", halts)),
        )


def probes_from_registry(registry: Registry) -> tuple[ProbeParams, ...]:
    """Every enabled sleeve that carries a stop block - the main book included, since 2026-09-09.

    It used to exclude `MAIN_BOOK` unconditionally, with no comment and no test.  §3 says main KEEPS
    its P&L stop, so the exclusion made `main -> probe` unreachable from the record: no cycle ever
    reported a stop for a main sleeve, and the trailing attributed P&L of the book that carries the
    whole `fraction` was not merely un-acted-on, it was never COMPUTED.  What a rule cannot see it
    cannot bound.

    A main sleeve without a stop block is still skipped: absent knowledge is not a threshold.
    """
    return tuple(
        ProbeParams.from_entry(entry.book, entry.id, entry.probe, halts=entry.book != MAIN_BOOK)
        for entry in registry.enabled
        if entry.probe and (entry.probe.get("stop") or entry.book != MAIN_BOOK)
    )


def _row_time_ms(row: Mapping[str, Any]) -> int | None:
    for key in ("until_ms", "bar_open_ms"):
        value = row.get(key)
        if isinstance(value, int | float):
            return int(value)
    stamp = row.get("at")
    if stamp:
        try:
            return int(datetime.fromisoformat(str(stamp)).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _accepted_ms(accepted_on: str) -> int | None:
    if not accepted_on:
        return None
    try:
        parsed = datetime.fromisoformat(accepted_on)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def marked_pnl(
    cycles: Sequence[Mapping[str, Any]],
    book: str,
    *,
    window_days: int,
    now_ms: int,
) -> dict[str, Any]:
    """The book's own MARK-TO-MARKET P&L over the trailing window, as a share of equity.

    Sum over bars of ``w_{t-1} . r_t`` where ``w`` is that book's own weights before `combine_books`
    sums them (`cycles[...]["book_weights"]`) and ``r`` is the bar return from the record's own
    closes.  This is the quantity the sleeve's evidence is in, and the one D-019's "-2% is about -2
    sigma" was talking about.

    Why it exists (2026-09-12, pre-registered at `b883d01e`).  The stop read a realised-income series
    instead, and the two are not the same quantity:

        realised attributed income   30-day sigma 0.137% of equity  ->  -2% sits at 14.6 sigma
        mark-to-market (this one)    30-day sigma 3.239%            ->  -2% sits at  0.62 sigma

    Realised income only moves when a position closes, and the no-trade band held 197 symbol-cycles on
    the day this was measured - a sleeve can bleed on the mark for a month while that series barely
    moves.  Neither number is the 2 sigma D-019 claimed, and they miss in opposite directions.

    A short window UNDER-states the loss, because it sums fewer bars.  For a stop that is the safe
    direction - it can only delay a stop, never cause one - so the reading is returned with its bar
    count rather than refused while it warms up.  `bars` is how a reader tells a quiet month from a
    window that has not filled.  Since the 2026-10-03 batch this is the reading the stop gates on.
    """
    usable = sorted(
        (row for row in cycles if row.get("book_weights") and row.get("closes")),
        key=lambda row: int(row.get("bar_open_ms") or 0),
    )
    window_start = now_ms - window_days * DAY_MS
    total = 0.0
    bars = 0
    for previous, current in pairwise(usable):
        stamp = int(current.get("bar_open_ms") or 0)
        if stamp < window_start or stamp > now_ms:
            continue
        weights = (previous.get("book_weights") or {}).get(book) or {}
        before, after = previous.get("closes") or {}, current.get("closes") or {}
        if not weights:
            continue
        for symbol, weight in weights.items():
            start, end = before.get(symbol), after.get(symbol)
            if not start or end is None:
                continue  # no price on one side is no claim about that symbol's return
            total += float(weight) * (float(end) / float(start) - 1.0)
        bars += 1
    return {
        "value": None if bars == 0 else total,
        "bars": bars,
        "why": None if bars else "no cycle in the window carries both `book_weights` and `closes`",
    }


def probe_status(
    params: ProbeParams,
    attribution_rows: Sequence[Mapping[str, Any]],
    *,
    equity: float | None,
    now_ms: int,
    cycles: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Trailing-window P&L of the probe's book and whether the stop / review rules fire.

    Two calibers, and since the 2026-10-03 batch the gate reads the mark-to-market one (`marked_pnl`).
    `probe-stop-caliber` (`governance/window_changes.yaml`), applied with flow's retirement on the operator's
    ruling D1 of 2026-10-02; until then the gate read realised attributed income, for these reasons:

    * the sleeve's evidence is a mark-to-market P&L; the stop read realised attributed income.  On 2026-09-12
      flow's 30-day sigma was **3.239%** of equity on the first and **0.137%** on the second, so its `-2%`
      sat at **0.62 sigma** of what it was calibrated against and **14.6 sigma** of what it read - it could
      not fire, and neither was the "-2 sigma" D-019 claimed;
    * caliber and threshold have to move TOGETHER.  Moving the caliber alone would have fired at once:
      flow's marked 30-day reading was -3.197% against -2% (falsifier F3 of the 09-12 pre-registration);
    * `max_loss` sits in `registry_digest` (`stop_of`), so changing it resets M-Q08's window and, since #280,
      the evidence window; it waited for a restart that resets them anyway.

    The threshold now is the empirical 2.28% quantile of the book's own 30-day mark-to-market P&L on the pit
    panel - main 5.1% at k 0.175, re-measured on the post-retirement registry - and what it was measured on
    sits beside it in the registry (`stop.measured_at`).  The realised reading stays in the row as
    `realised_would_stop`: it gates nothing, and the day the two disagree is the day somebody needs both.

    A record without marked inputs (`marked_pnl` returns None) does not fire.  That is the safe direction for
    a stop and the one `marked_pnl` already takes for a short window: no reading is no claim of a loss.
    """
    accepted_ms = _accepted_ms(params.accepted_on)
    # rows before the acceptance belong to whatever ran under this strategy id before the probe (not its record)
    window_start = max(now_ms - params.window_days * DAY_MS, accepted_ms or 0)
    pnl = 0.0
    rows_in_window = 0
    stale_basis = 0
    first_ms: int | None = None
    for row in attribution_rows:
        stamp = _row_time_ms(row)
        by_strategy = row.get("by_strategy") or {}
        if stamp is None or params.strategy not in by_strategy:
            continue
        if accepted_ms is not None and stamp < accepted_ms:
            continue
        first_ms = stamp if first_ms is None else min(first_ms, stamp)
        if stamp < window_start or stamp > now_ms:
            continue
        # D-044: rows written under the magnitude split are a different quantity and may not be added
        # to these.  Inside one symbol that rule gave every strategy the sign of the SYMBOL's P&L, so
        # for a short-only sleeve beside a long book the two series need not even agree on direction -
        # summing them produces a number that is neither.  Skipped and counted, never converted:
        # re-deriving an old row needs that cycle's contributions, and only the latest are kept.
        #
        # What the gap costs, said plainly because it is a real one: the window empties at the
        # changeover and refills over `window_days`.  It changes no behaviour today - this rule cannot
        # fire at all at its current threshold (30-day sigma 0.137% against a 2% `max_loss`, above) -
        # and reaching the 2026-10-03 re-derivation with a clean series is worth more than reaching it
        # with a mixed one, since that re-derivation reads this very quantity's own distribution.
        if row.get("basis") != ATTRIBUTION_BASIS:
            stale_basis += 1
            continue
        try:
            pnl += float(by_strategy[params.strategy])
        except (TypeError, ValueError):
            continue
        rows_in_window += 1
    pnl_pct = None if equity is None or equity <= 0 or rows_in_window == 0 else pnl / equity
    start_ms = accepted_ms or first_ms
    days_running = None if start_ms is None else max(0.0, (now_ms - start_ms) / DAY_MS)
    marked = marked_pnl(cycles, params.book, window_days=params.window_days, now_ms=now_ms)
    marked_value = marked["value"]
    stop = marked_value is not None and marked_value < 0 and marked_value <= -params.max_loss
    # What the realised caliber would say at the same threshold - reported, gating nothing since 2026-10-03.
    realised_would_stop = pnl_pct is not None and pnl_pct <= -params.max_loss and pnl < 0
    review_due = days_running is not None and days_running >= params.review_after_days
    return {
        "book": params.book,
        "strategy": params.strategy,
        "window_days": params.window_days,
        "rows": rows_in_window,
        # D-044: window rows dropped for carrying the retired magnitude basis.  Reported so a
        # thin window reads as "the caliber changed" rather than as "the sleeve stopped trading".
        "stale_basis_rows": stale_basis,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "max_loss": params.max_loss,
        "days_running": days_running,
        "review_after_days": params.review_after_days,
        "stop": stop,
        "marked_pnl_pct": marked_value,
        "marked_bars": marked["bars"],
        "marked_why": marked["why"],
        "gate": "marked",  # the caliber `stop` reads; the other one is `realised_would_stop`
        "realised_would_stop": realised_would_stop,
        "review_due": review_due,
        "status": "STOP" if stop else ("REVIEW_DUE" if review_due else "OK"),
    }


__all__ = ["DAY_MS", "ProbeParams", "marked_pnl", "probe_status", "probes_from_registry"]
