"""2026-09-09: the traded population is a decision, so it lives in the registry and moves by transaction.

Before this, `universe.json` was the decision and the loop rewrote it every day at about 01:00Z.  Every
cited report records the universe fingerprint it was produced under, so each daily re-rank made the
evidence stop describing the traded population and `registry_dataset_problems` refused the next armed
start.  Measured: restartability expired daily, and nothing anywhere said so until a canary triggered
it seven hours early on 2026-09-08.

So the re-rank keeps happening and keeps being recorded, and stops deciding - the same split the
governance plan applies to every other construction change, applied to the population.  What a batch
window acts on is the recorded proposal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from beidou_alpha.registry import parse_registry, registry_fingerprint
from beidou_live.config import resolve_universe

PINNED = {
    "version": 1,
    "universe": ["btcusdt", "ETHUSDT"],
    "strategies": [{"id": "tsmom", "enabled": True, "params": {}}],
}
UNPINNED = {"version": 1, "strategies": [{"id": "tsmom", "enabled": True, "params": {}}]}


def test_a_pinned_universe_is_normalised_and_ordered_as_written() -> None:
    assert parse_registry(PINNED).universe == ("BTCUSDT", "ETHUSDT")
    assert parse_registry(UNPINNED).universe == ()


def test_the_registry_outranks_the_file_but_not_an_explicit_override(tmp_path: Any) -> None:
    """The file is an observation; the registry is a decision; `--symbols` is a person saying otherwise."""
    profile = {"universe": str(tmp_path / "absent.yaml")}
    pinned = parse_registry(PINNED)
    assert resolve_universe(profile, None, tmp_path, registry=pinned) == ["BTCUSDT", "ETHUSDT"]
    assert resolve_universe(profile, ["solusdt"], tmp_path, registry=pinned) == ["SOLUSDT"]


def test_an_unpinned_registry_changes_nothing(tmp_path: Any) -> None:
    """Every registry written before 2026-09-09 has to keep behaving exactly as it did."""
    profile = {"universe": str(tmp_path / "u.yaml")}
    (tmp_path / "u.yaml").write_text(yaml.safe_dump({"always_include": ["ADAUSDT"]}), encoding="utf-8")
    assert resolve_universe(profile, None, tmp_path, registry=parse_registry(UNPINNED)) == ["ADAUSDT"]
    assert resolve_universe(profile, None, tmp_path) == ["ADAUSDT"]


def test_pinning_a_universe_moves_the_registry_digest() -> None:
    """A pinned population that could be edited with no digest moving is one nobody can tell has moved.

    It is the REGISTRY fingerprint rather than the construction one on purpose: M-010's 30-day window
    keys on the construction, and re-ranking the pool must not silently restart the clock the whole
    governance line is waiting on.
    """
    assert (
        registry_fingerprint(parse_registry(PINNED))["digest"]
        != registry_fingerprint(parse_registry(UNPINNED))["digest"]
    )
    assert registry_fingerprint(parse_registry(UNPINNED))["universe"] == []


@pytest.mark.parametrize("pinned", [True, False])
def test_the_engine_only_stops_adopting_when_the_registry_pins(pinned: bool) -> None:
    """`universe_proposal_only` follows the registry, so an unpinned deployment is untouched."""
    from beidou_live.config import live_config

    profile = yaml.safe_load(Path("config/live.demo.yaml").read_text(encoding="utf-8"))
    registry = parse_registry(PINNED if pinned else UNPINNED)
    config = live_config(profile, ["BTCUSDT"], registry, dry_run=True)
    assert config.universe_proposal_only is pinned
