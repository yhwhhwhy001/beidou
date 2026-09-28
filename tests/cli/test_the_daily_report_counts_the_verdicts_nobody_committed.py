"""日报数出夜间 governance gate 追进 checkout、却没有提交的裁决行（2026-09-29）。

`deploy/run_governance_gate.sh` 往被跟踪的 `governance/verdicts.jsonl` 追行，追在它运行的那个 checkout
的工作树里，入库要人手做。2026-09-25 与 09-27 两行就这样在主 checkout 里放了三天（#239 才入库），没有任何
报告提过。这里钉住读数的四种状态：纯追加的行数与最新一行；没有未入库的行；本地改动不是纯追加；不在 git
checkout 里。都只报告，不告警。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from beidou_cli.live_cmd import _uncommitted_verdicts
from beidou_live.report_governance import _uncommitted_verdict_lines

LEDGER = "governance/verdicts.jsonl"
FIRST = {"at": "2026-09-19T18:30:06+00:00", "kind": "family_gate", "subject": "tsmom", "ruling": "refuse"}
APPENDED = [
    {"at": "2026-09-25T18:30:05+00:00", "kind": "family_gate", "subject": "tsmom", "ruling": "refuse"},
    {"at": "2026-09-27T18:30:06+00:00", "kind": "family_gate", "subject": "tsmom", "ruling": "allow"},
]


@pytest.fixture(autouse=True)
def _no_user_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's global config (signing, hooks, templates) has no business in a scratch repository."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _line(row: dict[str, str]) -> str:
    return json.dumps(row, sort_keys=True) + "\n"


def _repository(root: Path) -> Path:
    repo = root / "repo"
    (repo / "governance").mkdir(parents=True)
    (repo / LEDGER).write_text(_line(FIRST), encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "ledger"]):
        subprocess.run(
            ["git", "-c", "user.name=beidou-test", "-c", "user.email=test@example.invalid", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    return repo


def test_rows_the_gate_appended_are_counted_with_the_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    assert _uncommitted_verdicts() == {"rows": 0, "append_only": True, "latest": None}
    with (repo / LEDGER).open("a", encoding="utf-8") as handle:
        handle.writelines(_line(row) for row in APPENDED)
    reading = _uncommitted_verdicts()
    assert reading == {"rows": 2, "append_only": True, "latest": APPENDED[-1]}
    lines = _uncommitted_verdict_lines(reading)
    assert isinstance(lines, dict) and lines["未入库"].startswith("2 行")
    assert lines["最新"] == "2026-09-27T18:30:06Z family_gate tsmom allow"


def test_a_changed_history_row_is_not_read_as_an_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    (repo / LEDGER).write_text(_line({**FIRST, "ruling": "allow"}) + _line(APPENDED[0]), encoding="utf-8")
    reading = _uncommitted_verdicts()
    assert reading == {"rows": None, "append_only": False, "latest": None}
    assert "不是纯追加" in str(_uncommitted_verdict_lines(reading))


def test_outside_a_checkout_the_section_says_it_did_not_look(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _uncommitted_verdicts() is None
    assert _uncommitted_verdict_lines(None) == "未检查（不在 git checkout 里）"
    assert _uncommitted_verdict_lines({"rows": 0, "append_only": True, "latest": None}) == "无"
