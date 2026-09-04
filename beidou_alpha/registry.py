"""Strategy registry contract (parsed from ``config/alpha_registry.yaml`` by the caller; no I/O here)."""

from __future__ import annotations

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
    if read_report is not None and canonical_params is not None and report_ok:
        problems.extend(param_problems(entry, read_report(path), canonical_params))
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
