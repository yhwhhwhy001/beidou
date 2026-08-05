"""R0-R10 风险规则注册表 — BD-07。

每条规则输出 PASS/REJECT/UNKNOWN，附带 reason、policy_version、facts_version 和 remediation。
所有规则通过注册表加载，不允许硬编码绕过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
        cls.RULES[rule.rule_id] = rule

    @classmethod
    def evaluate_all(cls, context: dict) -> dict[str, RuleDecision]:
        """按优先级执行所有规则，返回每个规则的结果。"""
        results: dict[str, RuleDecision] = {}
        for rule in sorted(cls.RULES.values(), key=lambda r: r.priority):
            try:
                results[rule.rule_id] = rule.evaluate(context)
            except Exception:
                results[rule.rule_id] = RuleDecision.UNKNOWN
        return results

    @classmethod
    def is_approved(cls, results: dict[str, RuleDecision]) -> bool:
        """所有规则必须 PASS。UNKNOWN 不算通过。"""
        return all(d == RuleDecision.PASS for d in results.values())


# ================================================================
# R0-R10 规则实现
# ================================================================

def _r0_max_leverage(context: dict) -> RuleDecision:
    """R0: 最大杠杆检查。"""
    leverage = context.get("leverage", 0)
    max_lev = context.get("max_leverage", 3.0)
    if leverage <= 0 or max_lev <= 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if leverage <= max_lev else RuleDecision.REJECT

def _r1_concentration(context: dict) -> RuleDecision:
    """R1: 集中度检查。"""
    conc = context.get("concentration_pct", 0)
    max_conc = context.get("max_concentration_pct", 50.0)
    if conc <= 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if conc <= max_conc else RuleDecision.REJECT

def _r2_drawdown(context: dict) -> RuleDecision:
    """R2: 回撤检查。"""
    dd = context.get("drawdown_pct", 0)
    max_dd = context.get("max_drawdown_pct", 20.0)
    return RuleDecision.PASS if dd < max_dd else RuleDecision.REJECT

def _r3_daily_loss(context: dict) -> RuleDecision:
    """R3: 单日亏损限制。"""
    daily = context.get("daily_loss_pct", 0)
    max_daily = context.get("max_daily_loss_pct", 5.0)
    return RuleDecision.PASS if daily < max_daily else RuleDecision.REJECT

def _r4_consecutive_losses(context: dict) -> RuleDecision:
    """R4: 连续亏损限制。"""
    consecutive = context.get("consecutive_losses", 0)
    max_cons = context.get("max_consecutive_losses", 5)
    return RuleDecision.PASS if consecutive < max_cons else RuleDecision.REJECT

def _r5_sharpe(context: dict) -> RuleDecision:
    """R5: Sharpe 比率下限。"""
    sharpe = context.get("rolling_sharpe", 0)
    min_sharpe = context.get("min_sharpe_rolling", 0.0)
    if sharpe is None:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if sharpe >= min_sharpe else RuleDecision.REJECT

def _r6_margin(context: dict) -> RuleDecision:
    """R6: 保证金充足率。"""
    margin_ratio = context.get("margin_ratio", 0)
    if margin_ratio <= 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if margin_ratio < 0.8 else RuleDecision.REJECT

def _r7_liquidation_distance(context: dict) -> RuleDecision:
    """R7: 清算距离。"""
    liq_price = context.get("liquidation_price", 0)
    current_price = context.get("current_price", 0)
    if liq_price <= 0 or current_price <= 0:
        return RuleDecision.UNKNOWN
    distance_pct = abs(current_price - liq_price) / current_price * 100
    return RuleDecision.PASS if distance_pct > 5.0 else RuleDecision.REJECT

def _r8_protection_coverage(context: dict) -> RuleDecision:
    """R8: 保护覆盖率。"""
    protected = context.get("protected_positions", 0)
    total = context.get("total_positions", 0)
    if total == 0:
        return RuleDecision.PASS
    if protected < 0 or total < 0:
        return RuleDecision.UNKNOWN
    return RuleDecision.PASS if protected == total else RuleDecision.REJECT

def _r9_account_capability(context: dict) -> RuleDecision:
    """R9: 账户能力检查。"""
    can_trade = context.get("can_trade", False)
    can_withdraw = context.get("can_withdraw", True)
    if can_withdraw:
        return RuleDecision.REJECT  # 提款权限必须关闭
    return RuleDecision.PASS if can_trade else RuleDecision.UNKNOWN

def _r10_duplicate_order(context: dict) -> RuleDecision:
    """R10: 重复订单检查。"""
    duplicate_count = context.get("duplicate_orders_24h", 0)
    return RuleDecision.PASS if duplicate_count == 0 else RuleDecision.REJECT


# 注册所有规则
for rule in [
    RiskRule("R0", "最大杠杆", "杠杆不得超过配置上限", 0, _r0_max_leverage,
             remediation="降低杠杆或减少仓位"),
    RiskRule("R1", "集中度", "单品种集中度不得超过上限", 1, _r1_concentration,
             remediation="分散到多个品种"),
    RiskRule("R2", "回撤限制", "回撤不得超过最大回撤百分比", 2, _r2_drawdown,
             remediation="暂停策略、等待回撤恢复"),
    RiskRule("R3", "单日亏损", "单日亏损不得超过上限", 3, _r3_daily_loss,
             remediation="当日停止交易"),
    RiskRule("R4", "连续亏损", "连续亏损笔数不得超过上限", 4, _r4_consecutive_losses,
             remediation="暂停策略、审查信号"),
    RiskRule("R5", "Sharpe下限", "滚动Sharpe不得低于下限", 5, _r5_sharpe,
             remediation="降级至Paper、重新校准"),
    RiskRule("R6", "保证金充足", "保证金使用率不得超过80%", 6, _r6_margin,
             remediation="减仓或追加保证金"),
    RiskRule("R7", "清算距离", "清算价格距离当前价格至少5%", 7, _r7_liquidation_distance,
             remediation="减仓或移动止损"),
    RiskRule("R8", "保护覆盖", "所有开放仓位必须有ACKed保护单", 8, _r8_protection_coverage,
             remediation="立即创建保护单或平仓"),
    RiskRule("R9", "账户能力", "账户必须无提款权限、有交易权限", 9, _r9_account_capability,
             remediation="更换API密钥、关闭提款权限"),
    RiskRule("R10", "重复订单", "24小时内不得有重复订单", 10, _r10_duplicate_order,
             remediation="检查幂等键、修复订单去重"),
]:
    RiskRuleRegistry.register(rule)
