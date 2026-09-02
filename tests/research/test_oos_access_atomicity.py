"""ARO-OOS-003: one-time OOS access must be serialized across OS processes."""

from __future__ import annotations

import fcntl
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any

import pytest

from beidou_research.experiments.oos_seal import OOSAccessInvalidated, OOSBoundary, OOSSeal, OOSSealStore

KEY = b"0123456789abcdef0123456789abcdef"
AUDIT_KEY = b"abcdef0123456789abcdef0123456789"


def _boundary() -> OOSBoundary:
    return OOSBoundary(
        train_end="2026-08-31T23:59:59Z",
        oos_start="2026-09-01T00:00:00Z",
        oos_end="2026-09-30T23:00:00Z",
        timezone="UTC",
    )


def _race_worker(
    root: str,
    seal_payload: dict[str, Any],
    start: Any,
    results: Any,
) -> None:
    store = OOSSealStore(root)
    seal = OOSSeal.from_dict(seal_payload)
    original_append = store._append

    def slow_append(event: Any) -> None:
        time.sleep(0.2)
        original_append(event)

    store._append = slow_append  # type: ignore[method-assign]
    start.wait(timeout=10)
    try:
        receipt = store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id="atomic-run",
            experiment_identity_digest="4" * 64,
            checkpoint_digest="5" * 64,
            accessed_at="2026-10-01T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest="6" * 64,
            purpose="atomic-race",
        )
        results.put(("SUCCESS", receipt.sequence, receipt.audit_head))
    except OOSAccessInvalidated as exc:
        results.put(("DENIED", str(exc), ""))
    except Exception as exc:  # pragma: no cover - surfaced in parent assertion
        results.put(("ERROR", type(exc).__name__, str(exc)))


def _abandon_lock(lock_path: str) -> None:
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    os._exit(0)


def _seal(store: OOSSealStore) -> OOSSeal:
    return store.create(
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        lineage_digest="3" * 64,
        run_id="atomic-run",
        experiment_identity_digest="4" * 64,
        checkpoint_digest="5" * 64,
        sealed_at="2026-09-01T00:00:00Z",
        evaluation_not_before="2026-10-01T00:00:00Z",
    )


def test_two_os_processes_receive_exactly_one_unlock_capable_receipt(tmp_path: Path) -> None:
    root = tmp_path / "race"
    store = OOSSealStore(root)
    seal = _seal(store)
    context = mp.get_context("spawn")
    start = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_race_worker, args=(str(root), seal.as_dict(), start, results)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in processes]
    assert sum(outcome[0] == "SUCCESS" for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] == "DENIED" and "REPEATED_OOS_ACCESS" in outcome[1] for outcome in outcomes) == 1


def test_dead_lock_owner_is_released_and_preappend_attempt_can_retry(tmp_path: Path) -> None:
    root = tmp_path / "dead-owner"
    store = OOSSealStore(root)
    seal = _seal(store)
    context = mp.get_context("spawn")
    process = context.Process(target=_abandon_lock, args=(str(store.lock_path),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id="atomic-run",
        experiment_identity_digest="4" * 64,
        checkpoint_digest="5" * 64,
        accessed_at="2026-10-01T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="6" * 64,
        purpose="post-dead-owner",
    )
    assert receipt.sequence == 1


def test_failure_before_audit_append_can_retry_without_duplicate_claim(tmp_path: Path) -> None:
    store = OOSSealStore(tmp_path / "preappend")
    seal = _seal(store)
    original_append = store._append

    def fail_before_append(_event: Any) -> None:
        raise RuntimeError("simulated-preappend-failure")

    store._append = fail_before_append  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated-preappend-failure"):
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id="atomic-run",
            experiment_identity_digest="4" * 64,
            checkpoint_digest="5" * 64,
            accessed_at="2026-10-01T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest="6" * 64,
            purpose="preappend-failure",
        )
    assert store.audit_events() == ()
    store._append = original_append  # type: ignore[method-assign]
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id="atomic-run",
        experiment_identity_digest="4" * 64,
        checkpoint_digest="5" * 64,
        accessed_at="2026-10-01T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="6" * 64,
        purpose="preappend-retry",
    )
    assert receipt.sequence == 1


def test_partial_audit_is_permanently_fail_closed_without_reconstructed_receipt(tmp_path: Path) -> None:
    store = OOSSealStore(tmp_path / "partial")
    seal = _seal(store)
    store.audit_path.write_bytes(b'{"sequence":1')
    with pytest.raises(Exception, match="INVALID_OOS_AUDIT_EVENT"):
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id="atomic-run",
            experiment_identity_digest="4" * 64,
            checkpoint_digest="5" * 64,
            accessed_at="2026-10-01T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest="6" * 64,
            purpose="partial-audit",
        )
