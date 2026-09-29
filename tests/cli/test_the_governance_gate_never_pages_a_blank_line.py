"""`run_governance_gate.sh` 失败时，告警正文要说出失败的是什么，命令一个字没印也一样（2026-09-29）。

脚本的注释写着「不能让告警内容为空」：先取 FAIL 行；取不到就取输出尾部；再取不到，写一句
「`governance gate --check` 退出码 N，且没有任何输出」。最后这一句从 2026-09-19 写下起一次也没执行过。
没有输出时 `echo "$output"` 仍印一个换行，`tr '\\n' ' '` 把它换成空格，`[ -n ]` 判它非空。
于是告警正文只剩一个空格，正是那段注释要防的「告诉操作者有事，却不说是什么事」。

那一句自己还有两处错，因为走不到，一直没人看见。双引号里的反引号会把 `governance gate --check`
当命令执行：stderr 出 `governance: command not found`，正文丢掉命令名。`$rc，` 在 UTF-8 locale 下
被 bash 3.2 读成变量 `rc\\xEF`，`set -u` 当场退出（见 `tests/architecture/test_shell_variables_next_to_non_ascii_are_braced.py`）。

这里跑脚本自己的那一段：`cd "$REPO"` 之后到结尾。前面几行读凭据文件，测试不该执行它们。
`beidou` 是桩，按环境变量 `STUB_OUTPUT`、`STUB_RC` 印出输出、以那个码退出；`governance reopen` 那一次直接以 0 退出
（09-29 起 gate 之前先跑它，它的读数另有 `test_the_governance_gate_job_reads_the_reopen_conditions.py`）。环境里没有 webhook 变量，notify 只打印、不发送。
bash 用本机的 /bin/bash，在操作者的 Mac 上就是 launchd 用的 3.2.57。两个 locale 都跑：
C 是 launchd 给的，C.UTF-8 是终端里手动跑的样子。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "deploy" / "run_governance_gate.sh"
BASH = Path("/bin/bash")

STUB = """#!/bin/bash
[ "$2" = "reopen" ] && exit 0
printf '%s' "$STUB_OUTPUT"
exit "$STUB_RC"
"""

pytestmark = pytest.mark.skipif(not BASH.exists(), reason="no /bin/bash on this machine")


def _block() -> str:
    """launcher 从 `cd "$REPO"` 之后到结尾，套上它自己的 shell 选项，在 checkout 根目录里跑。"""
    text = GATE.read_text(encoding="utf-8")
    options = "set -uo pipefail"
    assert f"\n{options}\n" in text, "launcher 的 shell 选项变了，按新的选项跑这一段"
    cd = 'cd "$REPO" || exit 78\n'
    assert cd in text, "launcher 不再这样进入 checkout，这个测试截取的起点要跟着挪"
    return f'{options}\nREPO="$(pwd)"\nSUPPORT="$REPO/support"\n{text[text.index(cd) + len(cd) :]}'


def _page(tmp_path: Path, locale: str, printed: str, rc: int) -> str:
    """跑一次失败的 gate，返回 notify 打印的那条告警正文。"""
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    beidou = repo / ".venv" / "bin" / "beidou"
    beidou.write_text(STUB, encoding="utf-8")
    beidou.chmod(0o755)
    script = tmp_path / "gate.sh"
    script.write_text(_block(), encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": locale, "STUB_OUTPUT": printed, "STUB_RC": str(rc)}

    run = subprocess.run([str(BASH), str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert run.returncode == 1, f"门没过，job 要以 1 退出：{run.stderr.strip()}"
    assert run.stderr == "", (
        "stderr 应当为空：`command not found` 是反引号被执行，`unbound variable` 是 bash 3.2 读错了变量名"
    )
    pages = [line for line in run.stdout.splitlines() if "FAIL governance-gate: " in line]
    assert len(pages) == 1, run.stdout
    return pages[0].split("FAIL governance-gate: ", 1)[1].strip()


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
def test_a_failure_that_printed_nothing_still_says_which_command_and_what_it_returned(
    tmp_path: Path, locale: str
) -> None:
    """进程被杀（137）或一声不吭地退出，都会走到这里。"""
    assert _page(tmp_path, locale, "", 137) == "`governance gate --check` 退出码 137，且没有任何输出"


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
def test_a_failure_with_no_verdict_pages_the_last_lines_it_printed(tmp_path: Path, locale: str) -> None:
    """命令没跑起来（导入炸了），输出里没有 FAIL 行。取尾部时空行不占名额。"""
    printed = "Traceback (most recent call last):\n  File beidou_cli\n\nImportError: cannot import name 'gate'\n"
    page = _page(tmp_path, locale, printed, 1)
    assert page.startswith("Traceback") and page.endswith("ImportError: cannot import name 'gate'"), page


@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
def test_a_verdict_pages_the_fail_lines_and_only_them(tmp_path: Path, locale: str) -> None:
    printed = "PASS       flow\nFAIL       tsmom   OOS 1.2306 vs 1.5251 at N=185\n"
    assert _page(tmp_path, locale, printed, 1) == "FAIL       tsmom   OOS 1.2306 vs 1.5251 at N=185"
