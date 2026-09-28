"""周日任务跑的 `report weekly`：一周前的树从 git 读、读不到就说读不到，而且它从不发告警。

执行手册 §3.9（D-PR03，操作者 2026-09-28 裁定 Q5 = 是）给周报加了「Plan budget gap」一节（M-PR01），
并用 `deploy/com.beidou.weekly.plist` 给它排了周日 03:00 的 job——此前 `report weekly` 没有任何调用者，
2026-09-16 之后一份周报也没产出。这里钉住这个 job 依赖的三件事：

- **一周前是 main 的一周前。** 从 HEAD 自己的提交时刻往回数 7 天，沿 first-parent 取当时的树。不沿
  first-parent，一个 8 天前写、2 天前才合入的分支提交会被当成「一周前的 main」——那棵树 main 从来没有过。
  一周前还不存在的包按 0 行算，不是「读不出」。
- **读不出就印「不可读」，命令照样出报告。** 不是 checkout、或者 PATH 上没有 git，都不许让周报崩掉。
- **它从不发告警。** 无人值守的周任务照抄了 `run_check.sh` 读凭据的写法，环境里就有告警 URL；这个命令
  一次都不该碰 `WebhookAlerts.send`。launcher 里也没有 notify。
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from beidou_cli import main
from beidou_cli.live_cmd import _changed_lines, _source_lines_days_before_head
from beidou_live.alerts import WebhookAlerts
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-09-26"
SECTION = "## Plan budget gap (M-PR01, record only)"
# HEAD's own commit time.  Years before today on purpose: a week counted back from the wall clock would
# land after HEAD and read HEAD's own tree, so these dates are what catch that mistake.
HEAD_AT = "2020-09-27T00:00:00+00:00"

#: The tree main held on 2020-09-17, ten days before HEAD.  `beidou_shared` does not exist yet.
TEN_DAYS_BEFORE = {
    "beidou_alpha": 1,
    "beidou_live": 3,
    "beidou_cli": 1,
    "beidou_data": 1,
    "beidou_exchange": 1,
    "beidou_shared": 0,
    "beidou_governance": 1,
}
#: HEAD's tree: the side branch's +10 in beidou_live, beidou_shared added, one more line in beidou_cli.
AT_HEAD = {**TEN_DAYS_BEFORE, "beidou_live": 13, "beidou_shared": 2, "beidou_cli": 2}


@pytest.fixture(autouse=True)
def _no_user_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's global config (signing, hooks, templates) has no business in a scratch repository."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str, at: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at} if at else None
    subprocess.run(
        ["git", "-c", "user.name=beidou-test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, at: str, lines: dict[str, int]) -> None:
    for package, count in lines.items():
        if count:
            path = repo / package / "module.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(f"x{i} = {i}\n" for i in range(count)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", at, at=at)


def _repository(root: Path) -> Path:
    """main: 09-17, 09-24, a merge on 09-25 of a side branch written on 09-19, then HEAD on 09-27."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "2020-09-17T00:00:00+00:00", TEN_DAYS_BEFORE)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "2020-09-19T00:00:00+00:00", {"beidou_live": 13})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "2020-09-24T00:00:00+00:00", {"beidou_shared": 2})
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side", at="2020-09-25T00:00:00+00:00")
    _commit(repo, HEAD_AT, {"beidou_cli": 2})
    return repo


def test_last_weeks_tree_is_the_one_main_held_seven_days_before_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repository(tmp_path))
    # Without --first-parent this reads the side branch's 09-19 commit: beidou_live 13, a tree main never had.
    # beidou_shared did not exist on 09-17, and `git archive` refuses a missing path: it has to read as 0.
    assert _source_lines_days_before_head(7) == TEN_DAYS_BEFORE
    assert _source_lines_days_before_head(1) == {**AT_HEAD, "beidou_cli": 1}, "the merge's tree, side included"


def _week_ending(day: str) -> tuple[datetime, datetime]:
    """The weekly's window: seven days ending at the end of ``day`` (UTC), as `weekly_payload` draws it."""
    end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return end - timedelta(days=7), end


def test_the_effort_share_counts_what_merged_on_main_in_the_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator 2026-09-29: a week's effort, not the last 40 commits.  A PR counts once, in the week it merged.

    The side branch is written on 09-19 and merged on 09-25.  The week ending 09-26 sees it through the
    merge's diff against main (beidou_live 3 -> 13 lines: +10) beside 09-24's beidou_shared; HEAD's 09-27
    commit sits exactly on the window's end and is out.  The week ending 09-20 sees only 09-17's commit:
    walking every commit by date would have counted the side branch there too, a week before main had it.
    """
    monkeypatch.chdir(_repository(tmp_path))
    assert _changed_lines(*_week_ending("2020-09-26")) == {"beidou_shared/module.py": 2, "beidou_live/module.py": 10}
    first_week = _changed_lines(*_week_ending("2020-09-20"))
    assert first_week == {f"{package}/module.py": count for package, count in TEN_DAYS_BEFORE.items() if count}


def test_a_users_diff_merges_setting_does_not_change_the_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The count is a property of the history, not of the operator's git config: ``log.diffMerges=cc`` changes nothing.

    With git 2.54 ``--first-parent`` alone already yields first-parent diffs (a mutation dropping the explicit
    ``--diff-merges`` stays green), so this pins the property, not the flag.
    """
    monkeypatch.chdir(_repository(tmp_path))
    for key, value in {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "log.diffMerges",
        "GIT_CONFIG_VALUE_0": "cc",
    }.items():
        monkeypatch.setenv(key, value)
    assert _changed_lines(*_week_ending("2020-09-26")) == {"beidou_shared/module.py": 2, "beidou_live/module.py": 10}


def _profile(root: Path) -> str:
    profile = load_yaml(ROOT / "config/live.demo.yaml")
    profile["paths"] = {"state_dir": str(root / "live"), "reports_dir": str(root / "reports")}
    # A channel IS configured: the unattended job reads the same environment the hourly check does.
    profile["alerts"] = {"webhook_url": "https://example.invalid/hook", "webhook_url_2": ""}
    profile["registry"] = str(ROOT / "config/alpha_registry.yaml")
    (root / "archive" / "klines").mkdir(parents=True)
    (root / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    return str(root / "profile.yaml")


def _weekly(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    def never(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("report weekly must never page")

    monkeypatch.setattr(WebhookAlerts, "send", never)
    command = ["report", "weekly", "--profile", _profile(root), "--date", DAY, "--data-root", str(root / "archive")]
    result = CliRunner().invoke(main, command)
    assert result.exit_code == 0, result.output + repr(result.exception)
    written = root / "reports" / "weekly"
    payload = json.loads((written / f"{DAY}.json").read_text(encoding="utf-8"))
    return payload["plan_budget"], (written / f"{DAY}.md").read_text(encoding="utf-8")


def test_report_weekly_prints_the_gap_from_the_checkout_and_its_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repository(tmp_path))
    gap, markdown = _weekly(tmp_path, monkeypatch)
    assert gap["lines"] == AT_HEAD and gap["non_alpha_total"] == 20
    assert gap["non_alpha_growth_per_day"] == pytest.approx((20 - 7) / 7)
    section = markdown.split(SECTION)[1].split("\n## ")[0]
    assert "| beidou_live | 13 (plan 2,000) |" in section
    assert "| non_alpha_growth_per_day (last 7d) | +1.9 |" in section


@pytest.mark.parametrize("why", ["not a checkout", "no git on PATH"])
def test_without_git_the_growth_reads_unreadable_and_the_report_still_comes_out(
    why: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if why == "not a checkout":
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # never find a repository above it
        monkeypatch.chdir(plain)
    else:
        monkeypatch.chdir(_repository(tmp_path))
        monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))  # not "": an empty entry means the cwd
    gap, markdown = _weekly(tmp_path, monkeypatch)
    assert gap["non_alpha_growth_per_day"] is None
    assert "| non_alpha_growth_per_day (last 7d) | 不可读" in markdown.split(SECTION)[1]


def test_the_job_runs_the_weekly_report_early_on_sundays_and_is_never_relaunched() -> None:
    plist = plistlib.loads((ROOT / "deploy" / "com.beidou.weekly.plist").read_bytes())
    assert plist["StartCalendarInterval"] == {"Weekday": 0, "Hour": 3, "Minute": 0}
    assert "KeepAlive" not in plist, "under KeepAlive a job that runs to completion is relaunched on its exit"
    assert plist["ProgramArguments"][0] == "/bin/bash"
    assert plist["ProgramArguments"][-1].endswith("/deploy/run_weekly.sh")
    launcher = ROOT / "deploy" / "run_weekly.sh"
    assert os.access(launcher, os.X_OK)
    code = [line for line in launcher.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
    assert any('report weekly --date "$WEEK_ENDING"' in line for line in code)
    assert not [line for line in code if "WebhookAlerts" in line or "notify" in line], "the weekly job never pages"
