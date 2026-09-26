"""`deploy/run_shadow.sh` ends a soak by asking the record first - on the bash launchd actually runs.

The soak's plist relaunches every non-zero exit (`KeepAlive {SuccessfulExit: false}`), and `live run
--cycles 168` exits 1 whenever one of its 168 cycles failed.  A proxy 503 is enough, and most soaks have
one.  At 2026-09-23T08:00:26Z that relaunch silently began a second soak in the same record.  A clean
exit is no better at the next login: `RunAtLoad` starts the launcher again.

So the launcher asks `beidou governance canary --remaining` before every start:

* the latest round is finished -> exit 0 and start nothing, and launchd leaves the job down;
* it is unfinished (a crash, a reboot) -> run only the cycles it still owes, so one record stays one soak;
* the record is empty -> a whole soak;
* the question fails, or the answer is not a count -> exit 70 and start nothing.

Run on this machine's /bin/bash, which on the operator's Mac is the 3.2.57 launchd uses.  What runs is
the launcher's own text from `CANDIDATE=` to its end.  The lines above that source the operator's
credentials file, and a test has no business executing them (`test_tests_never_touch_the_real_app_support`).
`beidou` is a stub: it sends the canary question to this checkout's real CLI over a real record, and
writes down the arguments of the `live run` it is exec'd into instead of starting a loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.governance.test_a_soak_record_is_scored_one_round_at_a_time import _record_as_of_2026_09_26, _soak

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "deploy" / "run_shadow.sh"
BASH = Path("/bin/bash")

STUB = """#!/bin/bash
if [ "$1" = governance ]; then
  case "$STUB_ANSWER" in
    real) exec "$STUB_PYTHON" -m beidou_cli "$@" ;;
    fail) echo "Traceback (most recent call last): the record could not be read" >&2; exit 1 ;;
    *) echo "$STUB_ANSWER"; exit 0 ;;
  esac
fi
printf '%s\\n' "$@" > exec.args
"""

pytestmark = pytest.mark.skipif(not BASH.exists(), reason="no /bin/bash on this machine")


def _block() -> str:
    """The launcher from `CANDIDATE=` on, under its own shell options, run from the checkout root."""
    text = LAUNCHER.read_text(encoding="utf-8")
    options = "set -uo pipefail"
    assert f"\n{options}\n" in text, "the launcher's shell options changed; run the block under the new ones"
    start = text.index('CANDIDATE="${1:-')
    assert text.index('cd "$REPO" || exit 78') < start, "the block no longer begins inside the checkout"
    return f'{options}\nREPO="$(pwd)"\n{text[start:]}'


def _launch(tmp_path: Path, record: list[dict[str, Any]] | None, answer: str = "real") -> tuple[Path, Any]:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "alpha_registry.candidate.yaml").write_text("version: 1\n", encoding="utf-8")
    stub = repo / ".venv" / "bin" / "beidou"
    stub.parent.mkdir(parents=True)
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    if record is not None:
        directory = repo / ".beidou" / "live-shadow-dry-run"
        directory.mkdir(parents=True)
        (directory / "cycles.jsonl").write_text("".join(json.dumps(r) + "\n" for r in record), encoding="utf-8")
    (repo / "launcher.sh").write_text(_block(), encoding="utf-8")
    env = {**os.environ, "STUB_ANSWER": answer, "STUB_PYTHON": sys.executable, "PYTHONPATH": str(ROOT)}
    run = subprocess.run(
        [str(BASH), "launcher.sh"], cwd=repo, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    return repo, run


@pytest.mark.parametrize(
    ("record", "owed"),
    [
        (None, 168),  # nothing soaked yet
        (_soak(0, 100, "a", errors=(5,)), 68),  # a crash or a reboot at cycle 100
        (_record_as_of_2026_09_26(), 86),  # the second soak, if its process dies before 2026-09-30
    ],
)
def test_an_unfinished_soak_starts_the_loop_for_what_it_still_owes(
    tmp_path: Path, record: list[dict[str, Any]] | None, owed: int
) -> None:
    repo, run = _launch(tmp_path, record)

    assert run.returncode == 0, run.stderr
    arguments = (repo / "exec.args").read_text(encoding="utf-8").splitlines()
    assert arguments[:2] == ["live", "run"] and "--dry-run" in arguments
    assert arguments[arguments.index("--state-dir") + 1] == ".beidou/live-shadow"
    assert arguments[arguments.index("--registry") + 1] == "config/alpha_registry.candidate.yaml"
    assert arguments[-2:] == ["--cycles", str(owed)]
    assert f"for {owed} cycles into .beidou/live-shadow" in run.stdout


@pytest.mark.parametrize(
    "record",
    [
        _soak(0, 168, "ccd7bb9764b5", errors=(3, 129, 132)),  # 2026-09-23T08:00Z: "3 of 168 cycle(s) failed"
        [*_record_as_of_2026_09_26(), *_soak(250, 86, "b8f215ab706c")],  # 2026-09-30T08:00Z: the second done
    ],
)
def test_a_finished_soak_exits_zero_and_starts_nothing(tmp_path: Path, record: list[dict[str, Any]]) -> None:
    repo, run = _launch(tmp_path, record)

    assert run.returncode == 0, f"launchd relaunches anything else (SuccessfulExit=false): {run.stderr}"
    assert not (repo / "exec.args").exists(), "a finished soak started the loop again"
    assert "is finished; not starting another" in run.stdout


@pytest.mark.parametrize("answer", ["fail", "", "UserWarning: something", "-3"])
def test_no_count_from_the_canary_means_no_loop(tmp_path: Path, answer: str) -> None:
    repo, run = _launch(tmp_path, None, answer)

    assert run.returncode == 70, run.stdout + run.stderr
    assert not (repo / "exec.args").exists()
    assert "not starting" in run.stderr


def test_the_soak_length_is_not_written_down_twice() -> None:
    """168 lives in `SOAK_CYCLES`; a second copy here is how a launcher and its reader drift apart."""
    code = [line for line in LAUNCHER.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if "168" in line]
