"""M-003: the non-alpha line budget is measured, so a breach is visible instead of assumed away.

The plan set a budget - `beidou_live` <= 2,000 lines, non-alpha <= 6,000, and at least 60% of the source
in `beidou_alpha` - and made exceeding it trigger a KILL-003 re-review.  Nothing ever measured it, so the
budget was breached without anyone noticing: the audit found non-alpha at 6,761 lines against 6,000, and
`beidou_live` at 2,776 against 2,000.

This test does not enforce the plan's numbers, because meeting them today would mean deleting tested code
the operator asked for, which is a worse outcome than carrying the debt.  It ratchets instead: today's
measurement is the ceiling, so the breach cannot grow while the operator decides whether to re-price the
budget or spend effort shrinking it.  Decided 2026-09-04: the operator carries the breach for now and
revisits it as long-term work, so this test's job is to hold the line rather than to force a cleanup.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "beidou_alpha",
    "beidou_live",
    "beidou_cli",
    "beidou_data",
    "beidou_exchange",
    "beidou_shared",
    "beidou_governance",
)

# The plan's budget, kept here so the gap between intent and reality stays legible.
# The plan's original budget, plus the operator's 2026-09-04 revision of the alpha share from 60% to 90%.
# The share is deliberately NOT asserted against the tree: reaching 90% of lines would mean 68,706 lines of
# signal code against today's 3,661, and bloated signal code is what the V5 rebuild deleted.  The 90% target
# governs newly authored work and is measured per week by `beidou report weekly` (reports.effort_share).
PLAN_BUDGET = {"beidou_live": 2_000, "non_alpha_total": 6_000, "alpha_share_tree": 0.60, "alpha_share_effort": 0.90}

# 抬顶记录：docs/SOURCE_BUDGET_LOG.md。原先写在这里的总述和每个条目之上的理由，已原文逐字搬到那里（WP-C1，
# 2026-09-28 操作者裁定 Q2a）。以后抬顶，理由写进那个文件对应包的一节；这里每个条目上方只留一行指向那一节。
CEILING = {
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_live
    "beidou_live": 15_847,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_cli
    "beidou_cli": 8_740,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_data
    "beidou_data": 3_677,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_exchange
    "beidou_exchange": 747,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_shared
    "beidou_shared": 289,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_governance
    "beidou_governance": 4_725,
    # 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou_alpha
    "beidou_alpha": 11_110,
}


def _lines(package: str) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted((ROOT / package).rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def test_no_package_grows_past_its_measured_ceiling() -> None:
    measured = {package: _lines(package) for package in PACKAGES}
    over = {name: (count, CEILING[name]) for name, count in measured.items() if count > CEILING[name]}
    assert not over, (
        f"these packages grew past the ratchet: {over}. "
        "Either take the growth back out, or raise the ceiling in the same commit that justifies it."
    )


def test_the_plans_budget_is_a_record_not_a_gate() -> None:
    """2026-09-28 操作者裁定 Q5=是：非 alpha 的增长率被接受，缺口不再断言。

    这条测试原名 `test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined`，从
    2026-09-04 起每天断言「非 alpha 超 6,000、beidou_live 超 2,000、alpha 占比低于 60%」仍然成立。它每天
    都绿，却推动不了任何决定——缺口一直在长，而「越界」这件事本身已经没有新信息。裁定之后 `PLAN_BUDGET`
    是历史记录：这里只钉它的字面量，改它等于改写 2026-09-04 定下的预算，要另一次裁定。缺口本身挪进
    周报的「Plan budget gap」一节（`beidou_live/report_governance.py` 的 `plan_budget_gap`，M-PR01），
    只印不告警，周日由 `deploy/com.beidou.weekly.plist` 跑。

    出处：`docs/analysis/2026-09-28-production-refactor-execution-plan.md` §3.9；裁定原文在
    `docs/analysis/2026-09-28-production-refactor-deep-analysis.md` §14（14.1 裁定表的 Q5 行）。
    """
    assert PLAN_BUDGET == {
        "beidou_live": 2_000,
        "non_alpha_total": 6_000,
        "alpha_share_tree": 0.60,
        "alpha_share_effort": 0.90,
    }, "PLAN_BUDGET 是 2026-09-04 的预算原文；重定价是一次裁定，不是一次编辑"
