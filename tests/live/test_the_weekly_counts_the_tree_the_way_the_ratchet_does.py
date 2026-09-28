"""周报的「Plan budget gap」读的行数，就是 ratchet 看见的行数；缺口只印，读不出的就说读不出。

2026-09-28 操作者裁定 Q5 = 是：非 alpha 的增长率被接受。`tests/architecture/test_source_budget.py` 那条
缺口测试因此改成只钉 `PLAN_BUDGET` 的字面量，缺口本身挪进周报（M-PR01，执行手册 §3.9）。

生产代码不 import 测试模块，所以周报的计数 `report_governance.package_lines` 是 ratchet 的 `_lines`
另写的一份。两份一旦口径分叉——一边开始跳过空行、换成 `wc -l`、或者少数一个包——周报印的数就不再是
ratchet 看见的数，而没有任何东西会说出来。第一条测试把两者钉在同一棵树的同一个数上：仓库自己这棵，
加一棵专门放了边角的合成树（最后一行没有换行、CRLF、换页符、空文件、`__pycache__` 里的 `.py`、
不存在的包）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beidou_live.report_governance import (
    ALPHA_EFFORT_TARGET,
    PLAN_BUDGET,
    SOURCE_PACKAGES,
    _plan_budget_lines,
    package_lines,
    plan_budget_gap,
)
from beidou_live.reports import weekly_markdown, weekly_payload
from beidou_live.state import StateStore
from tests.architecture import test_source_budget as ratchet

ROOT = Path(__file__).resolve().parents[2]
NOW = {
    "beidou_alpha": 900,
    "beidou_live": 700,
    "beidou_cli": 300,
    "beidou_data": 50,
    "beidou_exchange": 25,
    "beidou_shared": 15,
    "beidou_governance": 10,
}
SECTION = "## Plan budget gap (M-PR01, record only)"


def _edge_case_tree(root: Path) -> None:
    files = {
        "beidou_alpha/signal.py": "x = 1\ny = 2",  # no newline after the last line
        "beidou_live/crlf.py": "a\r\nb\r\n",
        "beidou_live/nested/page_break.py": "\x0c\nz\n",  # a form feed is a line boundary to splitlines
        "beidou_cli/__pycache__/stale.py": "never\ncounted\n",
        "beidou_data/notes.txt": "not python\n",
        "beidou_exchange/__init__.py": "",
        "beidou_governance/accent.py": "# é\n",
    }  # beidou_shared is left out on purpose: a package with no directory counts as zero on both sides
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


@pytest.mark.parametrize("tree", ["the repo", "edge cases"])
def test_the_weekly_and_the_ratchet_count_the_same_tree_to_the_same_number(
    tree: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ROOT
    if tree == "edge cases":
        _edge_case_tree(tmp_path)
        monkeypatch.setattr(ratchet, "ROOT", tmp_path)  # `_lines` reads the module's ROOT when it is called
        root = tmp_path
    assert SOURCE_PACKAGES == ratchet.PACKAGES
    assert package_lines(root) == {package: ratchet._lines(package) for package in ratchet.PACKAGES}


def test_the_edge_cases_are_counted_the_way_splitlines_counts_them(tmp_path: Path) -> None:
    """Pinned numbers, so the equality above cannot pass by both sides going wrong together."""
    _edge_case_tree(tmp_path)
    assert package_lines(tmp_path) == {
        "beidou_alpha": 2,
        "beidou_live": 5,
        "beidou_cli": 0,
        "beidou_data": 0,
        "beidou_exchange": 0,
        "beidou_shared": 0,
        "beidou_governance": 1,
    }


def test_the_weekly_carries_the_plans_budget_as_the_ratchet_file_records_it() -> None:
    assert {**PLAN_BUDGET, "alpha_share_effort": ALPHA_EFFORT_TARGET} == ratchet.PLAN_BUDGET


def test_the_gap_is_read_against_the_plan_and_the_growth_is_non_alpha_per_day() -> None:
    gap = plan_budget_gap(NOW, {**NOW, "beidou_live": 630, "beidou_alpha": 100})
    assert gap["non_alpha_total"] == 1_100 and gap["alpha_share_tree"] == pytest.approx(0.45)
    assert gap["non_alpha_growth_per_day"] == pytest.approx(10.0), "alpha's +800 is not non-alpha growth"
    rows = _plan_budget_lines(gap)
    assert rows["beidou_live"] == "700 (plan 2,000)" and rows["beidou_alpha"] == "900"
    assert rows["non_alpha_total"] == "1,100 (plan 6,000)"
    assert rows["alpha_share_tree"] == "45.00% (plan 60.00%)"
    assert rows["non_alpha_growth_per_day (last 7d)"] == "+10.0"


def test_a_week_git_could_not_read_prints_unreadable_rather_than_zero() -> None:
    gap = plan_budget_gap(NOW, None)
    assert gap["non_alpha_growth_per_day"] is None
    assert _plan_budget_lines(gap)["non_alpha_growth_per_day (last 7d)"].startswith("不可读")


def test_the_weekly_prints_the_section_beside_the_effort_share(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    markdown = weekly_markdown(weekly_payload(store, "2026-09-26", source_lines=NOW, source_lines_week_ago=NOW))
    section = markdown.split(SECTION)[1].split("\n## ")[0]
    assert "| non_alpha_growth_per_day (last 7d) | +0.0 |" in section
    assert markdown.index("## Effort share") < markdown.index(SECTION) < markdown.index("## Margin (M-007)")
    # A caller that passes no tree gets the same "none" every absent weekly reading prints.
    assert f"{SECTION}\n\n| key | value |\n| --- | --- |\n| none | 0 |" in weekly_markdown(
        weekly_payload(store, "2026-09-26")
    )
