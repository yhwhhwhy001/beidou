"""profile 的每个键都登记了读者，登记的读者也真的在读它（WP-C9，E-PR35，2026-09-28）。

`config/live.demo.yaml` 展成 `section.leaf`，2026-09-28 是 65 个键；顶层标量就是它自己的名字。E-PR35 量到：
`risk_budget` 块 15 个键里，只有 `min_liq_distance` 进 `LiveConfig`。其余 14 个只有 `report daily` 读，经
`RiskBudgetParams.from_mapping`。循环的 R8 尺子用 `RiskBudgetParams()` 的默认值，动手的档位来自 `Policy`。
所以改这 14 个键只改日报，不改循环。`test_the_digest_sees_every_live_knob.py` 变异的是 registry，看不到 profile。

`beidou_live/config.py` 顶部三张表写下每个键的去向，这里逐条核：

- 覆盖：每个键恰好在一张表里，表里没有 profile 已经删掉的键。变异那条调的也是 `assert_the_tables_cover`。
- `PROFILE_KEY_READERS`：登记的模块确实读这个键。
- `REPORT_ONLY_KEYS`：是 `RiskBudgetParams` 的字段；日报读它；循环装配自己的代码不读它。
- `UNREAD_KEYS`：哪个包里都查不到这个键。

**「读」怎么判。** `interval`、`registry`、`rest_url` 这类词在读者模块里到处都是。`execution_fidelity` 里的
`row.get("registry")`、`window["registry"]` 读的是周期记录；`live_cmd` 读 `market_data.rest_url`，不读
`venue.rest_url`。只找字面量，这些都会被当成证据。所以 `keys_read` 要求证据从 profile 的根一路连到这个键，
只认两种写法：

1. 查找链。先 `根.get("section")` 或 `根["section"]`，可以套 `or {}`，也可以先赋给一个局部名；再 `.get("leaf")`
   或读取语境下的 `["leaf"]`。顶层键直接在根上查。写入不算读。
2. 整块交出。`C.from_mapping(那一节)`、`C.from_mapping({**那一节, ...})` 或 `C(**那一节)`，而 `C` 在读者模块里是
   一个有 `leaf` 字段的 dataclass。profile 的几节交给的四个类（`PortfolioParams`、`ExitParams`、
   `DrawdownThrottleParams`、`RiskBudgetParams`）都按 `__dataclass_fields__` 过滤，有同名字段就会被读进去。

根只认两种：`load_profile(...)` 的返回值，和注解为映射、名叫 `profile` 的参数。`load_yaml(...)` 不算根：它也读
registry、universe、costs 三个文件，registry 里就有一个 `universe` 键。

**取舍：正面判定宁紧勿松。** 漏认一个真读者，测试变红，有人来看一眼；多认一个假读者，表就静默说错。按命名约定
认根，代价是换个参数名这里会红。`live_cmd` 里参数叫 `payload` 的几个 helper 因此不算读者，它们读的键都登记在
`beidou_live.config` 名下。反面的断言（「没人读」「循环不读」）要往红那边错，所以换成宽判定 `words_looked_up`：
在任何对象上查这个词都算读。「循环不读」还要求循环的装配代码里不出现 `RiskBudgetParams` 这个名字。

读 yaml 用 `yaml.safe_load`，不用 `load_profile`。后者会展开 `${BEIDOU_ALERTS_WEBHOOK_URL}`，而变异那条要把 profile
写进 `tmp_path`。webhook 地址是凭据，不该落进任何文件。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import itertools
from collections.abc import Mapping
from functools import cache
from pathlib import Path

import pytest
import yaml

from beidou_alpha.overlays.exits import ExitParams
from beidou_live.config import PROFILE_KEY_READERS, REPORT_ONLY_KEYS, UNREAD_KEYS
from beidou_live.risk_budget import RiskBudgetParams

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "config" / "live.demo.yaml"
TABLES: dict[str, frozenset[str]] = {
    "PROFILE_KEY_READERS": frozenset(PROFILE_KEY_READERS),
    "REPORT_ONLY_KEYS": REPORT_ONLY_KEYS,
    "UNREAD_KEYS": UNREAD_KEYS,
}
#: 日报在这里把 `risk_budget` 整块交给 `RiskBudgetParams.from_mapping`。
REPORT_READER = "beidou_cli.live_cmd"
#: 循环从 profile 装配自己的代码。`LiveConfig`、交易所、行情、池子、状态目录与模型在前两个模块里建，`live run` 把它们
#: 接起来，告警通道也在那里接。日报的命令也在 `live_cmd` 里，所以那个模块只看 `live_run` 一个函数。
LOOP_CODE: dict[str, str | None] = {
    "beidou_live.config": None,
    "beidou_live.composition": None,
    "beidou_cli.live_cmd": "live_run",
}


def profile_keys(path: Path) -> set[str]:
    """只展一层：第二层的值即使是列表或映射，读者也是整个拿走的。"""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for name, value in payload.items():
        keys |= {f"{name}.{leaf}" for leaf in value} if isinstance(value, dict) else {name}
    return keys


def assert_the_tables_cover(profile: Path) -> None:
    keys = profile_keys(profile)
    unrecorded = sorted(keys.difference(*TABLES.values()))
    assert not unrecorded, (
        f"{profile.name} 里这些键没有登记读者：{unrecorded}。在 beidou_live/config.py 的 PROFILE_KEY_READERS 写上"
        "读它的模块；只有日报读的放 REPORT_ONLY_KEYS，没人读的放 UNREAD_KEYS。"
    )
    stale = {name: sorted(table - keys) for name, table in TABLES.items() if table - keys}
    assert not stale, f"这些键已经不在 {profile.name} 里，表里还登记着：{stale}"
    shared = {
        f"{one} & {other}": sorted(TABLES[one] & TABLES[other])
        for one, other in itertools.combinations(TABLES, 2)
        if TABLES[one] & TABLES[other]
    }
    assert not shared, f"一个键只能在一张表里：{shared}"


def _lookup(node: ast.AST) -> tuple[ast.expr, str] | None:
    """`x.get("k", ...)` 与读取语境下的 `x["k"]` 给出 `(x, "k")`；别的都不是查找。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        receiver, key = node.func.value, node.args[0]
    elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        receiver, key = node.value, node.slice
    else:
        return None
    return (receiver, key.value) if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def _bare(node: ast.expr) -> ast.expr:
    """`x or {}` 就是 `x`。"""
    while isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    return node


def _handed_over(call: ast.Call) -> tuple[str, list[ast.expr]] | None:
    """`C.from_mapping(块)`、`C.from_mapping({**块, ...})`、`C(**块)`：给出 `C` 的名字和整块交出去的表达式。"""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "from_mapping" and isinstance(func.value, ast.Name):
        if not call.args:
            return None
        block = call.args[0]
        if isinstance(block, ast.Dict):
            return func.value.id, [value for key, value in zip(block.keys, block.values, strict=True) if key is None]
        return func.value.id, [block]
    unpacked = [keyword.value for keyword in call.keywords if keyword.arg is None]
    return (func.id, unpacked) if isinstance(func, ast.Name) and unpacked else None


def _read_in(function: ast.FunctionDef | ast.AsyncFunctionDef, namespace: Mapping[str, object]) -> set[str]:
    nodes = list(ast.walk(function))
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    roots = {
        argument.arg
        for argument in arguments
        if argument.arg == "profile"
        and argument.annotation is not None
        and any(word in ast.unparse(argument.annotation) for word in ("dict", "Mapping"))
    }
    assigned = [
        (node.targets[0].id, node.value)
        for node in nodes
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]
    roots |= {
        name
        for name, value in assigned
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "load_profile"
    }

    def is_root(node: ast.expr) -> bool:
        node = _bare(node)
        return isinstance(node, ast.Name) and node.id in roots

    def looked_up_on_root(node: ast.expr) -> str | None:
        hit = _lookup(_bare(node))
        return hit[1] if hit is not None and is_root(hit[0]) else None

    bound = {name: key for name, value in assigned if (key := looked_up_on_root(value)) is not None}

    def section(node: ast.expr) -> str | None:
        node = _bare(node)
        return bound.get(node.id) if isinstance(node, ast.Name) else looked_up_on_root(node)

    reads: set[str] = set()
    for node in nodes:
        if (hit := _lookup(node)) is not None:
            receiver, key = hit
            if (name := section(receiver)) is not None:
                reads.add(f"{name}.{key}")
            elif is_root(receiver):
                reads.add(key)
        if isinstance(node, ast.Call) and (handed := _handed_over(node)) is not None:
            owner = namespace.get(handed[0])
            if isinstance(owner, type) and dataclasses.is_dataclass(owner):
                for block in handed[1]:
                    if (name := section(block)) is not None:
                        reads |= {f"{name}.{field.name}" for field in dataclasses.fields(owner)}
    return reads


def keys_read(source: str, namespace: Mapping[str, object]) -> set[str]:
    """正面判定：`source` 里每个函数从 profile 的根一路读到的键。`namespace` 用来把类名解析成 dataclass。"""
    functions = [
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return set().union(*(_read_in(function, namespace) for function in functions))


def words_looked_up(tree: ast.AST) -> set[str]:
    """反面判定用的宽版本：在任何对象上 `.get("词")` 或读取 `["词"]`，这个词都算被读过。"""
    return {hit[1] for node in ast.walk(tree) if (hit := _lookup(node)) is not None}


@cache
def _module(name: str) -> tuple[str, dict[str, object]]:
    module = importlib.import_module(name)
    return Path(str(module.__file__)).read_text(encoding="utf-8"), vars(module)


@cache
def keys_read_by(name: str) -> frozenset[str]:
    return frozenset(keys_read(*_module(name)))


def test_every_key_of_the_shipped_profile_is_in_exactly_one_table() -> None:
    assert_the_tables_cover(SHIPPED)


def test_a_key_added_without_a_reader_is_named(tmp_path: Path) -> None:
    """变异自证：同一个断言函数，多一个没登记的键就失败，并且说出是哪个键。"""
    payload = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    payload["guards"]["made_up"] = 1
    mutated = tmp_path / SHIPPED.name
    mutated.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(AssertionError, match=r"guards\.made_up"):
        assert_the_tables_cover(mutated)


def test_every_recorded_reader_reads_its_key() -> None:
    unproven = {key: reader for key, reader in PROFILE_KEY_READERS.items() if key not in keys_read_by(reader)}
    assert not unproven, (
        f"登记的模块里找不到读这些键的地方：{unproven}。改成真正读它的模块。"
        "如果是换了读法，按模块说明里的两种写法补判定，不要放宽成找字面量。"
    )


def test_the_report_only_keys_are_risk_budget_fields_that_only_the_report_reads() -> None:
    strangers = sorted(REPORT_ONLY_KEYS - {f"risk_budget.{name}" for name in RiskBudgetParams.__dataclass_fields__})
    assert not strangers, f"不是 `risk_budget` 块里 RiskBudgetParams 的字段：{strangers}"
    unread = sorted(REPORT_ONLY_KEYS - keys_read_by(REPORT_READER))
    assert not unread, f"日报不再读这些键：{unread}。没人读了就挪进 UNREAD_KEYS。"
    for module, function in LOOP_CODE.items():
        tree: ast.AST = ast.parse(_module(module)[0])
        if function is not None:
            tree = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function)
        touched = sorted(key for key in REPORT_ONLY_KEYS if key.rpartition(".")[2] in words_looked_up(tree))
        if any(isinstance(node, ast.Name) and node.id == "RiskBudgetParams" for node in ast.walk(tree)):
            touched.append("RiskBudgetParams")
        assert not touched, (
            f"{module}{'.' + function if function else ''} 碰了 {touched}。循环读它们，它们就不再是只供日报的键："
            "挪进 PROFILE_KEY_READERS，并改掉这张表上面那段说明。"
        )


def test_nothing_looks_up_an_unread_key() -> None:
    words = {
        path.relative_to(ROOT).as_posix(): words_looked_up(ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(ROOT.glob("beidou_*/**/*.py"))
    }
    # 零命中既可能是「没人读」，也可能是扫描没扫到东西；先确认读 profile 的那几个模块在扫描范围里。
    assert {"beidou_live/config.py", "beidou_cli/live_cmd.py", "beidou_live/execution_fidelity.py"} <= set(words)
    found = {
        key: [path for path, looked_up in words.items() if key.rpartition(".")[2] in looked_up] for key in UNREAD_KEYS
    }
    found = {key: paths for key, paths in found.items() if paths}
    assert not found, f"这些键有人在查了：{found}。读的是 profile 就挪进 PROFILE_KEY_READERS；不是的话看一眼再说。"


PRECISION = [
    pytest.param('def f(profile: dict):\n    return profile.get("registry")\n', "registry", True, id="root"),
    pytest.param('def f(rows: list):\n    return rows[-1].get("registry")\n', "registry", False, id="cycle-row"),
    pytest.param(
        'def f(profile: str):\n    return load_yaml(profile).get("registry")\n', "registry", False, id="load-yaml"
    ),
    pytest.param(
        'def f(profile: dict):\n    return (profile.get("market_data") or {}).get("rest_url")\n',
        "market_data.rest_url",
        True,
        id="section",
    ),
    pytest.param(
        'def f(profile: dict):\n    return (profile.get("market_data") or {}).get("rest_url")\n',
        "venue.rest_url",
        False,
        id="other-section",
    ),
    pytest.param(
        'def f(report: dict):\n    return report.get("exits", {}).get("stop_loss")\n',
        "exits.stop_loss",
        False,
        id="not-the-profile",
    ),
    pytest.param(
        'def f(profile: dict):\n    profile.setdefault("paths", {})["state_dir"] = "x"\n',
        "paths.state_dir",
        False,
        id="write",
    ),
    pytest.param(
        'def f(path: str):\n    payload = load_profile(path)\n    alerts = payload.get("alerts") or {}\n'
        '    return alerts["webhook_url"]\n',
        "alerts.webhook_url",
        True,
        id="bound-name",
    ),
    pytest.param(
        "def f(profile: dict):\n"
        '    return ExitParams.from_mapping({**(profile.get("exits") or {}), "bars_per_day": 24})\n',
        "exits.stop_loss",
        True,
        id="handed-over",
    ),
    pytest.param(
        "def f(profile: dict):\n"
        '    return ExitParams.from_mapping({**(profile.get("exits") or {}), "bars_per_day": 24})\n',
        "exits.made_up",
        False,
        id="no-such-field",
    ),
]


@pytest.mark.parametrize(("source", "key", "reads"), PRECISION)
def test_the_evidence_is_the_key_and_not_a_word_that_spells_it(source: str, key: str, reads: bool) -> None:
    """模块说明里的判定，逐条写成例子：同一个词，只有从 profile 的根连过来才算读。"""
    assert (key in keys_read(source, {"ExitParams": ExitParams})) is reads
