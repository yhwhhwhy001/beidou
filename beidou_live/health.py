"""M-001: how much of the loop's own job it actually did, from the append-only cycle log.

The plan promised two numbers and neither existed: a cycle success rate with a 95%
floor, and a run of days without intervention.  Both are computed here as pure
functions over ``cycles.jsonl`` rows so the threshold can be enforced wherever the
operator looks, and so the definitions are testable rather than implied.

"Unattended" is not directly observable - nobody records a human decision - so what
is measured is its closest honest proxy: a day is *clean* when every cycle that ran
that day completed and no restart happened.  A restart is either a crash or an
operator acting; either way the run of untouched days ends, which is exactly what the
metric is for.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from beidou_shared.types import Position


@dataclass(frozen=True)
class CycleHealth:
    attempts: int
    failures: int
    window_hours: float
    success_rate: float | None  # None when nothing ran in the window
    clean_days: int
    last_failure: str | None

    def restarts_note(self) -> str:
        return "无失败记录" if self.last_failure is None else f"最近一次失败在 {self.last_failure}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "failures": self.failures,
            "window_hours": self.window_hours,
            "success_rate": self.success_rate,
            "clean_days": self.clean_days,
            "last_failure": self.last_failure,
        }


def _stamp(row: Mapping[str, Any]) -> datetime | None:
    bar = row.get("bar_open_ms")
    if isinstance(bar, int | float):
        return datetime.fromtimestamp(float(bar) / 1000, tz=UTC)
    at = row.get("at")
    if at:
        try:
            return datetime.fromisoformat(str(at))
        except ValueError:
            return None
    return None


def cycle_health(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    window_hours: float = 24.0,
    restarted_at: str | None = None,
) -> CycleHealth:
    """Success rate over the trailing window, and the run of days with no failure and no restart.

    A failed cycle is a row with ``phase == "ERROR"`` (written since 2026-09-04); a
    guard-skipped cycle counts as a success, because the loop did the right thing.
    Dry-run rows are excluded: they are rehearsals, not the loop doing its job.
    """
    cutoff = now - timedelta(hours=window_hours)
    attempts = failures = 0
    failure_days: set[str] = set()
    last_failure: str | None = None
    for row in rows:
        if row.get("dry_run"):
            continue
        stamp = _stamp(row)
        if stamp is None:
            continue
        failed = str(row.get("phase", "")) == "ERROR"
        if failed:
            failure_days.add(stamp.strftime("%Y-%m-%d"))
            last_failure = stamp.isoformat()
        if stamp >= cutoff:
            attempts += 1
            failures += int(failed)
    rate = None if attempts == 0 else 1.0 - failures / attempts
    return CycleHealth(
        attempts=attempts,
        failures=failures,
        window_hours=window_hours,
        success_rate=rate,
        clean_days=_clean_days(now, failure_days, restarted_at),
        last_failure=last_failure,
    )


def _clean_days(now: datetime, failure_days: set[str], restarted_at: str | None) -> int:
    """Whole UTC days back from today with no failed cycle and no restart."""
    restart_day: str | None = None
    if restarted_at:
        try:
            restart_day = datetime.fromisoformat(restarted_at).astimezone(UTC).strftime("%Y-%m-%d")
        except ValueError:
            restart_day = None
    days = 0
    while True:
        day = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if day in failure_days or day == restart_day:
            return days
        days += 1
        if days > 3650:  # a decade of clean days means the inputs are wrong, not that the loop is perfect
            return days


__all__ = ["CycleHealth", "cycle_health"]


# --- DL-X1: liquidation distance and the margin-mode assertion (L1-05 / KILL-R19) ---------------


def liquidation_distance(position: Position, *, daily_vol: float) -> float | None:
    """How far the mark is from the liquidation price, in daily volatility units.

    ``None`` whenever the question does not apply: a flat symbol, no volatility estimate, or - the
    common case - a position the venue reports no reachable liquidation price for.  ``None`` is not
    a small number, and the caller must not be able to treat it as one.
    """
    if position.qty == 0.0 or daily_vol <= 0.0:
        return None
    liquidation = position.liquidation_price
    if liquidation is None or liquidation <= 0.0 or position.mark_price <= 0.0:
        return None
    move = abs(position.mark_price - liquidation) / position.mark_price
    return move / daily_vol


def min_liquidation_distance(positions: Sequence[Position], daily_vol: Mapping[str, float]) -> dict[str, Any]:
    """M-Q06: the closest position that *has* a liquidation price, and how many do not.

    Both halves are reported.  "Every position is out of reach" is a fact about the book; a missing
    number would be a gap in the instrument, and the two must never look the same in the log.
    """
    measured: list[tuple[float, str]] = []
    unreachable = 0
    unmeasurable = 0
    for position in positions:
        if position.qty == 0.0:
            continue
        vol = float(daily_vol.get(position.symbol, 0.0))
        # Three outcomes, not two.  `unmeasurable` is the caller having no volatility estimate for
        # this symbol - the live case is a foreign position, which `asset_vol` never covers because
        # it only sizes the universe.  Folding it into `unreachable` would say "the venue reports no
        # liquidation price" about a symbol the venue was never asked, which is this function's own
        # mistake made one level up.
        if not math.isfinite(vol) or vol <= 0.0:
            unmeasurable += 1
            continue
        distance = liquidation_distance(position, daily_vol=vol)
        if distance is None:
            unreachable += 1
        else:
            measured.append((distance, position.symbol))
    closest = min(measured) if measured else None
    return {
        "min_distance": None if closest is None else closest[0],
        "symbol": None if closest is None else closest[1],
        "measured": len(measured),
        "unreachable": unreachable,
        "unmeasurable": unmeasurable,
    }


def margin_mode_problems(
    *, multi_assets: bool, isolated_symbols: Sequence[str], expect_multi_assets: bool
) -> list[str]:
    """KILL-R19: refuse to trade an account whose risk model is not the validated one.

    Asserts only.  Changing an account's margin mode under an open book is an operator action with
    consequences the loop cannot evaluate, so this reports and stops; it never sets.
    """
    problems: list[str] = []
    for symbol in isolated_symbols:
        problems.append(
            f"{symbol} is on ISOLATED margin; the book is sized and validated for CROSSED, and isolated "
            "margin manufactures liquidations the strategy never asked for (report 7.3(a))"
        )
    if bool(multi_assets) != bool(expect_multi_assets):
        problems.append(
            f"multiAssetsMargin is {multi_assets} but the profile expects {expect_multi_assets}; equity that "
            "floats with collateral prices is a different book from the one the evidence describes"
        )
    return problems


# --- construction identity: when the fingerprint's DEFINITION changes (2026-09-07) ------------------

# The field set `construction_fingerprint` hashes.  Bumped whenever a key is added or removed, and
# reported OUTSIDE the hash so that bumping it does not itself move the digest - inside, the version
# would change the very number it exists to explain.
#   1: the original 22 fields
#   2: + `exits.unit_mode` (P22).  The field is right - it decides what the exit overlay does, and
#      KILL-R14's 7-of-18 unplaceable stops are that setting - but adding it moved the digest while the
#      book was byte-identical, and `evidence_window` read that as a construction change.
#   3: + the four `exits.regime_*` (P23), the same day and the same way.  Inert at the shipped config:
#      `regime_window` defaults to 0 and `regime_tp_scale()` returns None at <= 0, and the profile sets
#      none of the four - so again the values are unchanged and only the shape moved.  Twice in one day
#      is why `test_construction_identity` now pins the field set: the next one fails a test instead.
CONSTRUCTION_PAYLOAD_VERSION = 3

# Digests the operator has declared to be the SAME BOOK as an earlier one.  In code rather than config
# because the declaration is a claim about evidence: it takes a commit, and the commit carries the proof.
#
# 2026-09-07, restart #5.  Recomputing both field sets against the one live config reproduced both
# digests exactly - 22 fields -> 0dcd044d..., 23 fields -> b441ea62... - so no construction VALUE
# changed, only the shape of what was hashed.  Operator ruled the pre-15:00Z rows are the same
# construction, so M-010's window continues across the boundary instead of restarting.
#
# History is not rewritten: the old rows keep the digest they were written with.  The equivalence lives
# here, in the reader, where it can be read and argued with.
CONSTRUCTION_ALIASES: dict[str, str] = {
    # v2 (+ unit_mode), recorded by the running loop from restart #5.
    "b441ea62d02184bfb6292ec1033f20482bc9c7c0a2c6ebfcc14623241b967417": (
        "0dcd044d0158c6aec263429eab9cdba9449dba0b55b07807dfd0e3d3a3a9b6e0"
    ),
    # v3 (+ the four regime_*), what the next restart will record.  Declared before it is ever written,
    # which is the right order: the claim is about values that are already known to be unchanged.
    "c0e5c49c5a4acb1d0e5bb709873ce38b0674c10a027405d5269dbb724d734d1c": (
        "0dcd044d0158c6aec263429eab9cdba9449dba0b55b07807dfd0e3d3a3a9b6e0"
    ),
}


def canonical_construction(digest: str | None) -> str | None:
    """The digest a row's construction should be COMPARED as.

    One hop only, never a chain: a chain would make the answer depend on resolution order, and a test
    holds every alias target out of the table's own keys so the single hop is always enough.
    """
    if digest is None:
        return None
    return CONSTRUCTION_ALIASES.get(str(digest), str(digest))
