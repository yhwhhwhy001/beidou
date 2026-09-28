"""引擎不经报告层：干净解释器里 `import beidou_live.engine` 之后，`sys.modules` 里没有 `beidou_live.report*`。

WP-C6（2026-09-28，执行手册 §3.2）。此前 `engine.py` 从 `beidou_live.reports` 取 `collateral_share`。
这一个名字把整个报告层带进了引擎的 import 闭包：`reports` 再导出 8 个领域模块，加上 `report_common` 共 10 个。
现在 `collateral_share` 定义在 `risk_budget`。`report_risk` 与 `reports` 仍在原地址再导出同一个对象，
见 `test_the_report_layer_kept_its_addresses.py` 的 `MOVED_OUT`。

**这条钉住的是引擎模块的依赖方向，不是 armed 进程的 `sys.modules`。** armed 进程是 `beidou live run`。
CLI 入口 `beidou_cli` 一 import 就载入 `live_cmd`，而 `live_cmd` 在模块顶层 import 了 `beidou_live.report_beta`
与 `beidou_live.reports`。所以 armed 进程里仍有报告层，报告层 import 时抛异常，循环仍然起不来。
关掉这一半是另一个工作包。

必须在子进程里量，因为 pytest 进程里别的测试早就 import 过报告层。子进程的 cwd 是仓库根，
`-c` 把 cwd 放在 `sys.path` 最前，所以 worktree 与 CI 里量的都是本树的代码。子进程顺带印出 `__file__`，
核的就是这一点。第二条是对照：同一个探针 import `beidou_live.reports` 时必须看得见报告层。
否则第一条可能因为前缀写错而永远通过。
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


def test_the_probe_sees_the_report_layer_when_it_is_imported() -> None:
    modules = _report_modules_after_importing("beidou_live.reports")
    assert "'beidou_live.reports'" in modules and "'beidou_live.report_risk'" in modules, modules
