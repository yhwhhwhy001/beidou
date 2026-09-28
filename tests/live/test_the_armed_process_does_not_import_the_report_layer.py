"""armed 进程不经报告层：干净解释器里 `import beidou_cli` 之后，`sys.modules` 里没有 `beidou_live.report*`。

分两步关掉。第一步是 WP-C6（#210，2026-09-28），关引擎。`engine.py` 原先从 `beidou_live.reports` 取
`collateral_share`，这一个名字把整个报告层带进了引擎的 import 闭包：`reports` 再导出 8 个领域模块，加上
`report_common` 共 10 个。现在 `collateral_share` 定义在 `risk_budget`。`report_risk` 与 `reports` 仍在
原地址再导出同一个对象，见 `test_the_report_layer_kept_its_addresses.py` 的 `MOVED_OUT`。

第二步是 #224，关 CLI。armed 进程是 `deploy/run_live.sh` 起的 `beidou live run --armed`，入口 `beidou_cli:main`。
`beidou_cli/__init__.py` 一 import 就载入全部命令模块，而 `live_cmd` 原先在模块顶层从报告层取 17 个名字。
现在这些 import 挪进了用它们的函数：四个命令 `live status`、`report daily`、`report weekly`、`report beta`，
加 `report weekly` 的 helper `_source_lines_days_before_head`。报告层 import 时抛异常，倒下的只有这四个命令，
循环照常起。改前 `import beidou_cli` 带进 10 个报告层模块，改后 0 个。

**第二条测试才是对 armed 进程本身的断言。** 第一条留着，它钉的是引擎模块的依赖方向。第二条红的时候，
看第一条就知道是哪一层回退了。文件名在 #210 里没用手册的这个名字，因为当时 armed 进程仍 import 报告层；
第二步合入后，这个名字才说对。

必须在子进程里量，因为 pytest 进程里别的测试早就 import 过报告层。子进程的 cwd 是仓库根，
`-c` 把 cwd 放在 `sys.path` 最前，所以 worktree 与 CI 里量的都是本树的代码。子进程顺带印出 `__file__`，
核的就是这一点。第三条是对照：同一个探针 import `beidou_live.reports` 时必须看得见报告层。
否则前两条可能因为前缀写错而永远通过。

探针量不到函数内 import：import 时不执行，要等函数被调用。`live run` 调到的函数里若有人写
`from beidou_live.reports import ...`，探针照绿，循环却会在那个函数第一次跑时载入报告层。第四条补这一半：
它读 AST、不跑代码，报告层以外每一处 import 报告层的位置都要在 `REPORT_LAYER_IMPORTERS` 里点名。
比的是集合相等，所以名单上的函数不再 import 时它也红；扫描坏了、一处都没扫到，同样会红。
动态 import（`importlib.import_module("beidou_live.reports")`）不在它的眼里，今天生产代码里没有。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 报告层以外、import 报告层的全部位置：（文件，所在的顶层定义）。点名而不是计数，与
#: `tests/architecture/test_every_module_is_reachable_from_an_entry_point.py` 的 EXEMPT 同一个理由：
#: 计数会让下一个悄悄进来。今天只有 `live_cmd` 的五个函数，`live run` 一个都不调。
REPORT_LAYER_IMPORTERS = {
    ("beidou_cli/live_cmd.py", "live_status"),
    ("beidou_cli/live_cmd.py", "report_daily"),
    ("beidou_cli/live_cmd.py", "report_weekly"),
    ("beidou_cli/live_cmd.py", "report_beta"),
    ("beidou_cli/live_cmd.py", "_source_lines_days_before_head"),
}


def _report_modules_after_importing(module: str) -> str:
    probe = (
        f"import {module}, sys; print({module}.__file__); "
        "print(sorted(m for m in sys.modules if m.startswith('beidou_live.report')))"
    )
    run = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
    imported_from, modules = run.stdout.splitlines()
    assert Path(imported_from).resolve().is_relative_to(ROOT), f"子进程 import 的是 {imported_from}，不是本树"
    return modules


def _imported_modules(node: ast.Import | ast.ImportFrom, path: str) -> list[str]:
    """这一句 import 引到的全部模块名。相对 import 按文件所在的包还原成绝对名。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = path.removesuffix(".py").split("/")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    # `from beidou_live import reports` 引的是子模块，只看 `node.module` 会漏掉它。
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _report_layer_importers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for file in sorted(ROOT.glob("beidou_*/**/*.py")):
        path = file.relative_to(ROOT).as_posix()
        if path.startswith("beidou_live/report"):  # 报告层自己
            continue
        for top in ast.parse(file.read_text(encoding="utf-8")).body:
            owner = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "<module>"
            for node in ast.walk(top):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                    name.startswith("beidou_live.report") for name in _imported_modules(node, path)
                ):
                    found.add((path, owner))
    return found


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


def test_only_the_named_functions_import_the_report_layer() -> None:
    found = _report_layer_importers()
    extra, stale = sorted(found - REPORT_LAYER_IMPORTERS), sorted(REPORT_LAYER_IMPORTERS - found)
    assert not extra and not stale, (
        f"名单外多了 import 报告层的地方：{extra}；名单上已不再 import 的：{stale}。"
        "多出来的若在 `live run` 会调到的路径上，循环会在它第一次跑时载入报告层，报告层一坏循环就倒，要挪走；"
        "若是新的报告命令，加进 REPORT_LAYER_IMPORTERS。不再 import 的，从名单里删掉。"
    )
