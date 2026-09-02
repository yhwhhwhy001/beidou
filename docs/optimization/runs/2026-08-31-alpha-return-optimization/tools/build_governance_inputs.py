"""Build verified governance inputs without starting Alpha candidate search.

Outputs cover the externally frozen Metric Owner policy and a prospective
future OOS window-custody record.  The latter is intentionally a preseal draft:
it binds a real future window before any read, but cannot become a governed v2
seal until a separately authorized candidate is frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from beidou_research.experiments import (  # noqa: E402
    OOSBoundary,
    OOSWindowCustodySpecV2,
    assess_oos_preseal_v2,
    canonical_json,
)
from beidou_research.validation.contracts import load_metric_owner_policy  # noqa: E402

OUTPUT_ROOT = (
    REPOSITORY_ROOT
    / "artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/inputs"
)
AUTHORIZATION_ROOT = Path("/Users/maguannan/beidou-authorization/BD-AF-P3-T07")
METRIC_POLICY = AUTHORIZATION_ROOT / "metric-owner-policy.json"
APPROVAL = AUTHORIZATION_ROOT / "BD-AF-P3-T07.approval.json"
APPROVAL_SIGNATURE = AUTHORIZATION_ROOT / "BD-AF-P3-T07.approval.json.sig"
REVIEW = AUTHORIZATION_ROOT / "BD-AF-P3-T07.reviewer-record.json"
REVIEW_SIGNATURE = AUTHORIZATION_ROOT / "BD-AF-P3-T07.reviewer-record.json.sig"
TRUST_ROOT = Path(
    "/Users/maguannan/beidou-authorization/BD-AF-P0P3-V2/approval-trust-root.allowed_signers"
)
APPROVAL_NAMESPACE = "beidou-alpha-first-task-approval-v1"
REVIEW_NAMESPACE = "beidou-alpha-first-task-review-v1"
TASK_CUSTODIAN_ID = "codex-task-01a0546f-f56c-75c0-9392-90fd3d492377"
OOS_SYMBOLS = ("BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")
OOS_START = "2026-10-01T00:00:00Z"
OOS_END = "2026-10-31T23:00:00Z"
TRAIN_END = "2026-09-30T23:59:59Z"
FIRST_READ_NOT_BEFORE = "2026-11-02T00:00:00Z"
SOURCE_ENDPOINT = (
    "https://data.binance.vision/data/futures/um/monthly/klines/"
    "BTCUSDT/1h/BTCUSDT-1h-2026-10.zip"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC_TIMESTAMP_REQUIRED")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError("UTC_TIMESTAMP_REQUIRED")
    return result


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _write_once(path: Path, value: object) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"IMMUTABLE_GOVERNANCE_ARTIFACT_MISMATCH:{path}")
        return
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()


def _write_report(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def verify_ssh_signature(
    *, message_path: Path, signature_path: Path, identity: str, namespace: str
) -> dict[str, object]:
    command = [
        "ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(TRUST_ROOT),
        "-I",
        identity,
        "-n",
        namespace,
        "-s",
        str(signature_path),
    ]
    completed = subprocess.run(  # noqa: S603 - fixed ssh-keygen argv, no shell
        command,
        input=message_path.read_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
    return {
        "status": "VERIFIED" if completed.returncode == 0 else "FAILED",
        "exit_code": completed.returncode,
        "identity": identity,
        "namespace": namespace,
        "message_path": str(message_path),
        "message_sha256": _sha256_file(message_path),
        "signature_path": str(signature_path),
        "signature_sha256": _sha256_file(signature_path),
        "trust_root_path": str(TRUST_ROOT),
        "trust_root_sha256": _sha256_file(TRUST_ROOT),
        "tool_observation": output,
    }


def verify_metric_owner() -> dict[str, object]:
    policy = load_metric_owner_policy(METRIC_POLICY)
    approval = json.loads(APPROVAL.read_text(encoding="utf-8"))
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    owner = policy.document["owner"]
    identity = str(owner["identity"])
    approval_signature = verify_ssh_signature(
        message_path=APPROVAL,
        signature_path=APPROVAL_SIGNATURE,
        identity=identity,
        namespace=APPROVAL_NAMESPACE,
    )
    review_signature = verify_ssh_signature(
        message_path=REVIEW,
        signature_path=REVIEW_SIGNATURE,
        identity=identity,
        namespace=REVIEW_NAMESPACE,
    )
    reasons: list[str] = []
    expected_bindings = {
        "METRIC_OWNER": identity,
        "METRIC_OWNER_POLICY_ID": policy.document["policy_id"],
        "METRIC_OWNER_POLICY_VERSION": policy.document["policy_version"],
        "METRIC_OWNER_POLICY_SHA256": policy.source_sha256,
        "METRIC_OWNER_POLICY_DIGEST": policy.digest,
    }
    if approval.get("owner_bindings") != expected_bindings:
        reasons.append("APPROVAL_OWNER_BINDING_MISMATCH")
    if approval_signature["status"] != "VERIFIED":
        reasons.append("APPROVAL_SIGNATURE_INVALID")
    if review_signature["status"] != "VERIFIED":
        reasons.append("REVIEW_SIGNATURE_INVALID")
    if review.get("approval_sha256") != _sha256_file(APPROVAL):
        reasons.append("REVIEW_APPROVAL_HASH_MISMATCH")
    if review.get("approval_signature_sha256") != _sha256_file(APPROVAL_SIGNATURE):
        reasons.append("REVIEW_APPROVAL_SIGNATURE_HASH_MISMATCH")
    expires_at = datetime.fromisoformat(str(approval["expires_at"]))
    if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        reasons.append("APPROVAL_EXPIRED")
    return {
        "schema_version": "1.0",
        "status": "PASS_WITH_SCOPE_LIMIT" if not reasons else "FAIL",
        "reasons": reasons,
        "policy": {
            "path": str(METRIC_POLICY),
            "policy_id": policy.document["policy_id"],
            "policy_version": policy.document["policy_version"],
            "policy_status": policy.document["status"],
            "source_sha256": policy.source_sha256,
            "semantic_digest": policy.digest,
        },
        "owner": {
            "role": owner["role"],
            "identity": identity,
            "approval_id": owner["approval_id"],
            "confirmed_at": owner["confirmed_at"],
        },
        "approval": {
            "package_id": approval["package_id"],
            "task_id": approval["task_id"],
            "scope": approval["scope"],
            "baseline_commit": approval["baseline_commit"],
            "approved_worktree": approval["approved_worktree"],
            "protected_source_worktree": approval["protected_source_worktree"],
            "expires_at": approval["expires_at"],
            "signature": approval_signature,
        },
        "independent_review": {
            "decision": review["decision"],
            "reviewed_at": review["reviewed_at"],
            "signature": review_signature,
        },
        "applicability": {
            "metric_semantics_for_this_run": "VERIFIED_FROZEN_POLICY",
            "current_candidate_authorization": "NOT_GRANTED",
            "trading_or_runtime_authorization": "NOT_GRANTED",
            "reason": (
                "The signed envelope is authoritative for the frozen Metric Owner policy and its "
                "original task only; its baseline/worktree scope is not relabelled as current-run "
                "candidate, deployment, or trading authorization."
            ),
        },
        "verified_at": _utc_now(),
    }


def future_contracts() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_core: dict[str, object] = {
        "schema_version": "1.0",
        "contract_id": "ARO-OOS-OCT2026-PUBLIC-ARCHIVE-SOURCE",
        "venue": "BINANCE_USDM",
        "symbols": list(OOS_SYMBOLS),
        "interval": "1h",
        "timezone": "UTC",
        "window": {"start_inclusive": OOS_START, "end_inclusive": OOS_END},
        "endpoint_template": (
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            "{symbol}/1h/{symbol}-1h-2026-10.zip"
        ),
        "source_class": "OFFICIAL_PUBLIC_ARCHIVE",
        "environment": "PUBLIC_READ_ONLY",
        "intended_use": "ECONOMIC_RESEARCH",
        "required_controls": [
            "published_sha256_checksum_per_archive",
            "exact_744_closed_hourly_rows_per_symbol",
            "independent_public_api_row_reconciliation",
            "no_redirect_or_unlisted_host",
            "no_account_or_order_endpoint",
        ],
        "read_prohibition_before": FIRST_READ_NOT_BEFORE,
    }
    source = {**source_core, "source_contract_digest": _canonical_digest(source_core)}
    dataset_core: dict[str, object] = {
        "schema_version": "1.0",
        "manifest_kind": "PROSPECTIVE_DATASET_ACQUISITION_CONTRACT",
        "status": "SEALED_FUTURE_CONTRACT_NO_DATA_ACCESSED",
        "venue": "BINANCE_USDM",
        "symbols": list(OOS_SYMBOLS),
        "interval": "1h",
        "timezone": "UTC",
        "start_inclusive": OOS_START,
        "end_exclusive": "2026-11-01T00:00:00Z",
        "expected_rows_per_symbol": 744,
        "source_contract_digest": source["source_contract_digest"],
        "content_hashes": "UNAVAILABLE_BY_DESIGN_UNTIL_AUTHORIZED_FIRST_READ",
        "candidate_search": False,
        "oos_data_accessed": False,
    }
    dataset = {**dataset_core, "prospective_dataset_contract_digest": _canonical_digest(dataset_core)}
    lineage_core: dict[str, object] = {
        "schema_version": "1.0",
        "manifest_kind": "PROSPECTIVE_PIT_LINEAGE_CONTRACT",
        "status": "SEALED_FUTURE_CONTRACT_NO_DATA_ACCESSED",
        "required_roles": [
            "dataset",
            "universe_membership",
            "corporate_actions",
            "revisions",
            "calendar",
            "timestamps",
            "timezone",
            "features",
            "labels",
            "costs",
        ],
        "causality_rule": (
            "every feature record available_at must be <= decision_at and strictly before its "
            "bound label_available_at"
        ),
        "dataset_contract_digest": dataset["prospective_dataset_contract_digest"],
        "content_hashes": "UNAVAILABLE_BY_DESIGN_UNTIL_AUTHORIZED_FIRST_READ",
        "candidate_search": False,
        "oos_data_accessed": False,
    }
    lineage = {**lineage_core, "prospective_pit_contract_digest": _canonical_digest(lineage_core)}
    return source, dataset, lineage


def build_window_custody(
    *,
    custody_sealed_at: datetime,
    source: Mapping[str, object],
    dataset: Mapping[str, object],
    lineage: Mapping[str, object],
) -> OOSWindowCustodySpecV2:
    return OOSWindowCustodySpecV2(
        boundary=OOSBoundary(
            train_end=TRAIN_END,
            oos_start=OOS_START,
            oos_end=OOS_END,
            timezone="UTC",
        ),
        venue="BINANCE_USDM",
        symbols=OOS_SYMBOLS,
        interval="1h",
        source_endpoint=SOURCE_ENDPOINT,
        source_contract_digest=str(source["source_contract_digest"]),
        dataset_manifest_digest=str(dataset["prospective_dataset_contract_digest"]),
        pit_manifest_digest=str(lineage["prospective_pit_contract_digest"]),
        custodian_id=TASK_CUSTODIAN_ID,
        custody_sealed_at=custody_sealed_at,
        first_read_not_before=_parse_utc(FIRST_READ_NOT_BEFORE),
    )


def create(*, output: Path = OUTPUT_ROOT) -> dict[str, object]:
    output = output.resolve()
    owner_path = output / "metric-owner-verification.json"
    owner_report = verify_metric_owner()
    if owner_report["status"] != "PASS_WITH_SCOPE_LIMIT":
        raise RuntimeError("METRIC_OWNER_VERIFICATION_FAILED")
    if owner_path.exists():
        existing_owner = json.loads(owner_path.read_text(encoding="utf-8"))
        existing_verified_at = existing_owner.get("verified_at")
        if not isinstance(existing_verified_at, str):
            raise RuntimeError("METRIC_OWNER_VERIFICATION_TIMESTAMP_INVALID")
        _parse_utc(existing_verified_at)
        owner_report["verified_at"] = existing_verified_at
    source, dataset, lineage = future_contracts()
    window_path = output / "oos-window-custody-v2.json"
    if window_path.exists():
        existing = json.loads(window_path.read_text(encoding="utf-8"))
        custody_sealed_at = _parse_utc(existing["custody_sealed_at"])
    else:
        custody_sealed_at = datetime.now(timezone.utc)
    window = build_window_custody(
        custody_sealed_at=custody_sealed_at,
        source=source,
        dataset=dataset,
        lineage=lineage,
    )
    window_record: dict[str, object] = {
        **window.canonical_payload(),
        "window_custody_digest": window.digest,
        "custodian_kind": "LOCAL_CODEX_TASK_PROCESS",
        "custodian_authority_source": "USER_AUTHORIZED_CURRENT_TASK",
        "human_custodian_signature": "ABSENT_NOT_CLAIMED",
        "institutional_custody": "NOT_VERIFIABLE",
        "oos_data_accessed": False,
        "governed_v2_seal_created": False,
    }
    preseal = assess_oos_preseal_v2(window=window, candidate=None)
    assessment = {
        "schema_version": preseal.schema_version,
        "status": preseal.status,
        "reasons": list(preseal.reasons),
        "eligible_for_v2_seal": preseal.eligible_for_v2_seal,
        "assessment_digest": preseal.assessment_digest,
        "window_custody_digest": window.digest,
        "candidate_freeze_digest": None,
        "candidate_search_started": False,
        "oos_data_accessed": False,
        "governed_v2_seal_created": False,
    }
    _write_once(owner_path, owner_report)
    _write_once(output / "future-source-contract.json", source)
    _write_once(output / "future-dataset-contract.json", dataset)
    _write_once(output / "future-pit-lineage-contract.json", lineage)
    _write_once(window_path, window_record)
    _write_once(output / "oos-preseal-assessment-v2.json", assessment)
    report: dict[str, object] = {
        "schema_version": "1.0",
        "report_id": "ARO-07-GOVERNANCE-INPUTS",
        "status": "PASS_WITH_CONDITIONS",
        "metric_owner_status": "VERIFIED_FROZEN_POLICY",
        "window_custody_status": "SEALED_FUTURE_WINDOW_CONTRACT",
        "preseal_status": preseal.status,
        "preseal_reasons": list(preseal.reasons),
        "real_unknown_window": True,
        "oos_data_accessed": False,
        "candidate_search_started": False,
        "candidate_freeze_created": False,
        "governed_v2_seal_created": False,
        "limitations": [
            "The local Codex task is the recorded process custodian; no human or institutional "
            "custody signature is claimed.",
            "The prospective dataset/PIT digests bind acquisition contracts because October data "
            "does not yet exist; actual content hashes must be added after the separately authorized "
            "first read.",
            "The assessment is DRAFT_BLOCKED only because candidate freeze is intentionally "
            "deferred to separate authorization.",
            "The frozen Metric Owner policy does not authorize a current candidate, deployment, "
            "account access, or trading.",
        ],
        "artifact_hashes": {
            name: _sha256_file(output / name)
            for name in (
                "metric-owner-verification.json",
                "future-source-contract.json",
                "future-dataset-contract.json",
                "future-pit-lineage-contract.json",
                "oos-window-custody-v2.json",
                "oos-preseal-assessment-v2.json",
            )
        },
        "verified_at": _utc_now(),
    }
    _write_report(output / "governance-inputs-report.json", report)
    return report


def verify(*, output: Path = OUTPUT_ROOT) -> dict[str, object]:
    output = output.resolve()
    report_path = output / "governance-inputs-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reasons: list[str] = []
    for name, expected in report.get("artifact_hashes", {}).items():
        path = output / name
        if not path.is_file() or _sha256_file(path) != expected:
            reasons.append(f"ARTIFACT_HASH_MISMATCH:{name}")
    assessment = json.loads((output / "oos-preseal-assessment-v2.json").read_text(encoding="utf-8"))
    if assessment.get("status") != "DRAFT_BLOCKED" or assessment.get("reasons") != [
        "MISSING_CANDIDATE_FREEZE"
    ]:
        reasons.append("PRESEAL_STATE_INVALID")
    if any(
        assessment.get(field) is not False
        for field in (
            "eligible_for_v2_seal",
            "candidate_search_started",
            "oos_data_accessed",
            "governed_v2_seal_created",
        )
    ):
        reasons.append("PRESEAL_BOUNDARY_INVALID")
    owner = json.loads((output / "metric-owner-verification.json").read_text(encoding="utf-8"))
    if owner.get("status") != "PASS_WITH_SCOPE_LIMIT":
        reasons.append("METRIC_OWNER_STATUS_INVALID")
    return {
        "schema_version": "1.0",
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "report_path": str(report_path),
        "report_sha256": _sha256_file(report_path),
        "verified_at": _utc_now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    create_parser = subcommands.add_parser("create")
    create_parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()
    result = create(output=args.output) if args.command == "create" else verify(output=args.output)
    print(json.dumps(result, sort_keys=True))  # noqa: T201 - CLI result contract
    return 0 if result["status"] in {"PASS", "PASS_WITH_CONDITIONS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
