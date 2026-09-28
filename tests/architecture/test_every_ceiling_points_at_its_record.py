"""WP-C1：`CEILING` 的每个条目都指向它的抬顶记录，记录本身还在。

2026-09-28 操作者裁定 Q2a：source budget 的抬顶理由从 `test_source_budget.py` 的注释搬到
`docs/SOURCE_BUDGET_LOG.md`，原文逐字（`scratchpad/source_budget_log_migration.py`，带往返校验）。
测试文件里每个条目上方只留一行指针。搬迁之后最容易悄悄坏掉的是下面四件事，这里各守一条：

- 指针还在。新加一个包、或者抬顶时顺手删掉那一行，读代码的人就找不到理由了。
- 指针指得到地方。anchor 由 GitHub 按标题生成：`## beidou_live` 的 anchor 是 `#beidou_live`，下划线保留，
  不会变成连字符。2026-09-28 用 GitHub 渲染本仓库的 `docs/RUNBOOK.md` 核过：标题
  「Registry 里的书（`config/alpha_registry.yaml`）」的 anchor 是 `#registry-里的书configalpha_registryyaml`。
- 理由没有写回测试文件。`CEILING` 表里只有指针和条目；理由一旦写回来，这个文件又会长回四千行。
- 记录没有被「整理」掉。四千多行原文看起来像可以删减的冗余，但它是这个仓库最完整的设计记录，
  别人按现象措辞搜的就是它。少于 4,000 行就红。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.architecture.test_source_budget import CEILING

ROOT = Path(__file__).resolve().parents[2]
BUDGET = ROOT / "tests" / "architecture" / "test_source_budget.py"
LOG = ROOT / "docs" / "SOURCE_BUDGET_LOG.md"
POINTER = "    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#"
ENTRY = re.compile(r'^    "(?P<package>\w+)": [0-9_]+,$')
LOG_FLOOR = 4_000


def _anchor(heading: str) -> str:
    """GitHub 由标题生成 anchor：小写；字母、数字、下划线、连字符、空格以外的字符去掉；空格换成连字符。"""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _table() -> list[str]:
    """`CEILING = {` 与 `}` 之间的行。"""
    lines = BUDGET.read_text(encoding="utf-8").splitlines()
    start = lines.index("CEILING = {")
    return lines[start + 1 : lines.index("}", start)]


def _headings() -> list[str]:
    """记录里的二级标题。代码块里的行不算：原文注释里也有以 `#` 开头的行。"""
    headings: list[str] = []
    fenced = False
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            headings.append(line[3:])
    return headings


def test_every_ceiling_entry_has_its_pointer_directly_above_it() -> None:
    table = _table()
    entries = [(index, match["package"]) for index, line in enumerate(table) if (match := ENTRY.match(line))]
    assert [package for _, package in entries] == list(CEILING), "每个 CEILING 条目单独占一行"
    lost = [package for index, package in entries if index == 0 or table[index - 1] != POINTER + _anchor(package)]
    assert not lost, f"这些条目上方没有指向 docs/SOURCE_BUDGET_LOG.md 的那一行：{lost}"


def test_the_table_holds_only_pointers_and_entries() -> None:
    strays = [line for line in _table() if not (line.startswith(POINTER) or ENTRY.match(line))]
    assert not strays, f"理由写进 docs/SOURCE_BUDGET_LOG.md 对应包的一节，不写在 CEILING 表里：{strays[:3]}"


def test_every_pointer_lands_on_a_heading_in_the_log() -> None:
    anchors = {_anchor(heading): heading for heading in _headings()}
    missing = [package for package in CEILING if anchors.get(_anchor(package)) != package]
    assert not missing, f"docs/SOURCE_BUDGET_LOG.md 里没有这些包的 `## <包名>` 一节：{missing}"


def test_the_log_is_still_there_in_full() -> None:
    assert LOG.is_file(), "docs/SOURCE_BUDGET_LOG.md 不见了"
    count = len(LOG.read_text(encoding="utf-8").splitlines())
    assert count >= LOG_FLOOR, f"docs/SOURCE_BUDGET_LOG.md 只剩 {count:,} 行（下限 {LOG_FLOOR:,}）：原文不删、不并"
