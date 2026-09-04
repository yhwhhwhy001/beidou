"""Strategy registry contract (parsed from ``config/alpha_registry.yaml`` by the caller; no I/O here)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

MAIN_BOOK = "main"
PROBE_VERDICT = "ACCEPT"  # a book-level finding from `research book` (D-018); never a signal-level PASS


@dataclass(frozen=True)
class BookSpec:
    """An independent sleeve next to the main book, scaled to ``fraction`` of the main risk budget (D-019)."""

    name: str
    fraction: float = 1.0

    def __post_init__(self) -> None:
        if self.name == MAIN_BOOK:
            raise ValueError("the main book is implicit and cannot be declared")
        if not 0 < self.fraction <= 1:
            raise ValueError(f"book {self.name}: fraction must be in (0, 1]")


@dataclass(frozen=True)
class StrategyEntry:
    id: str
    enabled: bool = True
    weight: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] | None = None
    book: str = MAIN_BOOK
    probe: dict[str, Any] | None = None  # explicit operator exception for a probe book (D-019)

    @property
    def entry_threshold(self) -> float:
        return float(self.params.get("entry_threshold", 0.0)) or 1e-9


@dataclass(frozen=True)
class Registry:
    version: int
    ensemble_method: str
    turnover_penalty: float
    strategies: tuple[StrategyEntry, ...]
    books: dict[str, BookSpec] = field(default_factory=dict)  # declared non-main books

    @property
    def enabled(self) -> tuple[StrategyEntry, ...]:
        return tuple(entry for entry in self.strategies if entry.enabled)

    def fraction(self, book: str) -> float:
        return 1.0 if book == MAIN_BOOK else self.books[book].fraction


def parse_registry(payload: Mapping[str, Any]) -> Registry:
    ensemble = payload.get("ensemble", {}) or {}
    books: dict[str, BookSpec] = {}
    for name, raw in (payload.get("books", {}) or {}).items():
        spec = raw if isinstance(raw, Mapping) else {}
        books[str(name)] = BookSpec(name=str(name), fraction=float(spec.get("fraction", 1.0)))
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
        probe = raw.get("probe")
        book = str(raw.get("book", MAIN_BOOK) or MAIN_BOOK)
        if book != MAIN_BOOK and book not in books:
            raise ValueError(f"strategy {identifier} refers to undeclared book {book!r}")
        entries.append(
            StrategyEntry(
                id=identifier,
                enabled=bool(raw.get("enabled", True)),
                weight=float(raw.get("weight", 1.0)),
                params=dict(raw.get("params", {}) or {}),
                evidence=dict(evidence) if isinstance(evidence, Mapping) else None,
                book=book,
                probe=dict(probe) if isinstance(probe, Mapping) else None,
            )
        )
    return Registry(
        version=int(payload.get("version", 1)),
        ensemble_method=str(ensemble.get("method", "mean")),
        turnover_penalty=float(ensemble.get("turnover_penalty", 0.0)),
        strategies=tuple(entries),
        books=books,
    )


def registry_fingerprint(
    registry: Registry, canonical: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """The configuration a report was produced under: what runs, not how the file happens to be written.

    A report that decides something about the *ensemble* (``research overlay``) is only valid for the
    registry it was computed against, and a registry can change under a long research run - that is how
    a take-profit conclusion once came to rest on evidence that had expired fifteen minutes earlier.
    Hashing the file cannot express this: editing a comment would change the digest, while a parameter
    edited inside an existing line looks like any other change.  The digest is taken over the enabled
    entries, with parameters canonicalised through each signal's own parameter object so an unwritten
    default and an explicit one agree, plus the ensemble method and the book fractions.  Pure: the
    caller supplies the canonicaliser and does the file I/O.
    """
    strategies = {
        entry.id: {
            "book": entry.book,
            "weight": entry.weight,
            "params": dict(canonical(entry.id, entry.params) if canonical is not None else entry.params),
        }
        for entry in registry.enabled
    }
    payload: dict[str, Any] = {
        "ensemble_method": registry.ensemble_method,
        "turnover_penalty": registry.turnover_penalty,
        "books": {name: spec.fraction for name, spec in sorted(registry.books.items())},
        "strategies": strategies,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {"digest": digest, **payload}


def _same_param(left: Any, right: Any) -> bool:
    """Tolerant equality for values that crossed a YAML/JSON boundary (0.4 vs 0.40, list vs tuple)."""
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(_same_param(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-12)
    return bool(left == right)


CONSTRUCTION_KEYS: tuple[str, ...] = (
    "vol_target",
    "vol_halflife",
    "covariance_halflife",
    "min_asset_vol",
    "max_weight",
    "max_gross",
    "max_scalar",
    "no_trade_band",
    "no_trade_rel_band",
)


def construction_problems(
    entry: StrategyEntry, report: Mapping[str, Any], live_portfolio: Mapping[str, Any] | None
) -> list[str]:
    """The numbers in a strategy's evidence were produced by a portfolio construction; that must match too.

    Adopting P10 cell B made the gap concrete: the live relative band moved 0.25 -> 0.40 while tsmom's
    cited report had been validated at 0.25, and the gate could not see it because it compares signal
    parameters only.  A report written before ``validate`` recorded its construction carries no
    ``portfolio`` block; those are skipped rather than refused, since otherwise nothing in flight today
    could start.  From the first report that carries the block, a silent divergence is refused.
    """
    recorded = report.get("portfolio")
    if not isinstance(recorded, Mapping) or not recorded or live_portfolio is None:
        return []
    return [
        f"{entry.id}: portfolio {key} is {live_portfolio.get(key)!r} live but {recorded.get(key)!r} in the cited evidence"
        for key in CONSTRUCTION_KEYS
        if key in recorded and not _same_param(live_portfolio.get(key), recorded.get(key))
    ]


def evidence_params(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The parameter set a report actually validated: ``best_params``, or the sleeve's params in a book report."""
    if str(report.get("kind", "")) == "book":
        sleeve = report.get("sleeve")
        params = sleeve.get("params") if isinstance(sleeve, Mapping) else None
    else:
        params = report.get("best_params")
    return params if isinstance(params, Mapping) else None


def param_problems(
    entry: StrategyEntry, report: Mapping[str, Any], canonical: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
) -> list[str]:
    """The configuration that runs must be the configuration the cited report validated.

    The digest check proves the report has not been edited; it says nothing about the params next to
    it in the registry.  Without this, editing a parameter silently detaches the live book from its
    evidence - the same failure as KILL-027, one level up.
    """
    validated = evidence_params(report)
    if validated is None:
        return [f"{entry.id}: evidence report records no parameters to check against"]
    live = canonical(entry.id, entry.params)
    cited = canonical(entry.id, validated)
    differing = sorted(key for key in set(live) | set(cited) if live.get(key) != cited.get(key))
    if not differing:
        return []
    detail = ", ".join(f"{key}: registry {live.get(key)!r} vs evidence {cited.get(key)!r}" for key in differing)
    return [f"{entry.id}: registry params differ from the cited evidence ({detail})"]


def evidence_problems(
    entry: StrategyEntry,
    exists: Callable[[str], bool],
    sha256_of: Callable[[str], str],
    *,
    read_report: Callable[[str], Mapping[str, Any]] | None = None,
    book_fraction: float | None = None,
    canonical_params: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    live_portfolio: Mapping[str, Any] | None = None,
) -> list[str]:
    """KILL-015: an enabled strategy must cite a validation report that exists and matches its digest.

    A ``verdict: ACCEPT`` is a *book-level* finding (D-018).  It may only run as a non-main probe book
    with an explicit ``probe`` block (D-019), and when ``read_report`` is supplied the cited report must
    be an ACCEPTed book report about this strategy at the registry's book fraction.
    """
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
    report_ok = not problems
    if verdict == PROBE_VERDICT:
        problems.extend(_probe_problems(entry, path, read_report, book_fraction, report_ok=report_ok))
    elif verdict and verdict not in {"PASS", "WEAK_PASS"}:
        problems.append(f"{entry.id}: evidence verdict {verdict} does not allow live use")
    if read_report is not None and report_ok:
        report = read_report(path)
        if canonical_params is not None:
            problems.extend(param_problems(entry, report, canonical_params))
        problems.extend(construction_problems(entry, report, live_portfolio))
    return problems


def _probe_problems(
    entry: StrategyEntry,
    path: str,
    read_report: Callable[[str], Mapping[str, Any]] | None,
    book_fraction: float | None,
    *,
    report_ok: bool,
) -> list[str]:
    problems: list[str] = []
    if entry.book == MAIN_BOOK:
        problems.append(f"{entry.id}: a book-level ACCEPT can only run as a non-main probe book (D-019)")
    probe = entry.probe or {}
    stop = probe.get("stop") if isinstance(probe.get("stop"), Mapping) else None
    if not probe.get("accepted_by") or not probe.get("accepted_on") or stop is None:
        problems.append(
            f"{entry.id}: a probe book needs an explicit probe block (accepted_by, accepted_on, stop) in the registry"
        )
    else:
        try:
            window = int(stop.get("window_days", 0))
            loss = float(stop.get("max_loss", -1.0))
        except (TypeError, ValueError):
            window, loss = 0, -1.0
        if window <= 0 or loss < 0:
            problems.append(f"{entry.id}: probe stop needs window_days > 0 and max_loss >= 0")
    if read_report is not None and report_ok:
        report = read_report(path)
        if str(report.get("kind", "")) != "book" or str(report.get("book_verdict", "")).upper() != PROBE_VERDICT:
            problems.append(f"{entry.id}: evidence {path} is not an ACCEPTed book report")
        sleeve = str((report.get("sleeve") or {}).get("strategy", ""))
        if sleeve != entry.id:
            problems.append(f"{entry.id}: book report {path} is about {sleeve or '<none>'}")
        fraction = report.get("fraction")
        if book_fraction is None or fraction is None or abs(float(fraction) - float(book_fraction)) > 1e-4:
            problems.append(
                f"{entry.id}: registry book fraction {book_fraction} does not match the evidence fraction {fraction}"
            )
    return problems
