"""每晚的 governance-gate 任务顺带读一次重开条件，只印不告警（2026-09-29，操作者「2 做」）。

G11 的重开条件（`net-exposure-cap-g11`）是机读的，随实盘记录翻转，却只在有人手敲 `beidou governance reopen`
时才被读：满足那天没人会知道。`run_governance_gate.sh` 现在在 gate 之前跑一次 reopen，把 MET 的条目印进日志。
这里钉住四件事：MET 的条目进日志；没有 MET 时只印汇总；reopen 自己出错只记日志，退出码仍由 gate 决定；
gate FAIL 时 reopen 的读数照样留在日志里，告警只有 gate 那一条。

跑法与 `test_the_governance_gate_never_pages_a_blank_line.py` 相同：截取 `cd "$REPO"` 之后到结尾，
`beidou` 是桩，按子命令印不同的输出。环境里没有 webhook 变量，notify 只打印、不发送。两个 locale 都跑：
C 是 launchd 给的，C.UTF-8 是终端里手动跑的样子。
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beidou_governance.reopen import LIST, load, render, survey

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "deploy" / "run_governance_gate.sh"
BASH = Path("/bin/bash")

STUB = """#!/bin/bash
if [ "$2" = "reopen" ]; then
  printf '%s' "$REOPEN_OUTPUT"
  exit "$REOPEN_RC"
fi
printf '%s' "$GATE_OUTPUT"
exit "$GATE_RC"
"""


def _reopen_output(legs: list[float]) -> str:
    """What `beidou governance reopen` prints: its own renderer over the shipped list, not a hand-typed copy.

    The first version of this file typed the output by hand and ended it on the counts line.  The real one
    ends on "N 条机器答不了", so the job's `tail -n 1` summary passed here and printed the wrong line on the
    first real run (2026-09-29).  Rendering it keeps the fixture on the contract that exists.
    """
    shipped = load(ROOT / LIST)
    entries = [entry for entry in shipped if entry.check != "resolved"]
    facts = {"equity": 13_258.0, "columns": {"oi", "lsr", "basis"}, "now": datetime(2026, 9, 29, tzinfo=UTC)}
    return render(survey(entries, {**facts, "short_legs": legs}), len(shipped) - len(entries)) + "\n"


REOPEN_MET = _reopen_output([0.5] * 720)
REOPEN_QUIET = _reopen_output([0.02] * 10)
MET_LINE = next(line for line in REOPEN_MET.splitlines() if line.startswith("MET "))
GATE_PASS = "PASS       tsmom                OOS 1.8257 vs 1.5726 at N=343\n"
GATE_FAIL = "FAIL       tsmom                OOS 1.2306 vs 1.5251 at N=185\n"
LOCALES = pytest.mark.parametrize("locale", ["C", "C.UTF-8"])

pytestmark = pytest.mark.skipif(not BASH.exists(), reason="no /bin/bash on this machine")


def _block() -> str:
    """launcher 从 `cd "$REPO"` 之后到结尾，套上它自己的 shell 选项，在 checkout 根目录里跑。"""
    text = GATE.read_text(encoding="utf-8")
    options = "set -uo pipefail"
    assert f"\n{options}\n" in text, "launcher 的 shell 选项变了，按新的选项跑这一段"
    cd = 'cd "$REPO" || exit 78\n'
    assert cd in text, "launcher 不再这样进入 checkout，这个测试截取的起点要跟着挪"
    return f'{options}\nREPO="$(pwd)"\nSUPPORT="$REPO/support"\n{text[text.index(cd) + len(cd) :]}'


def _run(
    tmp_path: Path, locale: str, *, reopen: str, reopen_rc: int = 0, gate: str = GATE_PASS, gate_rc: int = 0
) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    beidou = repo / ".venv" / "bin" / "beidou"
    beidou.write_text(STUB, encoding="utf-8")
    beidou.chmod(0o755)
    script = tmp_path / "gate.sh"
    script.write_text(_block(), encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": locale,
        "REOPEN_OUTPUT": reopen,
        "REOPEN_RC": str(reopen_rc),
        "GATE_OUTPUT": gate,
        "GATE_RC": str(gate_rc),
    }
    run = subprocess.run([str(BASH), str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert run.stderr == "", run.stderr
    return run


def _summary(run: subprocess.CompletedProcess[str]) -> str:
    """The one line the job prints for reopen's summary."""
    lines = [line for line in run.stdout.splitlines() if "] reopen " in line]
    assert len(lines) == 1, run.stdout
    return lines[0]


@LOCALES
def test_a_met_condition_is_printed_into_the_log_and_pages_nobody(tmp_path: Path, locale: str) -> None:
    run = _run(tmp_path, locale, reopen=REOPEN_MET)
    assert run.returncode == 0, run.stdout
    assert "条：MET 1, NOT MET" in _summary(run), "the counts line, not the line printed after it"
    assert "有重开条件已满足" in run.stdout and f"           {MET_LINE}" in run.stdout
    assert "vwap-42" not in run.stdout, "only the MET lines are copied, not the whole list"
    assert "net-exposure-cap-g11" in MET_LINE, "the fixture has to make G11 the MET entry"
    assert "FAIL governance-gate" not in run.stdout


@LOCALES
def test_nothing_met_prints_the_summary_alone(tmp_path: Path, locale: str) -> None:
    run = _run(tmp_path, locale, reopen=REOPEN_QUIET)
    assert run.returncode == 0, run.stdout
    assert "条：MET 0, NOT MET" in _summary(run)
    assert "有重开条件已满足" not in run.stdout


@LOCALES
def test_a_reopen_that_breaks_is_logged_and_does_not_decide_the_exit_code(tmp_path: Path, locale: str) -> None:
    run = _run(tmp_path, locale, reopen="Traceback (most recent call last):\nKeyError: 'short_legs'\n", reopen_rc=1)
    assert run.returncode == 0, "the exit code answers the family gate, which passed"
    assert "reopen 没跑完（退出码 1）" in run.stdout and "KeyError: 'short_legs'" in run.stdout
    assert "FAIL governance-gate" not in run.stdout, "a broken reopen is logged, not paged"


@LOCALES
def test_a_failing_gate_still_leaves_the_reopen_reading_in_the_log(tmp_path: Path, locale: str) -> None:
    run = _run(tmp_path, locale, reopen=REOPEN_MET, gate=GATE_FAIL, gate_rc=1)
    assert run.returncode == 1, run.stdout
    assert f"           {MET_LINE}" in run.stdout
    pages = [line for line in run.stdout.splitlines() if "FAIL governance-gate: " in line]
    assert len(pages) == 1 and "tsmom" in pages[0], run.stdout
