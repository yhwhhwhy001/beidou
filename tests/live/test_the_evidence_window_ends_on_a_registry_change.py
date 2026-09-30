"""Operator ruling 2026-09-30: the evidence window ends on a registry change too, unless declared the same signals.

The construction fingerprint covers the portfolio layer; the signals are in the registry digest.  Until the
ruling `evidence_window` cut on the construction alone, so a restart that changed only a signal parameter put
two books into one M-010 window - six such switches ran 2026-09-08..15, before M-010 had ever read
(backtest-guard 2026-09-30, `docs/analysis/2026-09-30-backtest-guard-audit.md`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from beidou_live import construction
from beidou_live.report_common import evidence_window
from beidou_live.state import StateStore

BOOK = "c" * 64
OLD, NEW = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
START_MS = 1_790_000_000_000
STEP_MS = 3_600_000


def _store(tmp_path: Path, registries: list[str | None], constructions: list[str] | None = None) -> StateStore:
    store = StateStore(tmp_path / "live")
    books = constructions or [BOOK] * len(registries)
    with store.cycles_path.open("w", encoding="utf-8") as handle:
        for i, (registry, book) in enumerate(zip(registries, books, strict=True)):
            row: dict[str, Any] = {"bar_open_ms": START_MS + i * STEP_MS, "construction": book, "equity": 10_000.0 + i}
            if registry is not None:
                row["registry"] = registry
            handle.write(json.dumps(row) + "\n")
    return store


def test_a_registry_change_under_one_construction_ends_the_window(tmp_path: Path) -> None:
    out = evidence_window(_store(tmp_path, [OLD] * 5 + [NEW] * 3))
    assert out["bars"] == 3, "a signal change ran on inside the old window"
    assert out["since_ms"] == START_MS + 5 * STEP_MS
    assert out["registry"] == NEW
    assert out["changes_7d"] == 0, "the weekly promotion notice reads constructions alone"
    assert out["registry_changes_7d"] == 1


def test_a_declared_registry_alias_does_not_end_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(construction.REGISTRY_ALIASES, NEW, OLD)
    out = evidence_window(_store(tmp_path, [OLD] * 5 + [NEW] * 3))
    assert out["bars"] == 8
    assert out["registry"] == OLD
    assert out["registry_changes_7d"] == 0


def test_rows_written_before_the_loop_recorded_its_registry_do_not_end_the_window(tmp_path: Path) -> None:
    """2026-09-04 03:00 .. 09-06 09:00: 68 armed rows carry no registry digest.  Unknown is not a change."""
    out = evidence_window(_store(tmp_path, [None] * 4 + [OLD] * 3))
    assert out["bars"] == 7
    assert out["registry_changes_7d"] == 0


def test_a_construction_change_still_ends_the_window_when_the_registry_holds(tmp_path: Path) -> None:
    out = evidence_window(_store(tmp_path, [OLD] * 6, ["d" * 64] * 4 + [BOOK] * 2))
    assert out["bars"] == 2
    assert out["changes_7d"] == 1
    assert out["registry_changes_7d"] == 0


def test_every_registry_alias_is_one_hop_between_twelve_character_digests() -> None:
    """`registry_digest` keeps twelve hex characters; a chain would make the answer depend on lookup order."""
    for key, target in construction.REGISTRY_ALIASES.items():
        assert target not in construction.REGISTRY_ALIASES, f"{key} -> {target} is a chain"
        assert len(key) == len(target) == 12, f"{key} -> {target}"
