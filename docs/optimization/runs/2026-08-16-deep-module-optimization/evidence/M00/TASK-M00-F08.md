# TASK-M00-F08 证据：硬编码参数治理（P1-01 的 M00 部分）

## 问题

4 处交易语义参数硬编码在引擎代码中，游离于签名策略治理体系之外：
- 费率档 `set_fee_tier(venue, "vip1", 2.0, 4.0)`（engine.py 原 4771/8588 两处）
- ATR 止损钳制 `max(1.0, min(atr_pct*1.5, 5.0))`（原 8392）
- 资本预算 `account_balance * 0.1`（原 8484）
- 保证金使用率上限 `max_margin_ratio: 0.95`（原 8701）

## 修改

- 新增 `_policy_float_audited(key, default)`：策略提供字段→生效；缺失→保守默认+**首次 WARN 打印**（绝不静默采用代码常量）
- 抽取 `_compute_stop_loss_pct(atr_pct)` / `_apply_fee_tier(venue_id)` / `_capital_budget_amount(balance)` 三个可测试接缝；4 处调用点全部接入
- 策略键：`maker_fee_bps`(2.0) / `taker_fee_bps`(4.0) / `stop_loss_min_pct`(1.0) / `stop_loss_max_pct`(5.0) / `stop_loss_atr_multiplier`(1.5) / `capital_budget_ratio`(0.1) / `max_margin_ratio`(0.95)

## 测试证据（TDD：先测试后实现，7/7）

| REQ | 测试 |
|---|---|
| 策略提供→生效 | test_policy_float_audited_uses_policy_value / stop_loss_pct_policy_override / fee_tier_policy_override / capital_budget_policy_override / max_margin_ratio_reads_policy |
| 缺失→默认+首次 WARN（不重复、不静默） | test_policy_float_audited_falls_back_with_single_warn（断言 WARN 恰好 1 次） |
| 钳制数学 | test_stop_loss_pct_defaults_clamp（超限/低于下限/区间内） |

| 命令 | 结果 |
|---|---|
| `pytest tests/unit/test_policy_parameter_audit.py` | 7 passed |
| 全量 `pytest tests/` | **2533 passed** |
| `ruff check tests/unit/test_policy_parameter_audit.py` | All checks passed |
| `ruff check beidou_core/engine.py` | 15 errors（= 基线，无新增） |

## 待用户确认（策略文件更新）

代码侧已完成；`config/policies/risk_parameters.json` 尚未加入 7 个新键。当前行为：写模式下引擎以保守默认运行并打印 `[policy] PARAMETER_MISSING_FALLBACK:<key>=<默认>`（审计可见，非静默）。若要正式生效需：
1. 在 risk_parameters.json 的 parameters 增加 7 键（值经用户确认）
2. 用 BEIDOU_SIGNING_KEY 重新签名（repository 自带 signer）
3. 重启引擎后 `_policy_float_audited` 自动读取

该文件是签名制品且运行引擎正在使用 → 写入需用户显式确认后执行。
