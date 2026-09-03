"""Durable, human-readable live state: state.json, trades.jsonl, attribution.jsonl, cycles.jsonl, heartbeat.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class LiveState:
    last_bar_ms: int | None = None
    last_targets: dict[str, float] = field(default_factory=dict)
    last_contributions: dict[str, dict[str, float]] = field(default_factory=dict)
    last_equity: float | None = None
    day: str | None = None
    day_start_equity: float | None = None
    last_income_ms: int | None = None
    leverage_set: dict[str, int] = field(default_factory=dict)
    exit_states: dict[str, dict[str, Any]] = field(default_factory=dict)  # per-symbol exit overlay state (D-012)
    equity_hwm: float | None = None  # high-water mark for the drawdown throttle (D-015)
    universe: list[str] = field(default_factory=list)  # last refreshed universe (D-014)
    universe_day: str | None = None
    leaving: list[str] = field(default_factory=list)  # symbols that left the universe but still hold a position
    stopped_books: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # probe books closed by their stop rule (D-019)
    consecutive_errors: int = 0
    cycles: int = 0
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LiveState:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)


class StateStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self.trades_path = self.directory / "trades.jsonl"
        self.attribution_path = self.directory / "attribution.jsonl"
        self.cycles_path = self.directory / "cycles.jsonl"
        self.heartbeat_path = self.directory / "heartbeat.json"

    def load(self) -> LiveState:
        if not self.state_path.exists():
            return LiveState()
        try:
            return LiveState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            return LiveState()

    def save(self, state: LiveState) -> None:
        state.updated_at = utc_now_iso()
        self._atomic_write(self.state_path, json.dumps(state.to_dict(), indent=2, sort_keys=True))

    def heartbeat(self, payload: dict[str, Any]) -> None:
        self._atomic_write(self.heartbeat_path, json.dumps({"at": utc_now_iso(), **payload}, indent=2, sort_keys=True))

    def read_heartbeat(self) -> dict[str, Any] | None:
        if not self.heartbeat_path.exists():
            return None
        try:
            payload = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def append_trade(self, record: dict[str, Any]) -> None:
        self._append(self.trades_path, record)

    def append_attribution(self, record: dict[str, Any]) -> None:
        self._append(self.attribution_path, record)

    def append_cycle(self, record: dict[str, Any]) -> None:
        self._append(self.cycles_path, record)

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": utc_now_iso(), **record}, sort_keys=True, default=str) + "\n")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(path)
