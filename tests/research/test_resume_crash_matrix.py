"""Crash/restart differential matrix for every T05 durable boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from beidou_research.experiments import ExperimentRunIdentity
from beidou_research.mining.runner import (
    RESUME_DURABLE_BOUNDARIES,
    ResumableMiningRunner,
    ResumeAlreadyFinalizedError,
)

FENCING_TOKEN = "fence-1"


class Crash(RuntimeError):
    pass


def identity(run_id: str) -> ExperimentRunIdentity:
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
        {"candidate_id": "candidate-1", "evidence": {"p": 0.01, "raw": "1" * 64}},
        {
            "candidate_id": "candidate-2",
            "evidence": {"p": 0.80, "raw": "2" * 64},
            "rejection_reasons": ["MULTIPLE_TESTING"],
        },
        {"candidate_id": "candidate-3", "evidence": {"p": 0.02, "raw": "3" * 64}},
    ]


def start(root: Path, run_id: str, injector=None) -> dict:
    return ResumableMiningRunner.start(
        root / run_id,
        identity=identity(run_id),
        candidate_family=family(),
        writer_id="writer-a",
        fencing_token=FENCING_TOKEN,
        fault_injector=injector,
    )


def crash_once(target_boundary: str, target_transition: str):
    crashed = False

    def injector(boundary: str, transition: str, candidate_id: str | None) -> None:
        nonlocal crashed
        if not crashed and boundary == target_boundary and transition == target_transition:
            crashed = True
            raise Crash(candidate_id or transition)

    return injector


def result_after_crash(run_dir: Path) -> dict:
    try:
        return ResumableMiningRunner.resume(run_dir)
    except ResumeAlreadyFinalizedError:
        return ResumableMiningRunner.inspect_completed(run_dir)


@pytest.mark.parametrize("boundary", RESUME_DURABLE_BOUNDARIES)
@pytest.mark.parametrize("transition", ["candidate:0", "candidate:1", "finalize"])
def test_every_durable_boundary_resumes_to_uninterrupted_oracle(tmp_path: Path, boundary: str, transition: str) -> None:
    oracle = start(tmp_path / "oracle", "run-matrix")
    crashed_dir = tmp_path / "crashed" / "run-matrix"
    with pytest.raises(Crash):
        start(tmp_path / "crashed", "run-matrix", crash_once(boundary, transition))
    resumed = result_after_crash(crashed_dir)

    assert resumed == oracle
    assert resumed["attempt_count"] == len(family())
    assert len({item["candidate_id"] for item in resumed["attempts"]}) == len(family())
    assert resumed["multiple_testing_denominator"] == len(family())
    assert resumed["candidate_ids"] == [item["candidate_id"] for item in family()]
    assert resumed["rejection_reasons"] == {"candidate-2": ["MULTIPLE_TESTING"]}
    assert resumed["evidence_hashes"] == oracle["evidence_hashes"]

    if boundary == "after_checkpoint_write" and transition == "finalize":
        evidence_dir = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", tmp_path))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "uninterrupted-result.json").write_text(json.dumps(oracle, indent=2, sort_keys=True) + "\n")
        (evidence_dir / "resumed-result.json").write_text(json.dumps(resumed, indent=2, sort_keys=True) + "\n")
        (evidence_dir / "candidate-family-hashes.json").write_text(
            json.dumps(
                {
                    "family_hash": resumed["family_hash"],
                    "candidate_digests": [attempt["candidate_digest"] for attempt in resumed["attempts"]],
                    "evidence_hashes": resumed["evidence_hashes"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def test_repeated_crashes_do_not_duplicate_or_skip_candidate(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-repeated"
    with pytest.raises(Crash):
        ResumableMiningRunner.start(
            run_dir,
            identity=identity("run-repeated"),
            candidate_family=family(),
            writer_id="writer-a",
            fencing_token=FENCING_TOKEN,
            fault_injector=crash_once("after_event_write", "candidate:0"),
        )
    with pytest.raises(Crash):
        ResumableMiningRunner.resume(run_dir, fault_injector=crash_once("after_event_write", "candidate:1"))
    result = ResumableMiningRunner.resume(run_dir)
    assert [attempt["attempt_index"] for attempt in result["attempts"]] == [0, 1, 2]
    assert result["candidate_id_set"] == ["candidate-1", "candidate-2", "candidate-3"]


def test_rollback_characterization_is_fresh_run_only(tmp_path: Path) -> None:
    oracle = start(tmp_path, "fresh-only")
    with pytest.raises(ResumeAlreadyFinalizedError):
        ResumableMiningRunner.resume(tmp_path / "fresh-only")
    assert ResumableMiningRunner.inspect_completed(tmp_path / "fresh-only") == oracle
