"""L4 / DL-G5: six deployment-health checks over a shadow run, and the one thing they do not do.

**A canary does not filter alpha** (KILL-AR-04).  The Phase 7 review's sharpest correction was that
calling this a filter would let a bad candidate through on a technicality and, worse, let a good one
be blocked by an unrelated venue hiccup - and then be recorded as evidence against the candidate.  It
answers one question: *would this registry, deployed, behave like a working deployment?*  Whether the
sleeve has any edge is R3's 1/3 budget and the P&L stop's problem, not this module's.

The shadow is `live run --dry-run --state-dir .beidou/live-shadow` against the candidate registry.  The
one check with a baseline, `guard_rate`, compares it with the ARMED loop's own cycles over the bars the
scored round decided, rather than with a constant.  A constant would fail the whole canary on a day the
venue was slow, which is the false negative that teaches an operator to ignore it.  So would the armed
loop's whole history, which is what both callers passed until 2026-09-26 (`_same_bars`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SOAK_CYCLES = 168


def attempted(row: Mapping[str, Any]) -> bool:
    """A cycle `engine.run` counts against `--cycles`: OK or ERROR.  SKIPPED rows are bars it slept through."""
    return row.get("phase") != "SKIPPED"


def rounds(rows: Sequence[Mapping[str, Any]], soak_cycles: int = SOAK_CYCLES) -> list[list[Mapping[str, Any]]]:
    """The record cut into soaks of ``soak_cycles`` attempted cycles, oldest first; the last may be unfinished.

    Cut by position because a row names no process and no soak.  Until 2026-09-26 the canary read the
    whole record as one soak while it held two: `live run` exits 1 when any of its 168 cycles failed,
    launchd relaunches a non-zero exit, and at 2026-09-23T08:00Z a second 168 began in the same file
    under a new construction.  `construction_stable` then failed on two digests, each of them stable
    inside its own soak.  `deploy/run_shadow.sh` asks `remaining` before it starts and a relaunch
    finishes the open round instead of opening another, so these cuts are the launcher's own.

    A SKIPPED row stays in the round it was written in: a backoff after a round's last cycle must not
    open the next one, or the launcher would read a finished soak as a new one with 168 to go.
    """
    cut: list[list[Mapping[str, Any]]] = []
    count = 0
    for row in rows:
        if not cut or (attempted(row) and count >= soak_cycles):
            cut.append([])
            count = 0
        cut[-1].append(row)
        count += attempted(row)
    return cut


def remaining(rows: Sequence[Mapping[str, Any]], soak_cycles: int = SOAK_CYCLES) -> int:
    """Cycles the latest round still needs: all of them on an empty record, 0 once it is finished."""
    latest = rounds(rows, soak_cycles)[-1] if rows else []
    return max(0, soak_cycles - sum(map(attempted, latest)))


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CanaryResult:
    checks: tuple[Check, ...] = field(default_factory=tuple)
    soaked: int = 0
    #: The `registry` digests the scored round's decided cycles carry: what the soak ran, which is a
    #: reading and not a check - only a caller holding a proposal can judge it (`canary_health`).
    registries: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)


def _rate(rows: Sequence[Mapping[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row.get(key)) / len(rows)


def _longest_error_streak(rows: Sequence[Mapping[str, Any]]) -> int:
    longest = streak = 0
    for row in rows:
        streak = streak + 1 if row.get("phase") == "ERROR" else 0
        longest = max(longest, streak)
    return longest


def _same_bars(
    baseline: Sequence[Mapping[str, Any]], scored: Sequence[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], str]:
    """The armed loop's decided cycles over the bars the scored round decided, and those bars as text.

    Not the whole armed record.  Spread over it, an outage both loops sat through is diluted until the
    candidate looks guard-prone, and a storm weeks earlier raises the bar the candidate is held to.
    By `bar_open_ms`: the two loops close one bar seconds apart, and the bar is what they share.  A
    round with no stamped cycle has no window; it reads "?", as the round listing does.
    """
    stamps = [int(row["bar_open_ms"]) for row in scored if row.get("bar_open_ms") is not None]
    if not stamps:
        return [], "?"
    first, last = min(stamps), max(stamps)
    same = [
        row
        for row in baseline
        if row.get("phase") not in ("ERROR", "SKIPPED")
        and row.get("bar_open_ms") is not None
        and first <= int(row["bar_open_ms"]) <= last
    ]
    text = [datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()[:16] for ms in (first, last)]
    return same, "..".join(text)


def evaluate(
    shadow: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
    *,
    soak_cycles: int = SOAK_CYCLES,
    max_error_streak: int = 2,
    aliases: Mapping[str, str] | None = None,
) -> CanaryResult:
    """§5's L4 row, in the order a deployment fails them: `soak`, then five of its six checks.

    The sixth, zero startup-gate refusals, was here as `startup_gate` until 2026-09-27 and nothing ever
    supplied it.  The shadow is a dry run and a dry run refuses nothing; `plan`/`apply` passed no count,
    so it read `0 refusals` whatever the soak saw.  Measured that day: the process writing the scored
    round had printed `evidence: tsmom: evidence verdict FAIL does not allow live use` as it started.
    The question belongs to the write, where `promote` asks it of the bytes about to be written, with
    both halves `live run` refuses on (`governance_cmd._gate`).  Operator ruling the same day.

    ``aliases`` is `CONSTRUCTION_ALIASES`, and `construction_stable` is wrong without it: the raw
    digest has moved six times on the armed record since 2026-09-04 while the CANONICAL construction
    moved once, because a renamed field changes the hash and nothing else.  Measured 2026-09-09 by
    pointing this function at the armed loop's own cycles: 6 distinct digests, and a candidate would
    have been called unhealthy for a deployment that never changed.  Passed in rather than imported
    so the package stays free of `beidou_live`.

    ``shadow`` is the whole record and only its latest round is scored (`rounds`): `plan` and `apply`
    reach this through `canary_health`, so cutting here rather than in one caller covers both.
    """
    cut = rounds(shadow, soak_cycles)
    shadow = cut[-1] if cut else []
    soaked = sum(map(attempted, shadow))
    resolve = dict(aliases or {})
    decided = [row for row in shadow if row.get("phase") not in ("ERROR", "SKIPPED")]
    digests = {
        resolve.get(str(row.get("construction")), str(row.get("construction")))
        for row in decided
        if row.get("construction")
    }
    armed, bars = _same_bars(baseline, decided)
    base_guard_rate = _rate(armed, "guard_reasons")
    guard_rate = _rate(decided, "guard_reasons")
    streak = _longest_error_streak(shadow)

    over_cap = [
        f"{row.get('at')}: {order.get('symbol')}"
        for row in decided
        for order in (row.get("skipped") or [])
        if str(order.get("reason")) == "PARTICIPATION_CAP"
    ]
    outside = [
        f"{row.get('at')}: {symbol}"
        for row in decided
        for symbol in (row.get("targets") or {})
        if symbol not in set(row.get("universe") or [])
    ]

    checks = (
        Check("soak", soaked >= soak_cycles, f"{soaked}/{soak_cycles} cycles in round {len(cut)} of {len(cut)}"),
        Check("construction_stable", len(digests) <= 1, f"{len(digests)} distinct construction digests"),
        # `<=` with a tolerance rather than `<`: guards firing at the same rate as the armed book is
        # the expected outcome, and a canary that demands an improvement is asking the wrong question.
        # No armed cycle over the round's bars is a comparison that could not be made, never a pass.
        Check(
            "guard_rate",
            bool(armed) and guard_rate <= base_guard_rate + 0.05,
            f"shadow {guard_rate:.3f} over {len(decided)} vs armed {base_guard_rate:.3f} over {len(armed)}, bars {bars}"
            if armed
            else f"no decided armed cycle over bars {bars}; nothing to compare the shadow against",
        ),
        Check("no_error_streak", streak <= max_error_streak, f"longest ERROR streak {streak}"),
        # Planned orders inside the participation cap, and targets inside the universe.  Both are
        # deployment facts: an order the rebalancer would truncate and a target for a symbol the loop
        # does not manage are wiring mistakes, and neither says anything about the candidate's edge.
        Check("participation", not over_cap, f"{len(over_cap)} planned orders above the cap"),
        Check("targets_in_universe", not outside, f"{len(outside)} targets outside the managed universe"),
    )
    registries = tuple(sorted({str(row["registry"]) for row in decided if row.get("registry")}))
    return CanaryResult(checks, soaked=soaked, registries=registries)
