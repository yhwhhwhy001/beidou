"""Coverage-gap tests for two G5 scenario modules."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from beidou_certification.g5_scenarios.engine import reconciliation_mismatch as rm
from beidou_certification.g5_scenarios.protocol.create_query_cancel import _get_order_with_retry
from beidou_exchange.core.error_taxonomy import Result


def test_get_order_with_retry_exhausts_attempts() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def get_order(self, symbol, order_id):
            self.calls += 1
            return Result.failure("Order does not exist")

    client = Client()
    last, attempts = asyncio.run(_get_order_with_retry(client, "BTCUSDT", 1, attempts=2))

    assert attempts == 2
    assert client.calls == 2
    assert last.is_ok is False


def test_reconciliation_mismatch_finally_records_journal_delete_failure(tmp_path, monkeypatch) -> None:
    baseline = {"balance_amount": "100", "positions": {}}

    class FakeConn:
        def close(self) -> None:
            pass

    conn = FakeConn()
    journal_reads = 0

    def fake_read(conn, record_type, record_id):
        nonlocal journal_reads
        if record_type == rm._BASELINE_RECORD_TYPE:
            return dict(baseline)
        journal_reads += 1
        if journal_reads == 1:
            return None
        return {"payload": "stranded"}

    def fake_delete(conn, record_type, record_id):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(rm, "psycopg", SimpleNamespace(connect=lambda *a, **k: conn))
    monkeypatch.setattr(rm, "_read_record", fake_read)
    monkeypatch.setattr(rm, "_upsert_record", lambda *a, **k: None)
    monkeypatch.setattr(rm, "_delete_record", fake_delete)

    scenario = rm.ReconciliationMismatchScenario(now=lambda: 0.0, sleep=asyncio.sleep)

    async def boom(conn, original_payload, steps, started):
        raise RuntimeError("boom")

    monkeypatch.setattr(scenario, "_execute_flow", boom)

    ctx = SimpleNamespace(
        client=None,
        ledger=SimpleNamespace(record=lambda *_a, **_k: None),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=False,
    )
    result = asyncio.run(scenario.run(ctx))
    steps = result.evidence["steps"]
    assert any(step.get("action") == "journal_deleted_finally" and step.get("ok") is False for step in steps)
