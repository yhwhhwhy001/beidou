"""Append-only local JSONL custody for T08 decision records."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .contracts import GENESIS_HASH, PortfolioDecision, canonical_digest, canonical_json


class DecisionStoreError(RuntimeError):
    """A decision record was not accepted."""


class DecisionStoreNotVerifiableError(DecisionStoreError):
    """Existing decision history cannot be independently verified."""


DecisionStoreNotVerifiable = DecisionStoreNotVerifiableError


class AppendOnlyDecisionStore:
    """Hash-chained immutable decision generations; no update/delete surface."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise DecisionStoreNotVerifiable("DECISION_RECORD_NOT_OBJECT")
                records.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DecisionStoreNotVerifiable("DECISION_STORE_INVALID_JSONL") from exc
        return records

    def _validate(self, records: list[dict[str, Any]]) -> None:
        previous_hash = GENESIS_HASH
        previous_decision_id: str | None = None
        seen_decisions: set[str] = set()
        required = {
            "sequence",
            "previous_record_hash",
            "decision_digest",
            "record_hash",
            "decision",
        }
        for sequence, record in enumerate(records, start=1):
            if set(record) != required or record["sequence"] != sequence:
                raise DecisionStoreNotVerifiable("DECISION_SEQUENCE_INVALID")
            if record["previous_record_hash"] != previous_hash:
                raise DecisionStoreNotVerifiable("DECISION_CHAIN_FORKED")
            decision = record["decision"]
            if not isinstance(decision, dict):
                raise DecisionStoreNotVerifiable("DECISION_PAYLOAD_INVALID")
            if canonical_digest(decision) != record["decision_digest"]:
                raise DecisionStoreNotVerifiable("DECISION_DIGEST_MISMATCH")
            scope = {key: value for key, value in record.items() if key != "record_hash"}
            if canonical_digest(scope) != record["record_hash"]:
                raise DecisionStoreNotVerifiable("DECISION_RECORD_HASH_MISMATCH")
            decision_id = decision.get("decision_id")
            if not isinstance(decision_id, str) or len(decision_id) != 64 or decision_id in seen_decisions:
                raise DecisionStoreNotVerifiable("DECISION_ID_INVALID")
            if decision.get("promotable") is not False or decision.get("side_effects") != []:
                raise DecisionStoreNotVerifiable("RUNTIME_SIDE_EFFECT_REACHABLE")
            if sequence == 1 and decision.get("supersedes_decision_id") is not None:
                raise DecisionStoreNotVerifiable("SUPERSESSION_CHAIN_INVALID")
            if sequence > 1 and decision.get("supersedes_decision_id") != previous_decision_id:
                raise DecisionStoreNotVerifiable("SUPERSESSION_CHAIN_INVALID")
            seen_decisions.add(decision_id)
            previous_decision_id = decision_id
            previous_hash = record["record_hash"]

    def append(self, decision: PortfolioDecision) -> dict[str, Any]:
        records = self._records()
        self._validate(records)
        payload = decision.as_dict()
        if decision.promotable or decision.side_effects:
            raise DecisionStoreError("RUNTIME_SIDE_EFFECT_REACHABLE")
        existing = next(
            (record for record in records if record["decision"]["decision_id"] == decision.decision_id), None
        )
        if existing is not None:
            if existing["decision"] == payload:
                return existing
            raise DecisionStoreError("DECISION_ID_CONFLICT")
        if records and decision.supersedes_decision_id != records[-1]["decision"]["decision_id"]:
            raise DecisionStoreError("SUPERSESSION_CHAIN_INVALID")
        if not records and decision.supersedes_decision_id is not None:
            raise DecisionStoreError("SUPERSESSION_CHAIN_INVALID")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "sequence": len(records) + 1,
            "previous_record_hash": records[-1]["record_hash"] if records else GENESIS_HASH,
            "decision_digest": canonical_digest(payload),
            "decision": payload,
        }
        record["record_hash"] = canonical_digest(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def replay(self) -> dict[str, Any]:
        records = self._records()
        self._validate(records)
        return {
            "record_count": len(records),
            "record_hashes": [record["record_hash"] for record in records],
            "decision_ids": [record["decision"]["decision_id"] for record in records],
            "head_hash": records[-1]["record_hash"] if records else GENESIS_HASH,
        }


def rollback_decision_history(store: AppendOnlyDecisionStore) -> dict[str, Any]:
    """Disable future issuance conceptually while preserving verified bytes."""

    replay = store.replay()
    return {
        "new_decisions_enabled": False,
        "history_preserved": True,
        "record_count": replay["record_count"],
        "head_hash": replay["head_hash"],
        "paper_testnet_account_capital_side_effects": False,
    }


__all__ = [
    "AppendOnlyDecisionStore",
    "DecisionStoreError",
    "DecisionStoreNotVerifiable",
    "rollback_decision_history",
]
