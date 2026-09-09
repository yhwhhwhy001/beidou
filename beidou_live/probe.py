"""Probe books (D-019): a sleeve the operator runs as a bounded demo experiment with an automatic stop rule.

A probe book is enabled on a *book-level* ACCEPT (``beidou research book``) rather than a
signal-level PASS, so it exists to earn out-of-sample evidence, not because the evidence is in.
Two things bound it: ``stop`` closes the book automatically when its trailing attributed P&L
falls below ``-max_loss`` of equity, and ``review_after_days`` flags the review date in the
daily report.  Pure functions over attribution rows; the engine does the I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from beidou_alpha.registry import MAIN_BOOK, Registry

DAY_MS = 86_400_000


@dataclass(frozen=True)
class ProbeParams:
    book: str
    strategy: str
    window_days: int = 30
    max_loss: float = 0.01  # trailing attributed net P&L <= -max_loss x equity stops the book (0 = any loss)
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


def probe_status(
    params: ProbeParams,
    attribution_rows: Sequence[Mapping[str, Any]],
    *,
    equity: float | None,
    now_ms: int,
) -> dict[str, Any]:
    """Trailing-window attributed P&L of the probe's strategy and whether the stop / review rules fire."""
    accepted_ms = _accepted_ms(params.accepted_on)
    # rows before the acceptance belong to whatever ran under this strategy id before the probe (not its record)
    window_start = max(now_ms - params.window_days * DAY_MS, accepted_ms or 0)
    pnl = 0.0
    rows_in_window = 0
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
        try:
            pnl += float(by_strategy[params.strategy])
        except (TypeError, ValueError):
            continue
        rows_in_window += 1
    pnl_pct = None if equity is None or equity <= 0 or rows_in_window == 0 else pnl / equity
    start_ms = accepted_ms or first_ms
    days_running = None if start_ms is None else max(0.0, (now_ms - start_ms) / DAY_MS)
    stop = pnl_pct is not None and pnl_pct <= -params.max_loss and pnl < 0
    review_due = days_running is not None and days_running >= params.review_after_days
    return {
        "book": params.book,
        "strategy": params.strategy,
        "window_days": params.window_days,
        "rows": rows_in_window,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "max_loss": params.max_loss,
        "days_running": days_running,
        "review_after_days": params.review_after_days,
        "stop": stop,
        "review_due": review_due,
        "status": "STOP" if stop else ("REVIEW_DUE" if review_due else "OK"),
    }


__all__ = ["DAY_MS", "ProbeParams", "probe_status", "probes_from_registry"]
