# TASK-M00-F06 证据：apps 遗留入口退役（P1-16）

## 问题

6 个非生产入口并存：strategy_engine/safety_executor 直接实例化 AutonomousEngine（硬编码 paper/safety_only）构成第二引擎启动路径；research_lab --run 与 factor_miner resume/compare 为误导性占位（打印"启动/分析"但不执行任何工作）。

## 修改

| 入口 | 处理 |
|---|---|
| apps/strategy_engine/__main__.py | 退役：打印迁移目标 + exit 2（不再实例化引擎） |
| apps/safety_executor/__main__.py | 退役：打印迁移目标 + exit 2 |
| apps/research_lab/__main__.py | --run 分支退役：显式 NOT-IMPLEMENTED + exit 2，指向 factor_miner CLI |
| apps/factor_miner/__main__.py | resume/compare 占位改为显式 NOT_IMPLEMENTED + sys.exit(2) |
| apps/autopilot/__main__.py | 保留（dev-only 手动入口，launchd 不使用） |

- 架构护栏 `test_architecture.py::test_no_second_engine_entry_outside_main_chain`：全仓扫描，除 beidou_launcher/supervisor.py（主链）与 apps/autopilot（dev-only）外任何 `AutonomousEngine(` 实例化即失败；退役入口必须有 retired 标记。**实测当前唯一两个实例化点 = 主链 + autopilot**。

## 测试证据

| 命令 | 结果 |
|---|---|
| `pytest tests/architecture/test_architecture.py` | 31 passed |
| `ruff check apps/` | All checks passed |
| 全量 `pytest tests/` | 2526 passed |
