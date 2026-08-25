"""Acceptance proofs for the real Factor Miner resume command (T05)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from apps.factor_miner.__main__ import cli
from apps.factor_miner.worker import resolve_run_directory, resume_run
from beidou_research.experiments import ExperimentRunIdentity, canonical_json
from beidou_research.mining.runner import (
    ResumableMiningRunner,
    ResumeAlreadyFinalizedError,
    ResumeRunError,
)

FENCING_TOKEN = "fence-1"


class Crash(RuntimeError):
    pass


def identity(run_id: str = "run-resume") -> ExperimentRunIdentity:
    return ExperimentRunIdentity(
        run_id=run_id,
        code_commit="a" * 40,
        code_tree_digest="b" * 64,
        policy_digest="c" * 64,
        dataset_manifest_digest="d" * 64,
        pit_manifest_digest="e" * 64,
        universe="BTCUSDT",
        timeframe="1h",
        feature_digest="f" * 64,
        label_digest="0" * 64,
        cost_model="maker-taker-v1",
        seed=7,
    )


def family() -> list[dict]:
    return [
        {"candidate_id": "candidate-1", "evidence": {"score": 0.4, "raw_hash": "1" * 64}},
        {
            "candidate_id": "candidate-2",
            "evidence": {"score": -0.1, "raw_hash": "2" * 64},
            "rejection_reasons": ["FAST_SCREEN"],
        },
        {"candidate_id": "candidate-3", "evidence": {"score": 0.2, "raw_hash": "3" * 64}},
    ]


def create_interrupted(root: Path, run_id: str = "run-resume") -> Path:
    run_dir = root / run_id

    def crash_before_first(boundary: str, transition: str, candidate_id: str | None) -> None:
        if boundary == "before_event_write" and transition == "candidate:0":
            raise Crash(candidate_id or transition)

    with pytest.raises(Crash):
        ResumableMiningRunner.start(
            run_dir,
            identity=identity(run_id),
            candidate_family=family(),
            writer_id="writer-a",
            fencing_token=FENCING_TOKEN,
            fault_injector=crash_before_first,
        )
    return run_dir


def rewrite_manifest(path: Path, mutate) -> None:
    manifest = json.loads(path.read_text())
    mutate(manifest)
    core = dict(manifest)
    core.pop("manifest_digest")
    manifest["manifest_digest"] = __import__("hashlib").sha256(canonical_json(core).encode()).hexdigest()
    path.write_text(canonical_json(manifest) + "\n")


def test_real_cli_resumes_unique_compatible_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = create_interrupted(tmp_path)
    result = CliRunner().invoke(cli, ["resume", "--run-id", "run-resume", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "COMPLETED"
    assert payload["candidate_ids"] == ["candidate-1", "candidate-2", "candidate-3"]
    assert payload["multiple_testing_denominator"] == 3

    evidence_dir = Path(__import__("os").environ.get("BEIDOU_EVIDENCE_DIR", tmp_path))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "resume-resolution-manifest.json").write_text(
        json.dumps(
            {
                "resolved_run": str(run_dir),
                "run_id": payload["run_id"],
                "identity_digest": payload["identity_digest"],
                "family_hash": payload["family_hash"],
                "status": payload["status"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_missing_run_fails_without_creating_state(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    with pytest.raises(ResumeRunError, match="RUN_ID_NOT_FOUND"):
        resume_run("missing", state_root=tmp_path)
    assert list(tmp_path.iterdir()) == before


def test_missing_checkpoint_fails_closed(tmp_path: Path) -> None:
    run_dir = create_interrupted(tmp_path)
    (run_dir / "checkpoints" / "checkpoint.current").unlink()
    with pytest.raises(ResumeRunError, match="MISSING_RESUME_STATE"):
        resume_run("run-resume", state_root=tmp_path)


def test_corrupt_checkpoint_fails_closed(tmp_path: Path) -> None:
    run_dir = create_interrupted(tmp_path)
    pointer = json.loads((run_dir / "checkpoints" / "checkpoint.current").read_text())
    checkpoint = run_dir / "checkpoints" / pointer["filename"]
    checkpoint.write_text("{corrupt")
    with pytest.raises(ResumeRunError, match="CHECKPOINT_NOT_VERIFIABLE"):
        resume_run("run-resume", state_root=tmp_path)


def test_ambiguous_run_id_is_rejected(tmp_path: Path) -> None:
    source = create_interrupted(tmp_path / "one")
    duplicate = tmp_path / "two" / "run-copy"
    duplicate.parent.mkdir(parents=True)
    shutil.copytree(source, duplicate)
    with pytest.raises(ResumeRunError, match="AMBIGUOUS_RUN_ID"):
        resolve_run_directory(tmp_path, "run-resume")


def test_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    run_dir = create_interrupted(tmp_path)
    manifest_path = run_dir / "run-manifest.json"

    def mutate(manifest: dict) -> None:
        manifest["identity"]["policy_digest"] = "9" * 64
        changed = ExperimentRunIdentity.from_dict(manifest["identity"])
        manifest["identity_digest"] = changed.digest

    rewrite_manifest(manifest_path, mutate)
    with pytest.raises(ResumeRunError, match="RESUME_NOT_VERIFIABLE:LedgerIntegrityError:IDENTITY_MISMATCH"):
        resume_run("run-resume", state_root=tmp_path)


def test_already_completed_run_is_refused(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-complete"
    ResumableMiningRunner.start(
        run_dir,
        identity=identity("run-complete"),
        candidate_family=family(),
        writer_id="writer-a",
        fencing_token=FENCING_TOKEN,
    )
    with pytest.raises(ResumeAlreadyFinalizedError, match="RUN_ALREADY_FINALIZED"):
        resume_run("run-complete", state_root=tmp_path)


def test_reordered_frozen_family_is_rejected(tmp_path: Path) -> None:
    run_dir = create_interrupted(tmp_path)
    manifest_path = run_dir / "run-manifest.json"

    def mutate(manifest: dict) -> None:
        manifest["candidate_family"] = list(reversed(manifest["candidate_family"]))

    rewrite_manifest(manifest_path, mutate)
    with pytest.raises(ResumeRunError, match="candidate family mismatch"):
        resume_run("run-resume", state_root=tmp_path)


def test_duplicate_candidate_id_never_creates_run(tmp_path: Path) -> None:
    duplicate = [family()[0], family()[0]]
    with pytest.raises(ValueError, match="DUPLICATE_CANDIDATE_ID"):
        ResumableMiningRunner.start(
            tmp_path / "duplicate",
            identity=identity("duplicate"),
            candidate_family=duplicate,
            writer_id="writer-a",
            fencing_token=FENCING_TOKEN,
        )
    assert not (tmp_path / "duplicate").exists()


def test_rollback_disables_resume_without_fresh_fallthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = create_interrupted(tmp_path)
    ledger_before = (run_dir / "experiment-run.sqlite3").read_bytes()
    children_before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    monkeypatch.setenv("BEIDOU_FACTOR_MINER_RESUME_ENABLED", "0")
    with pytest.raises(ResumeRunError, match="RESUME_DISABLED"):
        resume_run("run-resume", state_root=tmp_path)
    assert (run_dir / "experiment-run.sqlite3").read_bytes() == ledger_before
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == children_before


def test_rollback_cli_is_explicit_failure_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_interrupted(tmp_path)
    monkeypatch.setenv("BEIDOU_FACTOR_MINER_RESUME_ENABLED", "false")
    result = CliRunner().invoke(cli, ["resume", "--run-id", "run-resume", "--state-root", str(tmp_path)])
    assert result.exit_code != 0
    assert "RESUME_REJECTED:RESUME_DISABLED" in result.output
    assert "COMPLETED" not in result.output
