# PKG-00 交付报告 — 仓库、领域边界、决策时钟与工程治理基线

## 状态：PASS ✅

## 完成项目

### 1. 模块化单体代码骨架
- [x] `pyproject.toml` — 统一依赖锁、类型检查(mypy strict)、格式化(ruff/black)、测试框架(pytest)
- [x] `Makefile` — 一键安装、lint、typecheck、测试、构建、清理
- [x] `.pre-commit-config.yaml` — pre-commit hooks (ruff, bandit, check-yaml, detect-private-key)
- [x] 三个时钟层的应用入口 (safety_executor, strategy_engine, research_lab)

### 2. 三层决策时钟边界
- [x] `beidou_safety` — REALTIME 实时安全与执行层
- [x] `beidou_strategy` — NEARLINE 近线策略层
- [x] `beidou_research` — OFFLINE 离线研究层
- [x] `beidou_shared` — 共享内核（跨层契约和类型）
- [x] 各层包 `__init__.py` 声明职责边界与禁止事项

### 3. 领域包所有权与架构强制测试
- [x] 依赖方向检查 — 禁止 Safety→Strategy、Strategy→Research 等跨层导入
- [x] 共享内核不依赖领域包检查
- [x] 策略层不直连交易所适配器检查
- [x] 研究层不直连生产数据库检查
- [x] 禁止硬编码密钥/TODO/生产默认值扫描

### 4. 性能预算注册表
- [x] `config/performance_budget.yaml` — 12个热路径的 P50/P95/P99 预算
- [x] 包含参考硬件、测量方法、降级规则
- [x] 覆盖所有三层时钟域

### 5. ADR/Schema/Migration/Evidence/Runbook 版本规范
- [x] `docs/adr/index.md` — ADR 索引，含 8 个已接受 ADR
- [x] `beidou_shared/contracts/` — 版本化契约注册中心
- [x] `beidou_shared/serde/` — JSON/MessagePack 序列化
- [x] `evidence/` — 证据目录结构

### 6. 核心类型系统
- [x] 身份类型 (VenueId, AccountId, InstrumentId, CorrelationId 等)
- [x] 结果语义 (SUCCESS/EMPTY/UNKNOWN/ERROR/UNSUPPORTED)
- [x] EventEnvelope — 标准事件封套（correlation_id, causation_id, 幂等键）
- [x] DomainError — 错误分类与 Fail-Closed 语义
- [x] 领域值对象 (MonetaryValue, Quantity, Price, VenueInstrument)
- [x] AlphaGraph — 策略组件 DAG（拓扑排序、环检测）

## 测试结果

```
39 passed in 0.42s
```

- 8 项架构强制测试通过
- 22 项共享内核类型测试通过
- 9 项 AlphaGraph DAG 测试通过

## 文件清单

| 类别 | 文件数 |
|------|--------|
| 根配置 | 5 (pyproject.toml, Makefile, .pre-commit-config.yaml, README.md, config/) |
| beidou_shared | 5 模块 |
| beidou_safety | 5 模块 |
| beidou_strategy | 4 模块 |
| beidou_research | 4 模块 |
| apps | 3 入口 |
| tests | 5 文件 |
| docs/adr | 1 索引 |

## 遗留问题

- P1: `apps/*/__main__.py` 为骨架实现，待后续 PKG 填充
- P2: 缺少 CI/CD pipeline 配置（PKG-31 覆盖）

## 独立审查建议

- 无 P0 问题
- 建议独立审查确认架构边界测试充分性
- 建议在 PKG-31 中补充 CI/CD 自动化
