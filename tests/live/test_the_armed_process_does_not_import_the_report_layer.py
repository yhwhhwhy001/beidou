"""armed 进程不经报告层：干净解释器里 `import beidou_cli` 之后，`sys.modules` 里没有 `beidou_live.report*`。

分两步关掉。第一步是 WP-C6（#210，2026-09-28），关引擎。`engine.py` 原先从 `beidou_live.reports` 取
`collateral_share`，这一个名字把整个报告层带进了引擎的 import 闭包：`reports` 再导出 8 个领域模块，加上
`report_common` 共 10 个。现在 `collateral_share` 定义在 `risk_budget`。`report_risk` 与 `reports` 仍在
原地址再导出同一个对象，见 `test_the_report_layer_kept_its_addresses.py` 的 `MOVED_OUT`。

第二步关 CLI。armed 进程是 `deploy/run_live.sh` 起的 `beidou live run --armed`，入口 `beidou_cli:main`。
`beidou_cli/__init__.py` 一 import 就载入全部命令模块，而 `live_cmd` 原先在模块顶层从报告层取 15 个名字。
现在这些 import 挪进了用它们的四个命令：`live status`、`report daily`、`report weekly`、`report beta`。
报告层 import 时抛异常，倒下的只有这四个命令，循环照常起。改前 `import beidou_cli` 带进 10 个报告层模块，
改后 0 个。

**第二条测试才是对 armed 进程本身的断言。** 第一条留着，它钉的是引擎模块的依赖方向。第二条红的时候，
看第一条就知道是哪一层回退了。文件名在 #210 里没用手册的这个名字，因为当时 armed 进程仍 import 报告层；
第二步合入后，这个名字才说对。

它量不到的是函数内 import：import 时不执行，要等函数被调用。`live run` 调到的函数里若有人写
`from beidou_live.reports import ...`，这里照绿，循环却会在那个函数第一次跑时载入报告层。今天生产代码里，
从报告层外面 import 报告层的只有 `live_cmd` 那四个命令。

必须在子进程里量，因为 pytest 进程里别的测试早就 import 过报告层。子进程的 cwd 是仓库根，
`-c` 把 cwd 放在 `sys.path` 最前，所以 worktree 与 CI 里量的都是本树的代码。子进程顺带印出 `__file__`，
核的就是这一点。第三条是对照：同一个探针 import `beidou_live.reports` 时必须看得见报告层。
否则前两条可能因为前缀写错而永远通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _report_modules_after_importing(module: str) -> str:
    probe = (
        f"import {module}, sys; print({module}.__file__); "
        "print(sorted(m for m in sys.modules if m.startswith('beidou_live.report')))"
    )
    run = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
    imported_from, modules = run.stdout.splitlines()
    assert Path(imported_from).resolve().is_relative_to(ROOT), f"子进程 import 的是 {imported_from}，不是本树"
    return modules


def test_importing_the_engine_leaves_the_report_layer_out() -> None:
    modules = _report_modules_after_importing("beidou_live.engine")
    assert modules == "[]", f"引擎把报告层带进了 import 闭包：{modules}"


def test_importing_the_cli_leaves_the_report_layer_out() -> None:
    """`deploy/run_live.sh` 经 `.venv/bin/beidou` 起的就是 `beidou_cli:main`，import 的是这个包。"""
    modules = _report_modules_after_importing("beidou_cli")
    assert modules == "[]", f"`import beidou_cli` 把报告层带进了 armed 进程：{modules}"


def test_the_probe_sees_the_report_layer_when_it_is_imported() -> None:
    modules = _report_modules_after_importing("beidou_live.reports")
    assert "'beidou_live.reports'" in modules and "'beidou_live.report_risk'" in modules, modules
