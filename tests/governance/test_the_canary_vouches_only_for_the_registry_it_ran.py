"""L4 vouches for the registry the soak ran, not for whichever file `apply` is handed.

Found 2026-09-27 (RESEARCH_LOG, the startup-gate section's first "顺带发现"): `admit` asked whether the
latest round was healthy and never whether it ran the proposal.  Every shadow row carries `registry`,
the digest the process held (`engine.registry_digest`), and nothing compared it with the bytes about to
be written.  So a round soaked on one candidate vouched for any other: edit the candidate after the
soak, or hand `apply` a different file, and L4 read the old soak's health as this proposal's.

`governance canary` has no counterpart and needs none: it scores a soak and holds no proposal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from beidou_alpha.registry import parse_registry
from beidou_cli import main
from beidou_governance.admission import admit, canary_health, window_index
from beidou_governance.canary import SOAK_CYCLES
from beidou_governance.lifecycle import Book, Candidate, State
from beidou_governance.policy import Policy
from beidou_live.composition import build_model
from beidou_live.engine import registry_digest
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "config" / "live.demo.yaml"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _hourly(hours: int, registry: str | Callable[[int], str] | None) -> list[dict[str, Any]]:
    """Cycles as the engine writes them, ending at NOW; ``registry`` is one digest, one per hour, or none."""
    rows = []
    for hour in range(hours):
        bar = NOW - timedelta(hours=hours - hour)
        row: dict[str, Any] = {
            "at": (bar + timedelta(hours=1, seconds=25)).isoformat(),
            "bar_open_ms": int(bar.timestamp() * 1000),
            "construction": "aaaa",
            "guard_reasons": [],
            "universe": ["BTCUSDT"],
            "targets": {"BTCUSDT": 0.1},
            "skipped": [],
        }
        digest = registry(hour) if callable(registry) else registry
        if digest is not None:
            row["registry"] = digest
        rows.append(row)
    return rows


def _registry(*, sleeve: bool) -> dict[str, Any]:
    """The main book alone, or with `flow` asking for a third of it - the promotion under test."""
    payload: dict[str, Any] = {
        "version": 1,
        "ensemble": {"method": "mean", "turnover_penalty": 0.0},
        "books": {},
        "strategies": [{"id": "tsmom", "enabled": True, "weight": 1.0}],
    }
    if sleeve:
        payload["books"] = {"flow_short": {"fraction": 1 / 3}}
        payload["strategies"].append({"id": "flow", "enabled": True, "weight": 1.0, "book": "flow_short"})
    return payload


# --- the reader: one round, one registry, and it is the proposal --------------------------------


def test_a_healthy_soak_vouches_for_the_registry_it_ran() -> None:
    healthy, why = canary_health(
        _hourly(SOAK_CYCLES, "aaaa"), _hourly(SOAK_CYCLES, "aaaa"), aliases=None, registry="aaaa"
    )
    assert healthy, why
    assert "aaaa" in why


def test_a_healthy_soak_does_not_vouch_for_another_registry() -> None:
    healthy, why = canary_health(
        _hourly(SOAK_CYCLES, "aaaa"), _hourly(SOAK_CYCLES, "aaaa"), aliases=None, registry="bbbb"
    )
    assert not healthy
    assert why.startswith("L4: registry_soaked (") and "aaaa" in why and "bbbb" in why, why


def test_a_round_that_ran_two_registries_vouches_for_neither() -> None:
    """A relaunch finishes the open round (`canary.rounds`), so an edited candidate can land mid-round."""
    shadow = _hourly(SOAK_CYCLES, lambda hour: "aaaa" if hour < 100 else "bbbb")
    for proposal in ("aaaa", "bbbb"):
        healthy, why = canary_health(shadow, _hourly(SOAK_CYCLES, "aaaa"), aliases=None, registry=proposal)
        assert not healthy and "aaaa" in why and "bbbb" in why, why


def test_neither_a_round_without_digests_nor_a_proposal_without_one_is_a_match() -> None:
    """Could not be compared is not a match, and the reason says which side had nothing to compare."""
    unstamped, armed = _hourly(SOAK_CYCLES, None), _hourly(SOAK_CYCLES, "aaaa")
    healthy, why = canary_health(unstamped, armed, aliases=None, registry="aaaa")
    assert not healthy and "no decided cycle" in why, why
    healthy, why = canary_health(_hourly(SOAK_CYCLES, "aaaa"), armed, aliases=None, registry=None)
    assert not healthy and "no model" in why, why


# --- the rule that reads it --------------------------------------------------------------------


def test_admit_refuses_a_promotion_the_soak_did_not_run() -> None:
    """Otherwise clean - queued head, a third of the book, 45 days on one construction - so L4 alone refuses."""
    before, after = parse_registry(_registry(sleeve=False)), parse_registry(_registry(sleeve=True))
    book = Book(
        window=window_index(Policy(), now=NOW), candidates={"flow": Candidate("flow", State.QUEUED, fraction=1 / 3)}
    )
    shadow, armed = _hourly(SOAK_CYCLES, "aaaa"), _hourly(45 * 24, "aaaa")

    soaked = admit(before, after, book=book, cycles=armed, shadow=shadow, registry="aaaa", now=NOW)
    other = admit(before, after, book=book, cycles=armed, shadow=shadow, registry="bbbb", now=NOW)

    assert soaked.allowed, (soaked.reasons, soaked.measured["canary"])
    assert other.reasons == ("flow: L4: the canary soak did not pass",), other.reasons
    assert "registry_soaked" in str(other.measured["canary"])


# --- the command that hands it the proposal ------------------------------------------------------


def _plan(tmp_path: Path, proposed: dict[str, Any], shadow: list[dict[str, Any]]) -> Any:
    current, candidate = tmp_path / "alpha_registry.yaml", tmp_path / "proposed.yaml"
    current.write_text(yaml.safe_dump(_registry(sleeve=False)), encoding="utf-8")
    candidate.write_text(yaml.safe_dump(proposed), encoding="utf-8")
    record = tmp_path / "live-shadow-dry-run"
    record.mkdir(exist_ok=True)
    (record / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in shadow), encoding="utf-8")
    return CliRunner().invoke(
        main,
        [
            *("governance", "plan", "--proposed", str(candidate), "--registry", str(current)),
            *("--profile", str(PROFILE), "--data-root", str(tmp_path / "data"), "--root", str(tmp_path)),
            *("--state-dir", str(tmp_path / "live"), "--shadow-dir", str(tmp_path / "live-shadow")),
        ],
    )


def test_plan_matches_the_soak_against_the_proposal_it_was_handed(tmp_path: Path) -> None:
    """`_admission` computes the digest the way the engine stamps it: `registry_digest(build_model(...))`.

    An unfinished soak, so `soak` fails either way and `registry_soaked` is the only thing that moves.
    """
    proposed = _registry(sleeve=True)
    stamped = registry_digest(build_model(parse_registry(proposed), load_yaml(PROFILE)))

    ran_it = _plan(tmp_path, proposed, _hourly(24, stamped))
    ran_another = _plan(tmp_path, proposed, _hourly(24, "0" * 12))

    canary = [line for line in ran_it.stderr.splitlines() if line.strip().startswith("canary:")]
    assert canary and "soak (24/168" in canary[0] and "registry_soaked" not in canary[0], ran_it.output
    assert "registry_soaked" in ran_another.stderr and stamped in ran_another.stderr, ran_another.output


def test_a_stop_that_disables_every_strategy_is_not_held_up_by_it(tmp_path: Path) -> None:
    """`build_model` refuses a registry with no enabled strategy.  A stop must still reach the gate.

    §3's fast paths go the other way from a promotion and must not wait on anything a promotion needs;
    a digest that raised here would turn the one-line emergency stop into a traceback.
    """
    stop = _registry(sleeve=False)
    stop["strategies"] = [dict(entry, enabled=False) for entry in stop["strategies"]]

    result = _plan(tmp_path, stop, _hourly(24, "aaaa"))

    assert result.exit_code == 0, result.output
    assert "admission: ALLOWED (promoting: nothing)" in result.stderr
