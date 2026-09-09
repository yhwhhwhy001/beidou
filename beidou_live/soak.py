"""L3's criterion, computed - and computed twice, because §5 states it two ways that disagree.

Nothing read `.beidou/paper-l3` until 2026-09-09.  The soak ran, launchd kept it alive, and the
criterion it was running against lived only in prose.

**The two readings.**  §5's L3 row says "7 天无 ERROR 相".  The Testnet paragraph of the same section
says "到币安的路径经系统代理 1082，间歇 503——ERROR 相**不产生任何治理决定**", and KILL-AR-20 turned
that into `Policy.no_decision_phases`.  Those are not two wordings of one rule.  The first counts
ERROR cycles; the second says an ERROR cycle is a cycle that decided nothing, which is a statement
about consequences.  Measured on the armed record 2026-09-09: 2 ERROR cycles in 6.29 days, rate
0.318/day, longest clean run 4.92 days, and **both ERROR cycles produced zero governance side effect**
- no orders, nothing leaving, nothing quarantined, the ladder untouched, no universe update.

Under a Poisson fit that rate gives a 10.8% chance of seven consecutive clean days, 1.2% of fourteen,
0.03% of thirty.  So the literal reading is not a demanding criterion, it is a mostly unreachable one,
and it is unreachable for a reason §5 itself names two paragraphs later.  This module reports both and
rules on neither: `literal_pass` and `no_decision_pass`, side by side, with the counts under each.

A third number is reported because neither reading covers it: the longest ERROR STREAK.  One 503 is
the proxy blinking; six hours of them is the venue being unreachable, and only the second is a fact
about whether this deployment works.  It is reported, not gated, until somebody rules on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: What a cycle must not have done to count as having decided nothing.  Each is a governance action
#: the loop can take on its own; an ERROR cycle that took one would be KILL-AR-20 failing in practice.
DECISION_KEYS = ("orders", "leaving", "quarantined", "universe_update", "risk_ladder")


@dataclass(frozen=True)
class SoakReading:
    cycles: int = 0
    days: float = 0.0
    required_days: float = 7.0
    error_cycles: int = 0
    skipped_cycles: int = 0
    longest_error_streak: int = 0
    longest_clean_days: float = 0.0
    deciding_errors: tuple[str, ...] = ()
    transactions_closed: bool | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def long_enough(self) -> bool:
        return self.days >= self.required_days

    @property
    def literal_pass(self) -> bool:
        """§5 L3 as written: seven days and not one ERROR phase."""
        return self.long_enough and self.error_cycles == 0 and self.transactions_closed is not False

    @property
    def no_decision_pass(self) -> bool:
        """§5's Testnet clause: seven days in which no ERROR cycle decided anything."""
        return self.long_enough and not self.deciding_errors and self.transactions_closed is not False


def _decided(row: Mapping[str, Any]) -> tuple[str, ...]:
    """What a cycle did that a no-decision cycle must not have done.

    Emptiness is checked rather than presence: the loop writes `orders: []` and `risk_ladder: {}` on a
    quiet cycle, so `key in row` would call every cycle a decision.
    """
    out = []
    for key in DECISION_KEYS:
        value = row.get(key)
        if isinstance(value, list | dict | tuple):
            if value:
                out.append(key)
        elif value:
            out.append(key)
    return tuple(out)


def score(
    cycles: Sequence[Mapping[str, Any]],
    *,
    required_days: float = 7.0,
    transactions_closed: bool | None = None,
    no_decision_phases: Sequence[str] = ("ERROR",),
) -> SoakReading:
    """Both readings of L3 over one soak's record."""
    if not cycles:
        return SoakReading(required_days=required_days, notes=("the record is empty",))

    stamps: list[datetime] = []
    for row in cycles:
        try:
            stamps.append(datetime.fromisoformat(str(row.get("at"))))
        except ValueError:
            # Carry the previous stamp forward: one unreadable row inside a soak is a torn write, not a
            # reason to refuse the whole reading.  An unreadable FIRST row is different - there is no
            # previous stamp, and inventing an epoch would silently make `days` enormous and pass a
            # criterion about seven days on a record that cannot say when it started.
            if not stamps:
                return SoakReading(
                    cycles=len(cycles),
                    required_days=required_days,
                    transactions_closed=transactions_closed,
                    notes=(
                        f"the first cycle's timestamp is unreadable ({row.get('at')!r}); no window can be measured",
                    ),
                )
            stamps.append(stamps[-1])
    days = (stamps[-1] - stamps[0]).total_seconds() / 86_400.0

    errors = [i for i, row in enumerate(cycles) if str(row.get("phase")) in set(no_decision_phases)]
    skipped = sum(1 for row in cycles if str(row.get("phase")) == "SKIPPED")

    streak = longest = 0
    for row in cycles:
        streak = streak + 1 if str(row.get("phase")) in set(no_decision_phases) else 0
        longest = max(longest, streak)

    marks = [stamps[0], *[stamps[i] for i in errors], stamps[-1]]
    clean = max((marks[i + 1] - marks[i]).total_seconds() / 86_400.0 for i in range(len(marks) - 1))

    deciding = tuple(f"{cycles[i].get('at')}: {', '.join(_decided(cycles[i]))}" for i in errors if _decided(cycles[i]))
    notes: list[str] = []
    if errors and not deciding:
        notes.append(f"{len(errors)} ERROR cycles, none of which decided anything (KILL-AR-20's shape)")
    if longest > 1:
        notes.append(f"longest ERROR streak {longest}: a run is the venue being unreachable, not a blink")
    return SoakReading(
        cycles=len(cycles),
        days=days,
        required_days=required_days,
        error_cycles=len(errors),
        skipped_cycles=skipped,
        longest_error_streak=longest,
        longest_clean_days=clean,
        deciding_errors=deciding,
        transactions_closed=transactions_closed,
        notes=tuple(notes),
    )


def render(reading: SoakReading) -> str:
    lines = [
        f"soak     {reading.cycles} cycles, {reading.days:.2f}/{reading.required_days:.0f} days",
        f"phases   ERROR {reading.error_cycles}, SKIPPED {reading.skipped_cycles}, "
        f"longest ERROR streak {reading.longest_error_streak}",
        f"clean    longest run without an ERROR: {reading.longest_clean_days:.2f} days",
        f"§5 L3 字面   {'PASS' if reading.literal_pass else 'FAIL'}  (7 天无 ERROR 相)",
        f"§5 no-decision {'PASS' if reading.no_decision_pass else 'FAIL'}  (7 天内没有 ERROR 周期做过决定)",
    ]
    if reading.transactions_closed is not None:
        lines.append(f"事务链     {'closed' if reading.transactions_closed else 'BROKEN'}")
    lines.extend(f"  - {note}" for note in reading.notes)
    lines.extend(f"  ! ERROR cycle decided something: {why}" for why in reading.deciding_errors)
    return "\n".join(lines)
