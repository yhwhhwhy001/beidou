from __future__ import annotations

import sys
from types import SimpleNamespace

from scripts.run_migrations import run_migrations


class _FakeCursor:
    def __init__(self, *, existing_checksum: str | None = None) -> None:
        self.existing_checksum = existing_checksum
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, _params=()):
        self.statements.append(sql)

    def fetchone(self):
        return (self.existing_checksum,) if self.existing_checksum is not None else None


class _FakeConnection:
    def __init__(self, *, existing_checksum: str | None = None) -> None:
        self.cursor_state = _FakeCursor(existing_checksum=existing_checksum)
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, _params=()):
        self.statements.append(sql)

    def cursor(self):
        return self.cursor_state

    def commit(self):
        return None


def test_v3_migration_contains_fact_chain_tables_and_no_destructive_sql() -> None:
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "migrations/002_v3_fact_chain.up.sql").read_text()
    for table in (
        "v3_reconciliation_snapshots",
        "v3_reconciliation_results",
        "v3_user_stream_events",
        "v3_user_stream_projections",
        "v3_ledger_transactions",
        "v3_ledger_postings",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "PENDING" in sql and "APPLIED" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_execution_outbox_migration_contains_atomic_and_fencing_tables() -> None:
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "migrations/003_execution_outbox.up.sql").read_text()
    for table in (
        "v3_order_intents",
        "v3_risk_approvals",
        "v3_transactional_outbox",
        "v3_outbox_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "fencing_token" in sql
    assert "intent_hash" in sql
    assert "FOR UPDATE" not in sql  # locking belongs to the worker query, not DDL
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_risk_approval_intent_hash_migration_is_forward_only() -> None:
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "migrations/005_risk_approval_intent_hash.up.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS intent_hash" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_runtime_records_migration_is_forward_only_and_append_audited() -> None:
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "migrations/004_runtime_records.up.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS v3_runtime_records" in sql
    assert "CREATE TABLE IF NOT EXISTS v3_runtime_events" in sql
    assert "PRIMARY KEY (record_type, record_id)" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_migration_runner_bootstraps_schema_and_applies_forward_only(tmp_path, monkeypatch) -> None:
    migration = tmp_path / "001_test.up.sql"
    migration.write_text("CREATE TABLE test_fact (id INTEGER);")
    connections: list[_FakeConnection] = []

    def connect(_dsn: str):
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    result = run_migrations(str(tmp_path), "postgresql://test")
    assert result["errors"] == []
    assert result["applied"] == ["001_test.up.sql"]
    assert "schema_migrations" in connections[0].statements[0]


def test_migration_runner_rejects_checksum_drift(tmp_path, monkeypatch) -> None:
    migration = tmp_path / "001_test.up.sql"
    migration.write_text("CREATE TABLE test_fact (id INTEGER);")

    def connect(_dsn: str):
        # Bootstrap connection is empty; migration connection reports a
        # different checksum for an already applied version.
        if not hasattr(connect, "calls"):
            connect.calls = 0
        connect.calls += 1
        return _FakeConnection(existing_checksum="not-the-file-hash" if connect.calls == 2 else None)

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    result = run_migrations(str(tmp_path), "postgresql://test")
    assert result["applied"] == []
    assert any("checksum mismatch" in error for error in result["errors"])
