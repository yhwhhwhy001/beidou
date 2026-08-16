# TASK-M00-F04 证据：引擎死代码清理与 reduce-only 跨零漏洞修复（P1-16）

## 移除清单（每个符号移除前全仓 grep 验证，零生产/测试调用）

| 符号 | 位置 | 类型 | 验证 |
|---|---|---|---|
| `run_parity_check` | engine.py:2783-2788 | UNREACHABLE 方法 | 全仓仅定义处 1 引用 |
| `build_strategy_signal` | engine.py:9727-9743 | UNREACHABLE 静态方法（含双重 docstring 语法） | 仅定义处 |
| `build_execution_plan` | engine.py:9790-9807 | UNREACHABLE 方法 | 仅定义处 |
| `build_position_aggregate` | engine.py:9809-9817 | UNREACHABLE 方法（且是契约版 replay 的唯一调用方） | 仅定义处 |
| `build_idempotency_key` | engine.py:9819-9823 | UNREACHABLE 方法 | 仅定义处 |
| `_cert_manager`/`_production_ladder` | engine.py:1799-1824 | 死接线（构造-即弃，全仓零读取） | 构造块外零引用 |
| `_kernel_parity` | engine.py:1891 | 死属性（赋值-即弃） | 赋值行外零引用 |

随之清理的失效导入：`ExecutionPlan/Fill/OrderIdempotencyKey/PlanSlice/PlanStatus/PositionAggregate`（beidou_safety.execution.contracts 整块）、`ParityResult`。

**保留**（防御模式判定为正确）：
- `_sync_exchange_state`（engine.py:9026）：调用即抛 `EXCHANGE_STATE_MUTATION_RECOVERY_DISABLED` 的 fail-closed 墓碑 —— 故意保留的禁用路径守卫，2 个测试依赖其存在性语义（test_reconciliation_contract.py:232-244）与源码锚点（test_architecture.py:683/697），保留。
- `_signed_position_from_account`（8571 在用）、`build_portfolio_target`（8606 在用）保留。

## reduce-only 跨零漏洞修复（beidou_safety/execution/contracts.py:126-147）

原实现：SELL 违规时 `pass` 空操作（成交仍被应用 → 跨零开仓潜伏炸弹），且缺失 BUY 侧对称防护，**连"SELL 5 on long 2"的跨零都未拦截**。
修复：逐笔独立判定 —— SELL 需 `net>0 且 qty<=net`；BUY 需 `net<0 且 qty<=|net|`；违规成交跳过并置 `is_reduce_only_compliant=False`；非合规聚合（历史事实）不再拦截。

## 测试证据

| 阶段 | 命令 | 结果 |
|---|---|---|
| 新测试 | `pytest tests/unit/test_position_aggregate_replay_contract.py tests/architecture/test_architecture.py ...` | 142 passed（含新增 9 个 replay 契约测试 + 1 个架构死符号回归测试） |
| 编译 | `python -m compileall beidou_core/engine.py beidou_safety/execution/contracts.py` | OK |
| lint（本任务文件） | `ruff check beidou_safety/execution/contracts.py tests/unit/test_position_aggregate_replay_contract.py tests/architecture/test_architecture.py` | All checks passed |
| 全量回归 | `pytest tests/ -q`（后台，结果补记） | 见下 |

**注**：engine.py 全文件 ruff 仍有 15 个既有错误（与基线完全一致，本次修改零新增零消除）。其中 **F821 `order_id` 未定义（engine.py:4617/4624）是生产成交处理路径上的真实未定义变量** —— 已登记至风险基线 → M11 修复。

## 架构回归护栏

`test_architecture.py::test_engine_has_no_unreachable_builder_stubs`：8 个已移除死符号不得重新出现。

## 跨模块影响声明

- 契约版 `PositionAggregate.replay` 的调用方（build_position_aggregate）已移除 → 该路径无生产消费者；修复属防未来误接。
- 认证接线（CertificationManager/ProductionLadder）从引擎实例移除 —— 监督器与 beidou_certification 独立管理认证，无行为变化（原接线从未被读取）。
- 运行引擎（PID 77694）仍运行旧代码；本修改下次重启生效。
