"""A digest of the data a result was computed from (recovered from V2's dataset manifest / PIT lineage).

Everything else in this system already names itself: a validation report carries a digest, the registry
carries a signal fingerprint, a live cycle carries a construction fingerprint.  The *data* did not, and on
2026-09-04 that gap bit: the point-in-time membership table was rebuilt from monthly to daily at 13:20
while `config/live.demo.yaml` still described it as monthly, and P12 was written against the stale
description.  Nothing was wrong with either the table or the comment on its own - what was missing was
anything that would notice they had stopped agreeing.

Deliberately cheap.  It reads sizes, row counts and boundary timestamps from the parquet footers and the
small JSON files; it does not hash bar contents, because a manifest nobody runs is worth nothing and
hashing a 435 KB membership table plus 879 symbol files on every report would make it optional in
practice.  That is a stated limit, not an oversight: this detects *replacement and drift*, not silent
in-place corruption of individual bars.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

MANIFEST_FIELDS = ("membership", "universe", "klines", "funding")


def _digest(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _file_fact(path: Path) -> dict[str, Any] | None:
    """Size only.  Modification time is deliberately excluded: a rewrite that produces identical data is
    not drift, and a digest that moved every time a job re-ran would be ignored within a week."""
    return {"bytes": path.stat().st_size} if path.exists() else None


def _membership_fact(root: Path) -> dict[str, Any] | None:
    path = root / "membership.parquet"
    if not path.exists():
        return None
    table = pd.read_parquet(path)
    index = pd.DatetimeIndex(table.index)
    members = table.astype(bool)
    return {
        "refreshes": len(table),
        "symbols": len(table.columns),
        "union": int((members.any(axis=0)).sum()),
        "mean_size": round(float(members.sum(axis=1).mean()), 4) if len(table) else 0.0,
        "first": str(index[0]) if len(index) else None,
        "last": str(index[-1]) if len(index) else None,
        # the cadence the table actually has, which is the fact the P12 drift turned on
        "median_gap_hours": (
            round(float(pd.Series(index).diff().dt.total_seconds().median() / 3600.0), 3) if len(index) > 1 else None
        ),
        "file": _file_fact(path),
    }


def _store_fact(root: Path, folder: str, interval: str | None) -> dict[str, Any] | None:
    directory = root / folder
    if not directory.exists():
        return None
    symbols: dict[str, int] = {}
    total = 0
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        target = child / f"{interval}.parquet" if interval else child / "funding.parquet"
        if not target.exists():
            continue
        size = target.stat().st_size
        symbols[child.name] = size
        total += size
    return {"symbols": len(symbols), "bytes": total, "fingerprint": _digest(symbols)}


@dataclass(frozen=True)
class DatasetManifest:
    """What the data looked like when a result was produced."""

    root: str
    interval: str
    membership: dict[str, Any] | None
    universe: dict[str, Any] | None
    klines: dict[str, Any] | None
    funding: dict[str, Any] | None

    @property
    def digest(self) -> str:
        return _digest({field: getattr(self, field) for field in MANIFEST_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "dataset-manifest",
            "root": self.root,
            "interval": self.interval,
            "digest": self.digest,
            **{field: getattr(self, field) for field in MANIFEST_FIELDS},
        }

    def differences(self, other: DatasetManifest) -> list[str]:
        """Human-readable disagreements, so a stale pointer says *what* moved rather than only that it did."""
        out: list[str] = []
        for field in MANIFEST_FIELDS:
            mine, theirs = getattr(self, field), getattr(other, field)
            if mine == theirs:
                continue
            if mine is None or theirs is None:
                out.append(
                    f"{field}: {'absent' if mine is None else 'present'} then, {'absent' if theirs is None else 'present'} now"
                )
                continue
            for key in sorted(set(mine) | set(theirs)):
                if mine.get(key) != theirs.get(key):
                    out.append(f"{field}.{key}: {mine.get(key)} -> {theirs.get(key)}")
        return out


def build_manifest(root: str | Path, interval: str = "1h") -> DatasetManifest:
    """Read the cheap facts about a data root."""
    base = Path(root)
    universe_path = base / "universe.json"
    universe: dict[str, Any] | None = None
    if universe_path.exists():
        payload = json.loads(universe_path.read_text(encoding="utf-8"))
        symbols = [str(s) for s in payload.get("symbols", [])]
        universe = {
            "count": len(symbols),
            "fingerprint": _digest(sorted(symbols)),
            "source": payload.get("source"),
            "selected_at_ms": payload.get("selected_at_ms"),
        }
    return DatasetManifest(
        root=str(base),
        interval=interval,
        membership=_membership_fact(base),
        universe=universe,
        klines=_store_fact(base, "klines", interval),
        funding=_store_fact(base, "funding", None),
    )


def manifest_problems(recorded: dict[str, Any] | None, current: DatasetManifest) -> list[str]:
    """Empty when a report's recorded manifest still describes the data on disk.

    A report written before manifests existed has none; that is reported as unknown provenance rather
    than as agreement, because "no manifest" and "manifest matches" are different facts.
    """
    if not recorded:
        return ["no dataset manifest recorded: this result's data provenance cannot be checked"]
    if recorded.get("digest") == current.digest:
        return []
    previous = DatasetManifest(
        root=str(recorded.get("root", "")),
        interval=str(recorded.get("interval", current.interval)),
        membership=recorded.get("membership"),
        universe=recorded.get("universe"),
        klines=recorded.get("klines"),
        funding=recorded.get("funding"),
    )
    changes = previous.differences(current)
    return [f"dataset changed since this result was produced: {', '.join(changes)}"] if changes else []
