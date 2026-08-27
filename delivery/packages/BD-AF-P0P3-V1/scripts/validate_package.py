#!/usr/bin/env python3
# ruff: noqa: T201
"""Validate BD-AF-P0P3-V1 without repository imports or third-party packages."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_pairs)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError(f"cannot load strict JSON {path}: {exc}") from exc


def load_json_text(payload: str, label: str) -> Any:
    try:
        return json.loads(payload, object_pairs_hook=_no_duplicate_pairs)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError(f"cannot load strict JSON {label}: {exc}") from exc


def _type_ok(value: Any, expected: str) -> bool:
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[expected]()


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Apply the deliberately small JSON-Schema vocabulary used by this package."""
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        unknown = set(options) - {"object", "array", "string", "integer", "number", "boolean", "null"}
        if unknown:
            raise ValidationError(f"{path}: unsupported schema type(s) {sorted(unknown)}")
        if not any(_type_ok(value, item) for item in options):
            raise ValidationError(f"{path}: expected type {options}, got {type(value).__name__}")

    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: value {value!r} is not in enum")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError(f"{path}: string is shorter than minLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ValidationError(f"{path}: value {value!r} does not match {pattern!r}")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        raise ValidationError(f"{path}: value is below minimum")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{path}: array is shorter than minItems")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(canonical) != len(set(canonical)):
                raise ValidationError(f"{path}: array items are not unique")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValidationError(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValidationError(f"{path}: additional properties are forbidden: {extras}")
        for key, child_schema in properties.items():
            if key in value:
                validate_schema(value[key], child_schema, f"{path}.{key}")


def _unique(items: list[dict[str, Any]], field: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item[field]
        if key in result:
            raise ValidationError(f"duplicate {label}: {key}")
        result[key] = item
    return result


def _safe_child(root: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValidationError(f"{label} escapes package root: {relative}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"{label} escapes package root: {relative}") from exc
    return resolved


def _check_cycle(tasks: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValidationError(f"dependency cycle detected at {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["dependencies"]:
            if dependency not in tasks:
                raise ValidationError(f"unknown dependency {dependency} referenced by {task_id}")
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def _validate_oracle(criterion: dict[str, Any], task_id: str) -> None:
    oracle = criterion["oracle"]
    argv = oracle["argv"]
    if argv[0] != "python":
        raise ValidationError(f"{criterion['id']}: oracle must begin with portable 'python'")
    forbidden_tokens = {"rm", "sudo", "git-reset", "git-clean", "git-push", "kubectl", "curl", "wget"}
    normalized = {token.lower().replace(" ", "-") for token in argv}
    if normalized & forbidden_tokens:
        raise ValidationError(f"{criterion['id']}: destructive/network command token is forbidden")
    if any("*" in token or "?" in token for token in argv):
        raise ValidationError(f"{criterion['id']}: argv globs are forbidden")
    is_pytest = len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]
    expectation = oracle["test_expectation"]
    if is_pytest and expectation is None:
        raise ValidationError(f"{criterion['id']}: pytest oracle requires a JUnit test expectation")
    if not is_pytest and expectation is not None:
        raise ValidationError(f"{criterion['id']}: non-pytest oracle cannot claim JUnit counts")
    if is_pytest and not any(token.startswith("--junitxml=") for token in argv):
        raise ValidationError(f"{criterion['id']}: pytest oracle lacks --junitxml")
    if criterion["mandatory"] is not True:
        raise ValidationError(f"{criterion['id']}: every criterion in this P0 package must be mandatory")
    if not criterion["failure_action"].strip():
        raise ValidationError(f"{criterion['id']}: failure action is empty")
    if task_id not in criterion["id"]:
        raise ValidationError(f"{criterion['id']}: criterion ID is not namespaced by task")


def validate_semantics(
    root: Path,
    manifest: dict[str, Any],
    delivery: dict[str, Any],
    acceptances: dict[str, dict[str, Any]],
    *,
    check_files: bool = True,
) -> dict[str, int]:
    if manifest["package_id"] != delivery["package_id"] or root.name != manifest["package_id"]:
        raise ValidationError("package identity mismatch among directory, manifest, and delivery")
    if set(manifest["included_phases"]) != {"P0", "P1", "P2", "P3"}:
        raise ValidationError("included phases must be exactly P0-P3")
    if set(manifest["deferred_phases"]) != {"P4", "P5", "P6", "P7", "P8"}:
        raise ValidationError("deferred phases must be exactly P4-P8")
    hold_reasons = set(manifest["implementation_hold_reasons"])
    if (
        manifest["source_snapshot"]["implementation_baseline_commit"] is None
        and "IMPLEMENTATION_BASELINE_COMMIT_UNSET" not in hold_reasons
    ):
        raise ValidationError("unset implementation baseline lacks a matching hold reason")
    if manifest["approval_trust_root_sha256"] is None and "APPROVAL_TRUST_ROOT_UNSET" not in hold_reasons:
        raise ValidationError("unset approval trust root lacks a matching hold reason")

    requirements = _unique(delivery["requirements"], "id", "requirement id")
    deferred = _unique(delivery["deferred_requirements"], "id", "deferred requirement id")
    overlap = sorted(set(requirements) & set(deferred))
    if overlap:
        raise ValidationError(f"requirements cannot be both in-scope and deferred: {overlap}")
    tasks = _unique(delivery["tasks"], "id", "task id")
    _check_cycle(tasks)

    if manifest["task_count"] != len(tasks):
        raise ValidationError("manifest task_count does not match delivery")
    if manifest["in_scope_requirement_count"] != len(requirements):
        raise ValidationError("manifest in_scope_requirement_count does not match delivery")

    all_criterion_ids: set[str] = set()
    rollback_ids: set[str] = set()
    covered_requirements: set[str] = set()
    for task_id, task in tasks.items():
        if task["phase"] not in manifest["included_phases"]:
            raise ValidationError(f"{task_id}: task phase is outside included phases")
        expected_prefix = f"BD-AF-{task['phase']}-"
        if not task_id.startswith(expected_prefix):
            raise ValidationError(f"{task_id}: phase and ID disagree")
        if task["owner_role"] == task["reviewer_role"]:
            raise ValidationError(f"{task_id}: implementer and reviewer roles must differ")
        if not task["initial_status"].startswith("HOLD_"):
            raise ValidationError(f"{task_id}: initial status must fail closed on HOLD")
        unknown_requirements = sorted(set(task["requirement_ids"]) - set(requirements))
        if unknown_requirements:
            raise ValidationError(f"{task_id}: unknown or deferred requirement references {unknown_requirements}")
        covered_requirements.update(task["requirement_ids"])

        if task_id not in acceptances:
            raise ValidationError(f"{task_id}: missing acceptance contract")
        acceptance = acceptances[task_id]
        if acceptance["task_id"] != task_id:
            raise ValidationError(f"{task_id}: acceptance task_id mismatch")
        if acceptance["dependencies"] != task["dependencies"]:
            raise ValidationError(f"{task_id}: acceptance dependencies do not match delivery")
        expected_dependency_status = "ACCEPTED" if task["dependencies"] else "NOT_APPLICABLE_NO_DEPENDENCIES"
        if acceptance["preflight"]["dependency_status"] != expected_dependency_status:
            raise ValidationError(f"{task_id}: preflight dependency status does not match dependency graph")
        criterion_requirements: set[str] = set()
        for criterion in acceptance["criteria"]:
            if criterion["id"] in all_criterion_ids:
                raise ValidationError(f"duplicate acceptance criterion id: {criterion['id']}")
            all_criterion_ids.add(criterion["id"])
            unknown = sorted(set(criterion["requirement_ids"]) - set(task["requirement_ids"]))
            if unknown:
                raise ValidationError(f"{criterion['id']}: criterion references requirements outside task: {unknown}")
            criterion_requirements.update(criterion["requirement_ids"])
            _validate_oracle(criterion, task_id)
        missing_criterion_coverage = sorted(set(task["requirement_ids"]) - criterion_requirements)
        if missing_criterion_coverage:
            raise ValidationError(f"{task_id}: task requirements lack criterion coverage: {missing_criterion_coverage}")

        rollback = acceptance["rollback_rehearsal"]
        rollback_id = f"RB-{task_id}"
        rollback_ids.add(rollback_id)
        rollback_criterion = {
            "id": rollback_id,
            "mandatory": rollback["required"],
            "oracle": {
                "phase": "POST_IMPLEMENTATION",
                "argv": rollback["command"],
                "cwd": "REPO_ROOT",
                "timeout_seconds": rollback["timeout_seconds"],
                "network_policy": rollback["network_policy"],
                "expected_exit_codes": rollback["expected_exit_codes"],
                "test_expectation": rollback["test_expectation"],
            },
            "failure_action": rollback["failure_action"],
        }
        _validate_oracle(rollback_criterion, task_id)

        if check_files:
            task_dir = _safe_child(root, task["path"], f"{task_id} path")
            if not task_dir.is_dir() or task_dir.is_symlink():
                raise ValidationError(f"{task_id}: task directory missing or symlinked")
            expected_files = {"TASK.md", "ACCEPTANCE.yaml", "AGENT_PROMPT.md", "ROLLBACK.md"}
            actual_files = {path.name for path in task_dir.iterdir() if path.is_file()}
            if actual_files != expected_files:
                raise ValidationError(f"{task_id}: task directory exact file set mismatch: {sorted(actual_files)}")
            for name in ("TASK.md", "AGENT_PROMPT.md", "ROLLBACK.md"):
                text = (task_dir / name).read_text(encoding="utf-8")
                if len(text.strip()) < 200:
                    raise ValidationError(f"{task_id}/{name}: content is too short")
                if re.search(r"\b(TODO|TBD|CHANGEME)\b", text, flags=re.IGNORECASE):
                    raise ValidationError(f"{task_id}/{name}: unresolved placeholder")
            prompt = (task_dir / "AGENT_PROMPT.md").read_text(encoding="utf-8")
            for injection in (
                "AUTHORIZED_MAINNET",
                "TESTNET_WRITE_ALLOWED",
                "GIT_PUSH_ALLOWED",
                "SELF_APPROVAL_ALLOWED",
            ):
                if injection in prompt:
                    raise ValidationError(f"{task_id}: authority injection token in Agent prompt: {injection}")

    uncovered = sorted(set(requirements) - covered_requirements)
    if uncovered:
        raise ValidationError(f"uncovered in-scope requirements: {uncovered}")

    if check_files:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValidationError(f"symlink is forbidden in package: {path.relative_to(root)}")

    return {
        "tasks": len(tasks),
        "requirements": len(requirements),
        "deferred_requirements": len(deferred),
        "criteria": len(all_criterion_ids),
        "rollback_rehearsals": len(rollback_ids),
    }


def load_package(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = load_json(root / "PACKAGE-MANIFEST.json")
    delivery = load_json(root / "delivery.yaml")
    validate_schema(manifest, load_json(root / "schemas/package.schema.json"), "PACKAGE-MANIFEST")
    validate_schema(delivery, load_json(root / "schemas/delivery.schema.json"), "delivery")
    custody_path = root / "baseline/source-custody-manifest.json"
    validate_baseline_custody(root, custody_path)
    custody = load_json(custody_path)
    source_snapshot = manifest["source_snapshot"]
    if custody["repository"] != source_snapshot["repository"]:
        raise ValidationError("baseline custody repository does not match source snapshot")
    if custody["head"] != source_snapshot["commit"]:
        raise ValidationError("baseline custody HEAD does not match source snapshot")
    protected_count = custody["tracked_change_count"] + custody["untracked_change_count"]
    if protected_count != source_snapshot["protected_dirty_paths"]:
        raise ValidationError("baseline custody count does not match protected_dirty_paths")
    if custody["tracked_change_count"] != source_snapshot["protected_tracked_paths"]:
        raise ValidationError("baseline custody tracked count does not match source snapshot")
    if custody["untracked_change_count"] != source_snapshot["protected_untracked_paths"]:
        raise ValidationError("baseline custody untracked count does not match source snapshot")
    if custody["ignored_untracked_prefixes"] != source_snapshot["ignored_generated_untracked_prefixes"]:
        raise ValidationError("baseline custody ignored prefixes do not match source snapshot")
    if custody["entries_sha256"] != source_snapshot["protected_entries_sha256"]:
        raise ValidationError("baseline custody digest does not match source snapshot")
    acceptance_schema = load_json(root / "schemas/acceptance.schema.json")
    acceptances: dict[str, dict[str, Any]] = {}
    for task in delivery["tasks"]:
        path = _safe_child(root, f"{task['path']}/ACCEPTANCE.yaml", f"{task['id']} acceptance")
        acceptance = load_json(path)
        validate_schema(acceptance, acceptance_schema, f"acceptance[{task['id']}]")
        acceptances[task["id"]] = acceptance
    return manifest, delivery, acceptances


def _negative_fixture(root: Path, fixture: dict[str, Any], base: tuple[Any, Any, Any]) -> None:
    manifest, delivery, acceptances = copy.deepcopy(base)
    kind = fixture["kind"]
    if kind == "duplicate_task_id":
        delivery["tasks"].append(copy.deepcopy(delivery["tasks"][0]))
        validate_semantics(root, manifest, delivery, acceptances, check_files=False)
    elif kind == "dependency_cycle":
        delivery["tasks"][0]["dependencies"] = [delivery["tasks"][-1]["id"]]
        validate_semantics(root, manifest, delivery, acceptances, check_files=False)
    elif kind == "orphan_requirement":
        delivery["requirements"].append(
            {
                "id": "AF-REQ-999",
                "priority": "P0",
                "title": "Intentionally orphaned negative fixture",
                "status": "IN_SCOPE",
            }
        )
        manifest["in_scope_requirement_count"] += 1
        validate_semantics(root, manifest, delivery, acceptances, check_files=False)
    elif kind == "missing_oracle":
        target = acceptances[delivery["tasks"][1]["id"]]
        del target["criteria"][0]["oracle"]
        validate_schema(target, load_json(root / "schemas/acceptance.schema.json"), "negative.acceptance")
    elif kind == "missing_rollback_oracle":
        target = acceptances[delivery["tasks"][1]["id"]]
        del target["rollback_rehearsal"]["evidence"]
        validate_schema(target, load_json(root / "schemas/acceptance.schema.json"), "negative.rollback")
    elif kind == "legacy_status_injection":
        delivery["tasks"][0]["initial_status"] = "PASS"
        validate_schema(delivery, load_json(root / "schemas/delivery.schema.json"), "negative.delivery")
    elif kind == "path_escape":
        delivery["tasks"][0]["path"] = "../outside"
        validate_schema(delivery, load_json(root / "schemas/delivery.schema.json"), "negative.delivery")
    elif kind == "duplicate_json_key":
        load_json_text(fixture["payload"], fixture["fixture_id"])
    else:
        raise ValidationError(f"unknown negative fixture kind: {kind}")


def validate_negative_fixtures(root: Path, base: tuple[Any, Any, Any]) -> int:
    fixture_dir = root / "fixtures/invalid"
    fixtures = sorted(fixture_dir.glob("*.json"))
    if not fixtures:
        raise ValidationError("negative fixture directory is empty")
    rejected = 0
    for path in fixtures:
        fixture = load_json(path)
        required = {"fixture_id", "kind", "expected_error"}
        if not required.issubset(fixture):
            raise ValidationError(f"{path.name}: fixture descriptor lacks {sorted(required - set(fixture))}")
        try:
            _negative_fixture(root, fixture, base)
        except ValidationError as exc:
            if fixture["expected_error"].lower() not in str(exc).lower():
                raise ValidationError(
                    f"{path.name}: rejected for wrong reason; expected {fixture['expected_error']!r}, got {str(exc)!r}"
                ) from exc
            rejected += 1
        else:
            raise ValidationError(f"{path.name}: invalid fixture was accepted")
    return rejected


def validate_evidence(root: Path, evidence_path: Path) -> None:
    evidence = load_json(evidence_path)
    schema = load_json(root / "schemas/evidence-manifest.schema.json")
    validate_schema(evidence, schema, "evidence")


def validate_task_result(root: Path, result_path: Path) -> None:
    result = load_json(result_path)
    schema = load_json(root / "schemas/task-result.schema.json")
    validate_schema(result, schema, "task_result")
    dependency_hashes = result["dependency_result_sha256"]
    if not all(
        isinstance(key, str)
        and re.fullmatch(r"BD-AF-P[0-3]-T[0-9]{2}", key)
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
        for key, value in dependency_hashes.items()
    ):
        raise ValidationError("task_result: dependency hash map is invalid")
    evidence_ids = [item["criterion_id"] for item in result["evidence_manifests"]]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValidationError("task_result: evidence criterion IDs are not unique")
    if result["changed_paths"] != sorted(result["changed_paths"]):
        raise ValidationError("task_result: changed paths are not sorted")
    if result["reviewer_status"] == "ACCEPTED":
        required_review_fields = (
            "reviewer_identity",
            "reviewed_at",
            "reviewer_record_relative_path",
            "reviewer_record_sha256",
            "reviewer_signature_relative_path",
            "reviewer_signature_sha256",
        )
        missing = [field for field in required_review_fields if result[field] is None]
        if missing:
            raise ValidationError(f"task_result: accepted result lacks review custody fields {missing}")
        if result["implementer_status"] != "PASS":
            raise ValidationError("task_result: accepted result requires implementer PASS")
        if result["reviewer_identity"] == result["implementer_identity"]:
            raise ValidationError("task_result: reviewer must be independent from implementer")
        if re.search(r"\b(agent|codex|openai|gpt)\b", result["reviewer_identity"], re.IGNORECASE):
            raise ValidationError("task_result: Agent self-review is forbidden")
        if any(item["severity"] in {"P0", "P1"} for item in result["residual_risks"]):
            raise ValidationError("task_result: accepted result contains P0/P1 residual risk")


def validate_approval(root: Path, approval_path: Path) -> None:
    approval = load_json(approval_path)
    schema = load_json(root / "schemas/human-approval-envelope.schema.json")
    validate_schema(approval, schema, "approval")
    dependency_hashes = approval["dependency_result_sha256"]
    if not all(
        isinstance(key, str)
        and re.fullmatch(r"BD-AF-P[0-3]-T[0-9]{2}", key)
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
        for key, value in dependency_hashes.items()
    ):
        raise ValidationError("approval: dependency hash map is invalid")


def validate_reviewer_record(root: Path, review_path: Path) -> None:
    review = load_json(review_path)
    schema = load_json(root / "schemas/reviewer-record.schema.json")
    validate_schema(review, schema, "reviewer_record")
    for field in ("dependency_result_sha256", "evidence_manifest_sha256"):
        if not all(
            isinstance(key, str) and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in review[field].items()
        ):
            raise ValidationError(f"reviewer_record: {field} is invalid")
    if review["changed_paths"] != sorted(review["changed_paths"]):
        raise ValidationError("reviewer_record: changed paths are not sorted")
    if review["reviewer_identity"] == review["implementer_identity"]:
        raise ValidationError("reviewer_record: reviewer must be independent from implementer")
    if re.search(r"\b(agent|codex|openai|gpt)\b", review["reviewer_identity"], re.IGNORECASE):
        raise ValidationError("reviewer_record: Agent self-review is forbidden")
    if any(item["severity"] in {"P0", "P1"} for item in review["residual_risks"]):
        raise ValidationError("reviewer_record: accepted review contains P0/P1 residual risk")


def validate_baseline_custody(root: Path, custody_path: Path) -> None:
    custody = load_json(custody_path)
    schema = load_json(root / "schemas/baseline-custody.schema.json")
    validate_schema(custody, schema, "baseline_custody")
    entries = custody["entries"]
    paths = [entry["path"] for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValidationError("baseline custody entries must have unique sorted paths")
    if custody["tracked_change_count"] + custody["untracked_change_count"] != len(entries):
        raise ValidationError("baseline custody total change count mismatch")
    if custody["ignored_untracked_prefixes"] != sorted(custody["ignored_untracked_prefixes"]):
        raise ValidationError("baseline custody ignored prefixes must be sorted")
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if custody["entries_sha256"] != digest:
        raise ValidationError("baseline custody entries_sha256 mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--negative-fixtures", action="store_true")
    parser.add_argument("--validate-evidence", type=Path)
    parser.add_argument("--validate-task-result", type=Path)
    parser.add_argument("--validate-approval", type=Path)
    parser.add_argument("--validate-reviewer-record", type=Path)
    parser.add_argument("--validate-baseline-custody", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.validate_evidence:
            validate_evidence(root, args.validate_evidence.resolve())
            print(json.dumps({"status": "EVIDENCE_VALID", "path": str(args.validate_evidence)}, sort_keys=True))
            return 0
        if args.validate_task_result:
            validate_task_result(root, args.validate_task_result.resolve())
            print(json.dumps({"status": "TASK_RESULT_VALID", "path": str(args.validate_task_result)}, sort_keys=True))
            return 0
        if args.validate_approval:
            validate_approval(root, args.validate_approval.resolve())
            print(json.dumps({"status": "APPROVAL_VALID", "path": str(args.validate_approval)}, sort_keys=True))
            return 0
        if args.validate_reviewer_record:
            validate_reviewer_record(root, args.validate_reviewer_record.resolve())
            print(
                json.dumps(
                    {"status": "REVIEWER_RECORD_VALID", "path": str(args.validate_reviewer_record)},
                    sort_keys=True,
                )
            )
            return 0
        if args.validate_baseline_custody:
            validate_baseline_custody(root, args.validate_baseline_custody.resolve())
            print(
                json.dumps(
                    {"status": "BASELINE_CUSTODY_VALID", "path": str(args.validate_baseline_custody)},
                    sort_keys=True,
                )
            )
            return 0
        base = load_package(root)
        counts = validate_semantics(root, *base)
        rejected = validate_negative_fixtures(root, base) if args.negative_fixtures else 0
    except ValidationError as exc:
        print(json.dumps({"status": "PACKAGE_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": "PACKAGE_VALID", **counts, "negative_fixtures_rejected": rejected}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
