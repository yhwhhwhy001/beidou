# M00 第二轮对抗审查与修复证据（F10-R2）

## 审查结论摘要

独立审查代理对 fe62da152..HEAD 的 6 个提交构造反例，判定：F03/F05/F07 = CONFIRMED_BUG，F02/F04/F08 = RISK，F06 = CLEAN。共 10 个反例（A~K）。本轮全部处理如下。

## 反例处置矩阵

| 反例 | 内容 | 处置 | 证据 |
|---|---|---|---|
| A（F02） | 引擎侧 EXEMPT-07 第三自动 RESUME 路径仅靠未测试的 interlock 拦截 | ✅ 新增测试固化隐式耦合：`test_no_self_heal_engine_side_resume_blocked_by_interlock`（含授权有效时放行对照） | test_supervisor_testnet_auto_resume.py |
| B（F02） | --no-self-heal 下无人工恢复路径（CLI 无 resume 命令） | 📋 登记 M19：授权链挂接 + CLI resume 命令（受 TruthSnapshot 门禁） | 02-risk-baseline.md P0-12 |
| C（F03） | DEGRADED 期间新类型 P0 blocker 零告警 | ✅ 指纹变化检测：blocker 聚合指纹变化即告警（相同指纹不重复） | `test_new_blocker_type_during_degraded_triggers_alert` |
| D（F03） | HealthDebounce window=60s 容纳不下 lock_after=60 样本 → testnet LOCKED 死代码 | ✅ window_seconds=max(60, lock_after×interval×1.5)（testnet=450s）；测试模拟 60 连阻断 → LOCKED 可达 | `test_supervisor_debounce_window_supports_lock_after` |
| E（F04） | 活类 position_aggregate.apply_fill 无跨零防护但 docstring 声称有 | ✅ 修正误导性 docstring（事实入账不做改写）；活类防护策略 📋 登记 M12 | position_aggregate.py docstring |
| F（F04） | replay 空仓建仓序列可整体翻转且 compliance=True | ✅ 语义澄清：docstring 明示"空仓建仓不受限，仅拦截跨零反向"；单笔超量拦截已有测试；该语义为契约决策（与既有开仓用例一致） | contracts.py docstring + 9 个 replay 测试 |
| G（F05） | 登记表遗漏 ≥6 类特赦 | ✅ 补齐 EXEMPT-09~18（共 18 项）：对账容差 100×、inventory 空列表、MARGIN_CALL 信息性、流活性、replay baseline、权益估计、R9 提款权限、无主 Algo 清理、风控外部持仓、启动重试；新增**分支扫描完备性测试**（每个 `"testnet"` 比较行必须在标记 ±3 行内或白名单） | test_testnet_exemptions_registry.py（4/4） |
| H（F07） | wrapper 退出码映射与 supervisor 真实语义相反（4=启动失败无限重启；6=崩溃不重启） | ✅ 仅 5（LOCKED）映射为 0；4/6/其他非零透传重启（与 autopilot 既有运维语义一致：PG 短暂不可用需持续重试）；注释明确语义 | wrapper.sh + test_launchd_plist_governance.py |
| I（F07） | 漂移检查不查 wrapper；实装仍是旧危险 plist | ✅ 漂移检查增强：实装 ProgramArguments 不经 wrapper → drift 项；实装 plist 更新仍待用户授权（已登记） | preflight.py `_launchd_plist_drift` |
| J（F08） | policy_hash 不含参数值 → 审计事实失真 | ✅ `_policy_hash = sha256(id:version:canonical_params_json)` | engine.py 策略加载块 |
| K（F08） | 新键无类型/值域校验 → 非法值放大风险或静默停机 | ✅ `_validate_audited_policy_params`：7 键值域 spec + NaN/Inf 拒绝 + 止损交叉约束；非法 → _policy_error（写模式 readiness 阻断 fail-closed） | test_policy_parameter_audit.py 5 个新测试 |
| F06 小注 | Makefile 指向退役入口 | ✅ Makefile 三目标改为提示+exit 2 | Makefile |

## 测试证据

- R2 新增测试：10 个（interlock×1、指纹告警×1、防抖窗口×1、值域校验×5、wrapper 语义×2）—— 40/40 相关文件全绿
- 登记表扫描完备性测试：4/4（18 项登记 ↔ 引擎分支扫描 + 白名单）
- 全量回归：见本轮套件输出（后台运行结果补记）

## 残余 OPEN（已登记，责任模块明确）

1. M12：活类 PositionAggregate 仓位级 reduce-only 校验策略（反例 E 的深度修复）
2. M19：授权链挂接 + CLI 人工 resume 命令（反例 B）
3. 用户授权项：实装 plist 更新、策略文件补键+重签名、引擎重启
4. F06 小注：架构测试子串扫描可被别名绕过（接受为 tripwire 语义）

## 对抗审查方法论注记

- 全部 10 个反例先由本人逐项在源码复核（file:line）后才实施修复——审查员指控全部属实。
- 三轮"测试绿但行为错"的根因（覆盖对象错误/断言方向错误/完整性缺失）已通过 R2 测试设计修正：指纹变化、窗口可达性、值域校验、分支扫描均为"行为断言"而非"实现断言"。
