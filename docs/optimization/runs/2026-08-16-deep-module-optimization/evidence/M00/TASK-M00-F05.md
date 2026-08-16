# TASK-M00-F05 证据：testnet 特赦集中登记表（P0-20 治理）

## 问题

引擎内 8 类 testnet-only 安全门放宽散落在 6 个代码区（对账漂移降级、覆盖豁免、user stream 新鲜度、清算价推导、对账 300s、liveness 放宽、自动 RESUME、零写跳过对账），无集中登记、无漂移检测、无责任模块指派 —— 单点失效即可放大实盘风险（P0-20）。

## 修改

- 新增 `beidou_launcher/testnet_exemptions.py`：8 项 `TestnetExemption`（id/理由/风险记录/责任模块 M10/M12/M13/M19）
- `beidou_core/engine.py` 8 处代码点添加 `# TESTNET-EXEMPT: EXEMPT-0X` 标记（仅注释，零行为变化）
- 新增 `tests/architecture/test_testnet_exemptions_registry.py`：
  - 登记表完整性（唯一 id/8 项/理由非空/责任模块非空）
  - 登记↔标记双向一致（登记必有标记、标记必已登记——防未治理新特赦）

## 测试证据

| 命令 | 结果 |
|---|---|
| `pytest tests/architecture/test_testnet_exemptions_registry.py` | 3 passed |
| 全量 `pytest tests/` | 2526 passed（最终验证） |

## 边界声明

- 收紧/移除每项特赦由责任模块逐项裁决（M10/M12/M13/M19）；本任务只建立可见性与防漂移护栏。
- 特赦标记仅限 testnet 条件分支内；live/canary/paper 语义未变。
