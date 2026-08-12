"""R0-R10 风险规则注册表 — BD-07。

每条规则输出 PASS/REJECT/UNKNOWN，附带 reason、policy_version、facts_version 和 remediation。
所有规则通过注册表加载，不允许硬编码绕过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class RuleDecision(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RiskRule:
    """不可变风险规则定义。"""

    rule_id: str  # R0, R1, ..., R10
    name: str
    description: str
    priority: int  # 0=最高
    evaluate: Callable[..., RuleDecision]
    policy_version: str = "2.0.0"
    remediation: str | None = None
    auto_remediation: bool = False


class RiskRuleRegistry:
    """R0-R10 风险规则注册表。

    规则按 priority 排序执行。任一 REJECT 阻断交易。
    UNKNOWN 阻止新增风险但不强制退出已有仓位。
    """

    RULES: dict[str, RiskRule] = {}

    @classmethod
    def register(cls, rule: RiskRule) -> None:
        if not rule.rule_id or rule.rule_id not in {f"R{i}" for i in range(11)}:
            raise ValueError("Risk rule_id must be one of R0-R10")
        if not callable(rule.evaluate):
            raise ValueError("Risk rule evaluate callback must be callable")
        if rule.rule_id in cls.RULES:
            raise ValueError(f"Risk rule already registered: {rule.rule_id}")
        cls.RULES[rule.rule_id] = rule

    @classmethod
    def evaluate_all(cls, context: dict) -> dict[str, RuleDecision]:
        """按优先级执行所有规则，返回每个规则的结果。"""
        results: dict[str, RuleDecision] = {}
        for rule in sorted(cls.RULES.values(), key=lambda r: r.priority):
            try:
                decision = rule.evaluate(context)
                results[rule.rule_id] = decision if isinstance(decision, RuleDecision) else RuleDecision.UNKNOWN
            except Exception:
                results[rule.rule_id] = RuleDecision.UNKNOWN
        return results

    @classmethod
    def is_approved(cls, results: dict[str, RuleDecision]) -> bool:
        """所有规则必须 PASS。UNKNOWN 不算通过。"""
        return set(results) == set(cls.RULES) and all(d is RuleDecision.PASS for d in results.values())


# ================================================================
# R0-R10 规则实现
# ================================================================


def _r0_max_leverage(context: dict) -> RuleDecision:
    """R0: 最大杠杆检查。"""
    leverage = context.get("leverage")
    max_lev = context.get("max_leverage")
    if leverage is None or max_lev is None:
        return RuleDecision.UNKNOWN
    try:
        leverage = float(leverage)
        max_lev = float(max_lev)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(leverage) or not math.isfinite(max_lev) or leverage <= 0 or max_lev <= 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if leverage <= max_lev else RuleDecision.REJECT


def _r1_concentration(context: dict) -> RuleDecision:
    """R1: 集中度检查。"""
    conc = context.get("concentration_pct")
    max_conc = context.get("max_concentration_pct")
    if conc is None or max_conc is None:
        return RuleDecision.UNKNOWN
    try:
        conc = float(conc)
        max_conc = float(max_conc)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(conc) or not math.isfinite(max_conc) or conc <= 0 or max_conc <= 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if conc <= max_conc else RuleDecision.REJECT


def _r2_drawdown(context: dict) -> RuleDecision:
    """R2: 回撤检查。缺失数据返回 UNKNOWN (fail-closed)。"""
    dd = context.get("drawdown_pct")
    if dd is None:
        return RuleDecision.UNKNOWN
    max_dd = context.get("max_drawdown_pct")
    if max_dd is None:
        return RuleDecision.UNKNOWN
    try:
        dd = float(dd)
        max_dd = float(max_dd)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(dd) or not math.isfinite(max_dd) or dd < 0 or max_dd < 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if dd < max_dd else RuleDecision.REJECT


def _r3_daily_loss(context: dict) -> RuleDecision:
    """R3: 单日亏损限制。缺失数据返回 UNKNOWN (fail-closed)。"""
    daily = context.get("daily_loss_pct")
    if daily is None:
        return RuleDecision.UNKNOWN
    max_daily = context.get("max_daily_loss_pct")
    if max_daily is None:
        return RuleDecision.UNKNOWN
    try:
        daily = float(daily)
        max_daily = float(max_daily)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(daily) or not math.isfinite(max_daily) or daily < 0 or max_daily < 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if daily < max_daily else RuleDecision.REJECT


def _r4_consecutive_losses(context: dict) -> RuleDecision:
    """R4: 连续亏损限制。缺失数据返回 UNKNOWN (fail-closed)。"""
    consecutive = context.get("consecutive_losses")
    if consecutive is None:
        return RuleDecision.UNKNOWN
    max_cons = context.get("max_consecutive_losses")
    if max_cons is None:
        return RuleDecision.UNKNOWN
    try:
        if isinstance(consecutive, bool) or isinstance(max_cons, bool):
            return RuleDecision.UNKNOWN
        consecutive_float = float(consecutive)
        max_cons_float = float(max_cons)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if (
        not math.isfinite(consecutive_float)
        or not math.isfinite(max_cons_float)
        or not consecutive_float.is_integer()
        or not max_cons_float.is_integer()
        or consecutive_float < 0
        or max_cons_float < 0
    ):
        return RuleDecision.UNKNOWN
    consecutive = int(consecutive_float)
    max_cons = int(max_cons_float)
    return RuleDecision.PASS if consecutive < max_cons else RuleDecision.REJECT


def _r5_sharpe(context: dict) -> RuleDecision:
    """R5: Sharpe 比率下限。未校准或缺失保持 UNKNOWN。"""
    sharpe = context.get("rolling_sharpe")
    if sharpe is None:
        return RuleDecision.UNKNOWN
    min_sharpe = context.get("min_sharpe_rolling")
    if min_sharpe is None:
        return RuleDecision.UNKNOWN
    try:
        sharpe = float(sharpe)
        min_sharpe = float(min_sharpe)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(sharpe) or not math.isfinite(min_sharpe):
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if sharpe >= min_sharpe else RuleDecision.REJECT


def _r6_margin(context: dict) -> RuleDecision:
    """R6: 保证金充足率。"""
    margin_ratio = context.get("margin_ratio")
    max_margin_ratio = context.get("max_margin_ratio")
    if margin_ratio is None or max_margin_ratio is None:
        return RuleDecision.UNKNOWN
    try:
        margin_ratio = float(margin_ratio)
        max_margin_ratio = float(max_margin_ratio)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if (
        not math.isfinite(margin_ratio)
        or not math.isfinite(max_margin_ratio)
        or margin_ratio <= 0
        or max_margin_ratio <= 0
    ):
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if margin_ratio < max_margin_ratio else RuleDecision.REJECT


def _r7_liquidation_distance(context: dict) -> RuleDecision:
    """R7: 清算距离。

    ``liquidation_price`` 缺失只有在已明确证明没有持仓时才是正常的。
    否则把缺失的交易所事实当成“无清算风险”会错误放行加仓。
    """
    position_qty = context.get("position_qty")
    if position_qty is None:
        return RuleDecision.UNKNOWN
    try:
        position_qty = float(position_qty)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(position_qty):
        return RuleDecision.UNKNOWN

    liq_price = context.get("liquidation_price")
    current_price = context.get("current_price")
    if position_qty == 0:
        # A non-zero liquidation price with a proven-flat position is an
        # inconsistent snapshot, not a reason to pass the rule.
        if liq_price not in (None, 0, 0.0, "0", "0.0"):
            return RuleDecision.UNKNOWN
        return RuleDecision.PASS
    if liq_price is None or current_price is None:
        return RuleDecision.UNKNOWN
    try:
        liq_price = float(liq_price)
        current_price = float(current_price)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(liq_price) or not math.isfinite(current_price):
        return RuleDecision.UNKNOWN
    if liq_price <= 0 or current_price <= 0:
        return RuleDecision.UNKNOWN
    if (position_qty > 0 and liq_price >= current_price) or (position_qty < 0 and liq_price <= current_price):
        return RuleDecision.UNKNOWN
    distance_pct = abs(current_price - liq_price) / current_price * 100
    minimum_distance_pct = context.get("min_liquidation_distance_pct")
    if minimum_distance_pct is None:
        return RuleDecision.UNKNOWN
    try:
        minimum_distance_pct = float(minimum_distance_pct)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(minimum_distance_pct) or minimum_distance_pct < 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if distance_pct > minimum_distance_pct else RuleDecision.REJECT


def _r8_protection_coverage(context: dict) -> RuleDecision:
    """R8: 保护覆盖率，要求计数来自同一份已验证快照。"""
    protected = context.get("protected_positions")
    total = context.get("total_positions")
    if protected is None or total is None:
        return RuleDecision.UNKNOWN
    try:
        protected_float = float(protected)
        total_float = float(total)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(protected_float) or not math.isfinite(total_float):
        return RuleDecision.UNKNOWN
    if protected_float < 0 or total_float < 0 or not protected_float.is_integer() or not total_float.is_integer():
        return RuleDecision.UNKNOWN
    protected = int(protected_float)
    total = int(total_float)
    if protected > total:
        return RuleDecision.UNKNOWN
    if total == 0:
        return RuleDecision.PASS
    return RuleDecision.PASS if protected == total else RuleDecision.REJECT


def _r9_account_capability(context: dict) -> RuleDecision:
    """R9: 账户能力检查。"""
    can_trade = context.get("can_trade")
    can_withdraw = context.get("can_withdraw")
    if not isinstance(can_trade, bool) or not isinstance(can_withdraw, bool):
        return RuleDecision.UNKNOWN
    if can_withdraw:
        return RuleDecision.REJECT  # 提款权限必须关闭
    return RuleDecision.PASS if can_trade else RuleDecision.UNKNOWN


def _r10_duplicate_order(context: dict) -> RuleDecision:
    """R10: 重复订单检查。计数必须来自 durable 幂等身份事实。"""
    duplicate_count = context.get("duplicate_orders_24h")
    if duplicate_count is None:
        return RuleDecision.UNKNOWN
    if isinstance(duplicate_count, bool):
        return RuleDecision.UNKNOWN
    try:
        duplicate_float = float(duplicate_count)
    except (TypeError, ValueError):
        return RuleDecision.UNKNOWN
    if not math.isfinite(duplicate_float) or duplicate_float < 0 or not duplicate_float.is_integer():
        return RuleDecision.UNKNOWN
    duplicate_count = int(duplicate_float)
    return RuleDecision.PASS if duplicate_count == 0 else RuleDecision.REJECT


# 注册所有规则
for rule in [
    RiskRule("R0", "最大杠杆", "杠杆不得超过配置上限", 0, _r0_max_leverage, remediation="降低杠杆或减少仓位"),
    RiskRule("R1", "集中度", "单品种集中度不得超过上限", 1, _r1_concentration, remediation="分散到多个品种"),
    RiskRule("R2", "回撤限制", "回撤不得超过最大回撤百分比", 2, _r2_drawdown, remediation="暂停策略、等待回撤恢复"),
    RiskRule("R3", "单日亏损", "单日亏损不得超过上限", 3, _r3_daily_loss, remediation="当日停止交易"),
    RiskRule("R4", "连续亏损", "连续亏损笔数不得超过上限", 4, _r4_consecutive_losses, remediation="暂停策略、审查信号"),
    RiskRule("R5", "Sharpe下限", "滚动Sharpe不得低于下限", 5, _r5_sharpe, remediation="降级至Paper、重新校准"),
    RiskRule("R6", "保证金充足", "保证金使用率不得超过已签发策略上限", 6, _r6_margin, remediation="减仓或追加保证金"),
    RiskRule(
        "R7",
        "清算距离",
        "清算价格距离当前价格不得低于已签发策略下限",
        7,
        _r7_liquidation_distance,
        remediation="减仓或移动止损",
    ),
    RiskRule(
        "R8",
        "保护覆盖",
        "所有开放仓位必须有ACKed保护单",
        8,
        _r8_protection_coverage,
        remediation="立即创建保护单或平仓",
    ),
    RiskRule(
        "R9",
        "账户能力",
        "账户必须无提款权限、有交易权限",
        9,
        _r9_account_capability,
        remediation="更换API密钥、关闭提款权限",
    ),
    RiskRule(
        "R10", "重复订单", "24小时内不得有重复订单", 10, _r10_duplicate_order, remediation="检查幂等键、修复订单去重"
    ),
]:
    RiskRuleRegistry.register(rule)
