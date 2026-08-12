from __future__ import annotations

import builtins
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from beidou_research.mining.persistence import (
    JSONFileFactorStore,
    LocalArtifactStore,
    PostgreSQLFactorStore,
    SQLiteFactorStore,
    _safe_relative_path,
)


class _Cursor:
    def __init__(self, rows: list[tuple] | None = None, *, fail: bool = False) -> None:
        self.rows = list(rows or [])
        self.fail = fail
        self.executions: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.executions.append((query, params))
        if self.fail:
            raise RuntimeError("database operation failed")

    def fetchall(self) -> list[tuple]:
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, cursors: list[_Cursor] | None = None, *, close_fails: bool = False) -> None:
        self.cursors = list(cursors or [])
        self.close_fails = close_fails
        self.commits = 0
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursors.pop(0) if self.cursors else _Cursor()

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        if self.close_fails:
            raise RuntimeError("close failed")
        self.closed = True


def test_postgres_store_connects_only_with_complete_migrated_schema(monkeypatch) -> None:
    missing_url = PostgreSQLFactorStore(auto_connect=False)
    assert not missing_url._try_connect()
    assert missing_url.last_error == "DATABASE_URL_UNKNOWN"
    with pytest.raises(RuntimeError, match="unavailable"):
        missing_url._verify_schema()

    complete = _Connection([_Cursor([("factor_versions",), ("gate_decisions",)])])
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _dsn: complete))
    store = PostgreSQLFactorStore("postgresql://test", auto_connect=True)
    assert store.is_available

    incomplete = _Connection([_Cursor([("factor_versions",)])])
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _dsn: incomplete))
    unavailable = PostgreSQLFactorStore("postgresql://test", auto_connect=True)
    assert not unavailable.is_available
    assert "missing=['gate_decisions']" in unavailable.last_error

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _dsn: (_ for _ in ()).throw(RuntimeError("offline"))),
    )
    failed = PostgreSQLFactorStore("postgresql://test", auto_connect=True)
    assert not failed.is_available
    assert failed.last_error == "RuntimeError: offline"


def test_postgres_store_crud_and_numeric_version_ordering() -> None:
    cursors = [
        _Cursor(),
        _Cursor([(json.dumps({"score": 1.0}),)]),
        _Cursor(),
        _Cursor([("1.10",), ("1.2",), ("1.3",)]),
        _Cursor(),
        _Cursor(
            [
                (
                    "CANDIDATE",
                    "ACTIVE",
                    "bundle",
                    "PASS",
                    "ok",
                    "operator",
                    "2026-08-12T00:00:00Z",
                )
            ]
        ),
        _Cursor(),
    ]
    connection = _Connection(cursors)
    store = PostgreSQLFactorStore("postgresql://test", auto_connect=False)
    store._conn = connection
    store._available = True

    assert store.save_factor_version("factor", "1.0", {"score": 1.0})
    assert store.get_factor_version("factor", "1.0") == {"score": 1.0}
    assert store.get_factor_version("missing", "1.0") is None
    assert store.list_versions("factor") == ["1.2", "1.3", "1.10"]
    assert store.save_gate_decision(
        "factor",
        {
            "from_state": "CANDIDATE",
            "to_state": "ACTIVE",
            "evidence_bundle_hash": "bundle",
            "decision": "PASS",
            "reason": "ok",
            "operator": "operator",
        },
    )
    history = store.get_gate_history("factor")
    assert history == [
        {
            "from_state": "CANDIDATE",
            "to_state": "ACTIVE",
            "evidence_bundle_hash": "bundle",
            "decision": "PASS",
            "reason": "ok",
            "operator": "operator",
            "decided_at": "2026-08-12T00:00:00Z",
        }
    ]
    assert connection.commits == 2
    store.close()
    assert connection.closed
    assert not store.is_available


@pytest.mark.parametrize(
    ("method", "args", "expected"),
    [
        ("save_factor_version", ("factor", "1.0", {}), False),
        ("get_factor_version", ("factor", "1.0"), None),
        ("list_versions", ("factor",), []),
        ("save_gate_decision", ("factor", {}), False),
        ("get_gate_history", ("factor",), []),
    ],
)
def test_postgres_store_unavailable_paths(method, args, expected) -> None:
    store = PostgreSQLFactorStore(auto_connect=False)
    assert getattr(store, method)(*args) == expected


@pytest.mark.parametrize(
    ("method", "args", "expected", "becomes_unavailable"),
    [
        ("save_factor_version", ("factor", "1.0", {}), False, True),
        ("get_factor_version", ("factor", "1.0"), None, False),
        ("list_versions", ("factor",), [], False),
        ("save_gate_decision", ("factor", {}), False, True),
        ("get_gate_history", ("factor",), [], False),
    ],
)
def test_postgres_store_operation_failures_are_auditable(method, args, expected, becomes_unavailable) -> None:
    store = PostgreSQLFactorStore("postgresql://test", auto_connect=False)
    store._available = True
    store._conn = _Connection([_Cursor(fail=True)])
    assert getattr(store, method)(*args) == expected
    assert store.last_error == "RuntimeError: database operation failed"
    assert store.is_available is (not becomes_unavailable)


def test_postgres_store_close_failure_is_recorded() -> None:
    store = PostgreSQLFactorStore("postgresql://test", auto_connect=False)
    store._available = True
    store._conn = _Connection(close_fails=True)
    store.close()
    assert store.last_error == "RuntimeError: close failed"
    assert store._conn is None
    assert not store.is_available


def test_sqlite_factor_store_round_trip_and_version_ordering(tmp_path) -> None:
    store = SQLiteFactorStore(str(tmp_path / "factors.sqlite"))
    for version in ("1.10", "1.2", "1.3"):
        assert store.save_factor_version("factor", version, {"version": version})

    assert store.list_versions("factor") == ["1.2", "1.3", "1.10"]
    assert store.get_factor_version("factor", "1.2") == {"version": "1.2"}
    assert store.get_factor_version("factor", "missing") is None
    assert store.save_gate_decision(
        "factor",
        {
            "from_state": "CANDIDATE",
            "to_state": "ACTIVE",
            "evidence_bundle_hash": "bundle",
            "decision": "PASS",
            "reason": "verified",
            "operator": "operator",
        },
    )
    history = store.get_gate_history("factor")
    assert len(history) == 1
    assert history[0]["decision"] == "PASS"


def test_sqlite_factor_store_returns_fail_closed_on_database_errors(tmp_path, monkeypatch) -> None:
    store = SQLiteFactorStore(str(tmp_path / "factors.sqlite"))
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("offline")),
    )
    assert not store.save_factor_version("factor", "1.0", {})
    assert store.get_factor_version("factor", "1.0") is None
    assert store.list_versions("factor") == []
    assert not store.save_gate_decision("factor", {})
    assert store.get_gate_history("factor") == []


def test_json_factor_store_is_append_only_and_rejects_path_escape(tmp_path) -> None:
    store = JSONFileFactorStore(str(tmp_path / "factors"))
    for version in ("1.10", "1.2", "1.3"):
        assert store.save_factor_version("factor", version, {"version": version})

    assert store.list_versions("missing") == []
    assert store.list_versions("factor") == ["1.2", "1.3", "1.10"]
    assert store.get_factor_version("factor", "1.10") == {"version": "1.10"}
    assert store.get_factor_version("factor", "missing") is None
    assert store.get_gate_history("missing") == []
    assert store.save_gate_decision("factor", {"decision": "PASS", "sequence": 1})
    assert store.save_gate_decision("factor", {"decision": "FAIL", "sequence": 2})
    history = store.get_gate_history("factor")
    assert [entry["sequence"] for entry in history] == [1, 2]

    for operation in (
        lambda: store.save_factor_version("../escape", "1", {}),
        lambda: store.get_factor_version("factor", "../escape"),
        lambda: store.list_versions("../escape"),
        lambda: store.save_gate_decision("../escape", {}),
        lambda: store.get_gate_history("../escape"),
    ):
        with pytest.raises(ValueError, match="UNSAFE_STORAGE_KEY"):
            operation()
    assert not (tmp_path / "escape").exists()


def test_json_factor_store_reports_corruption_and_write_failures(tmp_path, monkeypatch) -> None:
    store = JSONFileFactorStore(str(tmp_path / "factors"))
    factor_dir = tmp_path / "factors" / "factor"
    factor_dir.mkdir()
    (factor_dir / "broken.json").write_text("not-json")
    assert store.get_factor_version("factor", "broken") is None

    audit_dir = factor_dir / "audit"
    audit_dir.mkdir()
    (audit_dir / "gate-broken.json").write_text("not-json")
    with pytest.raises(ValueError, match="CORRUPT_GATE_AUDIT"):
        store.get_gate_history("factor")

    original_open = builtins.open

    def failing_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("read-only")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    assert not store.save_factor_version("other", "1.0", {})
    assert not store.save_gate_decision("other", {})


def test_local_artifact_store_round_trip_hash_and_path_safety(tmp_path) -> None:
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    digest = store.put("nested/evidence.bin", b"evidence")
    assert digest == "ee8250fb76e094b34b471f13a73dbbe51d1ae142e9df59d7c0d31ec20f0a0a8e"
    assert store.exists("nested/evidence.bin")
    assert store.get("nested/evidence.bin") == b"evidence"
    assert store.get("missing.bin") is None
    assert not store.exists("missing.bin")

    for key in ("", "../escape", "/absolute", "nested/../../escape", "\\absolute"):
        with pytest.raises(ValueError, match="UNSAFE_STORAGE_KEY"):
            store.put(key, b"unsafe")
    assert not (tmp_path / "escape").exists()
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_KEY"):
        _safe_relative_path()
