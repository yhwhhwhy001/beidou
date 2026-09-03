"""Strategy registry contract (parsed from ``config/alpha_registry.yaml`` by the caller; no I/O here)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategyEntry:
    id: str
    enabled: bool = True
    weight: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] | None = None

    @property
    def entry_threshold(self) -> float:
        return float(self.params.get("entry_threshold", 0.0)) or 1e-9


@dataclass(frozen=True)
class Registry:
    version: int
    ensemble_method: str
    turnover_penalty: float
    strategies: tuple[StrategyEntry, ...]

    @property
    def enabled(self) -> tuple[StrategyEntry, ...]:
        return tuple(entry for entry in self.strategies if entry.enabled)


def parse_registry(payload: Mapping[str, Any]) -> Registry:
    ensemble = payload.get("ensemble", {}) or {}
    entries: list[StrategyEntry] = []
    seen: set[str] = set()
    for raw in payload.get("strategies", []) or []:
        if not isinstance(raw, Mapping) or not raw.get("id"):
            raise ValueError("every strategy needs an id")
        identifier = str(raw["id"])
        if identifier in seen:
            raise ValueError(f"duplicate strategy id {identifier}")
        seen.add(identifier)
        evidence = raw.get("evidence")
        entries.append(
            StrategyEntry(
                id=identifier,
                enabled=bool(raw.get("enabled", True)),
                weight=float(raw.get("weight", 1.0)),
                params=dict(raw.get("params", {}) or {}),
                evidence=dict(evidence) if isinstance(evidence, Mapping) else None,
            )
        )
    return Registry(
        version=int(payload.get("version", 1)),
        ensemble_method=str(ensemble.get("method", "mean")),
        turnover_penalty=float(ensemble.get("turnover_penalty", 0.0)),
        strategies=tuple(entries),
    )


def evidence_problems(
    entry: StrategyEntry, exists: Callable[[str], bool], sha256_of: Callable[[str], str]
) -> list[str]:
    """KILL-015: an enabled strategy must cite a validation report that exists and matches its digest."""
    if not entry.enabled:
        return []
    if not entry.evidence:
        return [f"{entry.id}: enabled without evidence"]
    path = str(entry.evidence.get("report", ""))
    digest = str(entry.evidence.get("sha256", "")).lower()
    problems: list[str] = []
    if not path or not exists(path):
        problems.append(f"{entry.id}: evidence report missing: {path or '<none>'}")
    elif len(digest) != 64 or sha256_of(path) != digest:
        problems.append(f"{entry.id}: evidence digest mismatch for {path}")
    verdict = str(entry.evidence.get("verdict", "")).upper()
    if verdict and verdict not in {"PASS", "WEAK_PASS"}:
        problems.append(f"{entry.id}: evidence verdict {verdict} does not allow live use")
    return problems
