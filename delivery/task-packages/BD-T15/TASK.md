# BD-T15 — CI 与证据系统加固

## 1. 基本信息

- 优先级：**P0**
- 阶段：Cross-cutting
- 前置依赖：BD-T00
- 目标：让所有门禁由 CI 真实强制，并将每次任务证据绑定 commit/config/policy/command 哈希。

## 2. 问题证据

- 核心交易模块被 mypy ignore_errors 豁免。
- 关键包覆盖率配置不会自动强制。
- CI 未执行 format、branch coverage、migration/recovery/chaos/Testnet。

## 3. 实施范围

- 涉及模块：CI, scripts, tests, evidence
- 预计修改文件：.github/workflows/*.yml, pyproject.toml, Makefile, scripts/*, tests/*, artifacts/evidence/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得为绿色结果扩大 ignore/allowlist；不得把生成证据提交为通过结论而不含原始输出。

## 4. 必须实现

1. CI 分 T0/T1/T2/T3/T4/T5；T4/T5 需要受保护环境和手工批准，不自动 Mainnet。
2. T0 强制 compile、ruff check/format、mypy strict、secret、hardcoded、architecture、dependency、SBOM、migration lint。
3. 核心交易链移除 mypy ignore_errors；新增豁免必须带 issue、owner、expiry 且 P0 包禁止豁免。
4. global line>=85%；critical line>=95%、branch>=90%；纯逻辑 mutation>=75%。
5. 扫描器遇 SyntaxError/读取失败必须退出非零；测试质量扫描真实检查 mock production path、无断言、永真断言、skip。
6. 证据 manifest 记录 commit、branch、config hash、policy、command、exit code、时间、stdout/stderr/artifact hash、agent。
7. 每个任务独立 commit 与 evidence bundle；package validator 作为 CI 门。

## 5. 接口与合同

- `EvidenceCollector.run(command)->EvidenceRecord`
- `GateRunner.evaluate(gate)->GateResult`

## 6. 数据迁移

无业务迁移；证据文件存对象存储或 artifact，不作为应用事实库。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
python delivery/scripts/validate_package.py
```
```bash
make verify
```
```bash
pytest tests -q --cov --cov-branch
```
```bash
python scripts/scan_hardcoded.py beidou_* apps
```
```bash
python scripts/scan_test_quality.py tests
```

## 9. 验收标准

AC-BD-T15-01. 核心链无 mypy ignore_errors。
AC-BD-T15-02. 覆盖率阈值由 CI 失败机制验证，而非仅配置。
AC-BD-T15-03. 故意引入 SyntaxError、默认密钥、空断言时相应扫描器必定失败。
AC-BD-T15-04. 所有 Gate 结果可由 evidence manifest 重放核验。

验收状态仅允许：`PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE`。

## 10. 交付证据

- 修改文件清单与 Git diff --stat
- 执行命令、退出码、开始/结束时间、stdout/stderr 原文及 SHA-256
- 测试报告与覆盖率报告
- 数据库/API/运行证据（如适用）
- 本任务验收矩阵逐项结果
- 遗留问题、风险与回滚命令
- commit SHA、branch、config hash、policy version、agent ID

## 11. 完成后动作

- 更新 `docs/optimization/06_ACCEPTANCE_MATRIX.md`；
- 生成 `artifacts/evidence/BD-T15/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
