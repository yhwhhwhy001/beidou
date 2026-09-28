"""归档专属测试有了执行位置（WP-P4），而默认门一条没少。

七个测试函数读 `.beidou/` 本身：从归档切出来的夹具、实盘循环自己的记录、数据集门。归档不在就跳过——CI 与
每个 worktree 都没有归档，所以真正跑它们的只有主 checkout，而四道门从不在那里跑。BNX 的夹具 2026-09-25
起在那里是红的，没人看见。现在 `deploy/run_data.sh` 每晚跑 `pytest -m archive`，FAIL 时推送；日报印最后
一次结果。

这里钉住五件会静默坏掉的事：

一、清单上每条同时带 marker 与原来的跳过条件。少了 marker，夜间 job 选不到它；少了跳过条件，CI 就红了。
二、反过来，凡是「归档不在就跳过」的测试都带 marker。新加一条忘了它，那条就回到没人跑的状态。
三、两道默认门（`pyproject.toml` 的 addopts、CI 的 pytest 调用）都不排除 `archive`：marker 只是叠加。
    「加 marker 前后 `--collect-only -m "not network"` 用例数相同」只在 PR 里量一次，不写成常量：写成常量，
    任何人加一条测试它就红，验的不是它声称验的东西。
四、job 里那一段真能跑：从脚本原样截出来，在 launchd 用的 /bin/bash 上以桩 pytest 走通过、失败、全跳过
    三支。本机是 macOS 的 3.2，CI 是 5.x；D-041 bridge 那次就是只在 3.2 上死。
五、job 写的那一行就是日报读的那一行：第四条跑出来的日志直接喂给日报的读数，不喂手编的格式。
"""

from __future__ import annotations

import ast
import plistlib
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from beidou_live.report_data import DATA_JOB_LOG, archive_tests_status
from beidou_live.reports import daily_alerts, daily_markdown, daily_payload
from beidou_live.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "run_data.sh"
PLIST = ROOT / "deploy" / "com.beidou.data.plist"
BASH = Path("/bin/bash")  # 写死路径而不走 PATH：plist 跑的就是它，macOS 上是 3.2

#: 2026-09-28 复核的清单（文件、函数）。执行手册 §3.5 列的也是这七条，其中两条的目录写反了：
#: `test_dataset_gate` 在 `tests/live/`，`test_a_gap_...` 在 `tests/data/`。
ARCHIVE_TESTS = {
    ("tests/data/test_a_gap_is_asked_for_once_and_never_filled_in.py", "test_the_fixtures_are_the_archive_verbatim"),
    (
        "tests/governance/test_the_record_reaches_the_state_exactly_once.py",
        "test_it_runs_on_the_armed_loops_own_record_without_writing",
    ),
    (
        "tests/live/test_a_restart_does_not_take_over_a_bar_a_failed_cycle_lost.py",
        "test_the_fixture_is_the_record_verbatim",
    ),
    ("tests/live/test_bar_sanity_is_seen_and_never_traded_on.py", "test_the_fixtures_are_the_archive_verbatim"),
    ("tests/live/test_dataset_gate.py", "test_the_shipped_registry_is_not_blocked_on_the_machine_that_runs_the_loop"),
    (
        "tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py",
        "test_the_frozen_digest_is_the_one_the_live_loop_recorded",
    ),
    (
        "tests/live/test_the_market_benchmark_cannot_be_built_with_hindsight.py",
        "test_the_field_names_match_what_the_running_loop_actually_writes",
    ),
}

Function = ast.FunctionDef | ast.AsyncFunctionDef


# --- 一、二：marker 与跳过条件 --------------------------------------------------------------------------


def _mentions_archive(node: ast.AST, names: set[str]) -> bool:
    """一段 `.beidou` 路径，或一个已经绑定到这种路径上的名字。"""
    return any(
        (isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.split("/")[0] == ".beidou")
        or (isinstance(sub, ast.Name) and sub.id in names)
        for sub in ast.walk(node)
    )


def _bound_to_archive(statements: list[ast.stmt], names: set[str]) -> set[str]:
    """这些语句里从一段 `.beidou` 路径赋值得到的名字：模块级的 `RECORD = ...`、函数里的 `path = ...`。"""
    return names | {
        target.id
        for statement in statements
        if isinstance(statement, ast.Assign) and _mentions_archive(statement.value, names)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }


def _skips_without_the_archive(function: Function, module_names: set[str]) -> bool:
    """归档不在就 `skipif`，或者在函数里 `pytest.skip` / `return`——七条里用到了这三种写法。"""
    names = _bound_to_archive(function.body, module_names)
    skipif = any(
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func).endswith("skipif")
        and _mentions_archive(decorator, names)
        for decorator in function.decorator_list
    )
    return skipif or any(
        isinstance(node, ast.If)
        and _mentions_archive(node.test, names)
        and any(isinstance(statement, ast.Return) or "skip" in ast.unparse(statement) for statement in node.body)
        for node in ast.walk(function)
    )


def _marked(function: Function) -> bool:
    return any(ast.unparse(decorator) == "pytest.mark.archive" for decorator in function.decorator_list)


def _test_functions() -> dict[tuple[str, str], tuple[Function, set[str]]]:
    """`tests/` 下每个模块级测试函数（本文件除外），连同它所在模块里绑定到归档路径的名字。"""
    found: dict[tuple[str, str], tuple[Function, set[str]]] = {}
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        if path == Path(__file__).resolve():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_names = _bound_to_archive(tree.body, set())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                found[(path.relative_to(ROOT).as_posix(), node.name)] = (node, module_names)
    return found


def test_each_listed_test_carries_the_marker_and_keeps_its_skip() -> None:
    """少了 marker，夜间 job 选不到它；少了跳过条件，CI 与每个 worktree 就红了。"""
    functions = _test_functions()
    for key in sorted(ARCHIVE_TESTS):
        assert key in functions, f"{key} 改名或搬走了：清单和夜间 job 会一起丢掉它"
        function, module_names = functions[key]
        assert _marked(function), f"{key} 没有 `@pytest.mark.archive`：夜间 job 不再跑它"
        assert _skips_without_the_archive(function, module_names), f"{key} 归档不在时不再跳过：CI 会红"


def test_every_test_that_skips_without_the_archive_is_marked() -> None:
    """另一个方向：新写一条读归档的测试而忘了 marker，它就回到没人跑的状态。

    按它守的条件找，不按名字找。已知的局限：条件藏在辅助函数后面（`if not _archive_present(): ...`）
    这里看不见，这里也不假装看得见。
    """
    functions = _test_functions()
    guarded = {key for key, (function, names) in functions.items() if _skips_without_the_archive(function, names)}
    marked = {key for key, (function, _) in functions.items() if _marked(function)}

    assert guarded >= ARCHIVE_TESTS, "扫描连已知的七条都找不全，坏的是扫描本身"
    assert guarded == marked, (
        f"跳过却没 marker：{sorted(guarded - marked)}；有 marker 却不跳过：{sorted(marked - guarded)}"
    )


# --- 三：两道默认门 -----------------------------------------------------------------------------------


def test_neither_default_gate_leaves_the_marked_tests_out() -> None:
    """marker 只是叠加：`-m "not network"` 里这些测试照旧在 CI 跳过、在主 checkout 上跑。"""
    options = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert any(marker.startswith("archive:") for marker in options["markers"]), "--strict-markers 要它先注册"
    assert "-m" not in options["addopts"] and not any("archive" in option for option in options["addopts"])

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    calls = [
        line
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        for line in str(step.get("run", "")).splitlines()
        if "pytest" in line and "--version" not in line
    ]
    assert calls, "CI 里找不到 pytest 的调用：这条检查自己没读到东西"
    assert not any("archive" in line for line in calls), calls


# --- 四、五：job 那一段真能跑，而且写的就是日报读的 ---------------------------------------------------


def test_the_job_runs_them_after_the_feeds_and_launchd_does_not_relaunch_it_on_a_red() -> None:
    """放在 `data status` 之后、`exit "$fail"` 之前；红的一夜让 job 以 1 退出，所以 plist 不能有 KeepAlive。

    `com.beidou.shadow` 2026-09-23 就是这样：会结束的任务以 1 退出，`KeepAlive` 当崩溃重拉。这里重拉的会是
    整个 data job——每一轮都从 `data sync` 重来。日志路径也从 plist 读，日报读的就是这个文件名。
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    status = next(i for i, line in enumerate(lines) if line.startswith('"$BEIDOU" data status'))
    run = next(i for i, line in enumerate(lines) if "-m pytest -m archive" in line)
    assert status < run < lines.index('exit "$fail"')

    plist = plistlib.loads(PLIST.read_bytes())
    assert "KeepAlive" not in plist, plist
    log = Path(plist["StandardOutPath"])
    assert log.name == DATA_JOB_LOG and log.parent.as_posix().endswith("Library/Application Support/beidou")


#: 2026-09-28 两次真实运行 `pytest -m archive -rfEs` 的末几行：对着归档（BNX 自 09-25 起红），与在 worktree
#: 里（全部跳过）。通过那一支是 BNX 定下来之后一夜的样子，行形照真实输出。
NIGHTS = {
    "pass": (0, "11 passed, 2820 deselected in 3.21s"),
    "fail": (
        1,
        "FAILED tests/live/test_bar_sanity_is_seen_and_never_traded_on.py::test_the_fixtures_are_the_archive_verbatim"
        "[BNXUSDT_2023-02-22.json]\n1 failed, 10 passed, 2820 deselected in 3.21s",
    ),
    "skipped": (
        0,
        "SKIPPED [2] tests/live/test_bar_sanity_is_seen_and_never_traded_on.py:112: the archive is not in this checkout "
        "(CI and worktrees)\n1 passed, 10 skipped, 2820 deselected in 2.77s",
    ),
}


def _night(tmp_path: Path, rc: int, output: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """job 自己的那几行，从脚本原样截出，用 launchd 的 bash 跑在一个桩 python 上。

    桩对 `-m pytest` 回一段固定输出和退出码；对 `notify` 交给 python 的参数只记下来、不发：什么都不出本机。
    返回 bash 的结果，与 `notify` 交出去的（正文、dedup key、状态文件）。
    """
    script = SCRIPT.read_text(encoding="utf-8")
    stamp = next(line for line in script.splitlines() if line.startswith("stamp() {"))
    end = '\nexit "$fail"\n'
    block = script[script.index("\nnotify() {\n") + 1 : script.index(end) + len(end)]
    python = tmp_path / "repo" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    (tmp_path / "pytest-output").write_text(output + "\n", encoding="utf-8")
    python.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-m" ]; then echo "$*" > "{tmp_path}/pytest-argv"; cat "{tmp_path}/pytest-output"; exit {rc}; fi\n'
        f'printf "%s\\n" "$3" "$4" "$5" > "{tmp_path}/page"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    program = "\n".join(
        ["set -uo pipefail", f'REPO="{tmp_path}/repo"', f'SUPPORT="{tmp_path}/support"', stamp, "fail=0", block]
    )
    env = {"PATH": "/usr/bin:/bin", "BEIDOU_ALERTS_WEBHOOK_URL": "http://127.0.0.1:9/never-contacted"}
    result = subprocess.run(
        [str(BASH), "-c", program], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    page = tmp_path / "page"
    return result, page.read_text(encoding="utf-8").splitlines() if page.exists() else []


@pytest.mark.skipif(not BASH.exists(), reason="no /bin/bash on this machine")
@pytest.mark.parametrize(
    ("night", "exit_code", "status"), [("pass", 0, "通过"), ("fail", 1, "失败"), ("skipped", 1, "失败")]
)
def test_a_night_writes_the_line_the_report_reads(tmp_path: Path, night: str, exit_code: int, status: str) -> None:
    """全部跳过也是失败：七条跳过的原因都是同一个——归档不在它找的地方，而没跑的检查不能报平安。"""
    rc, output = NIGHTS[night]

    result, page = _night(tmp_path, rc, output)

    assert result.returncode == exit_code, result.stdout + result.stderr
    argv = (tmp_path / "pytest-argv").read_text(encoding="utf-8").split()
    assert argv == ["-m", "pytest", "-m", "archive", "-p", "no:cacheprovider", "-rfEs"]
    (tmp_path / DATA_JOB_LOG).write_text(result.stdout, encoding="utf-8")
    reading = archive_tests_status(tmp_path / DATA_JOB_LOG)
    assert reading["status"] == status and reading["summary"].endswith(output.splitlines()[-1]), reading
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", reading["at"]), reading
    if night == "pass":
        assert page == [], "绿的一夜不推送"
    else:
        message, key, state = page
        assert message.startswith("北斗归档专属测试失败：") and output.splitlines()[0] in message, message
        assert (key, state) == ("archive-tests", f"{tmp_path}/support/alert-dedup.json")


# --- 日报的三种状态（合成日志，行形照上面那条测试里 job 真实写出的） ---------------------------------


def _verdict(at: str, verdict: str, summary: str) -> str:
    return f"[{at}] archive tests\n[{at}] {'ok  ' if verdict == 'ok' else 'FAIL'} archive tests: {summary}\n"


def test_before_the_job_has_written_a_verdict_the_report_says_not_run(tmp_path: Path) -> None:
    log = tmp_path / DATA_JOB_LOG
    assert archive_tests_status(log)["status"] == "未跑", "文件不在：job 没装载或还没跑过"

    log.write_text("[2026-09-27T17:20:05Z] data sync\n[2026-09-27T17:22:27Z] status\n", encoding="utf-8")
    reading = archive_tests_status(log)

    assert reading["status"] == "未跑" and DATA_JOB_LOG in reading["why"], "快进之前的那些夜里只有 feed 的行"


def test_a_green_night_reads_passed_with_its_own_time(tmp_path: Path) -> None:
    log = tmp_path / DATA_JOB_LOG
    log.write_text(
        _verdict("2026-09-29T17:22:41Z", "FAIL", "1 failed, 10 passed")
        + _verdict("2026-09-30T17:22:40Z", "ok", "11 passed, 2820 deselected in 3.1s"),
        encoding="utf-8",
    )

    assert archive_tests_status(log) == {
        "status": "通过",
        "at": "2026-09-30T17:22:40Z",
        "summary": "11 passed, 2820 deselected in 3.1s",
    }


def test_a_red_night_reads_failed_even_after_a_green_one(tmp_path: Path) -> None:
    log = tmp_path / DATA_JOB_LOG
    log.write_text(
        _verdict("2026-09-29T17:22:40Z", "ok", "11 passed")
        + _verdict("2026-09-30T17:22:41Z", "FAIL", "FAILED tests/x.py::test_y 1 failed, 10 passed")
        + "           1 failed, 10 passed, 2820 deselected in 3.21s\n",
        encoding="utf-8",
    )

    reading = archive_tests_status(log)

    assert (reading["status"], reading["at"]) == ("失败", "2026-09-30T17:22:41Z"), "最后一夜说了算，缩进的尾部不算"


def test_the_report_looks_where_the_suite_redirects_app_support(isolated_app_support: Path) -> None:
    """不带参数时在调用那一刻读 `lock.APP_SUPPORT`。按值 import 的话，测试会读到操作者真实的目录。"""
    (isolated_app_support / DATA_JOB_LOG).write_text(
        _verdict("2026-09-30T17:22:40Z", "ok", "11 passed"), encoding="utf-8"
    )

    assert archive_tests_status()["status"] == "通过"


def test_the_daily_report_prints_it_and_never_pages_on_it(tmp_path: Path) -> None:
    """只印不告警：推送是 job 的，一夜一次；日报每小时跑，从这里再推就是每小时一次。"""
    payload = daily_payload(StateStore(tmp_path / "live"), "2026-09-28", data_root=tmp_path / "data")
    markdown = daily_markdown(payload)
    assert "## 归档专属测试（夜间 data job，只报告）" in markdown and "| status | 未跑 |" in markdown

    red = {**payload, "archive_tests": {"status": "失败", "at": "2026-09-28T17:22:41Z", "summary": "1 failed"}}

    assert daily_alerts(red) == daily_alerts(payload)
