# BD-T00 — 冻结基线并修复构建门

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 0
- 前置依赖：无
- 目标：在不改变业务行为的前提下，证明当前基线可编译、可收集测试、可重复执行，并修复阻断 G0 的语法/导入/配置错误。

## 2. 问题证据

- README 声明 PIVOT / Paper HOLD / Testnet HOLD / Mainnet PROHIBITED。
- 当前 HEAD 缺少可见 CI workflow run 与 combined status。
- beidou_core/guard.py 存在疑似缩进回归；提交说明不能代替当前源码执行证据。
- README Python 3.11+ 与 pyproject >=3.12 不一致。

## 3. 实施范围

- 涉及模块：repository root, beidou_core/guard.py, pyproject.toml, Makefile, .github/workflows/ci.yml, tests
- 预计修改文件：beidou_core/guard.py, pyproject.toml, Makefile, .github/workflows/ci.yml, scripts/*, tests/baseline/*, docs/optimization/00_BASELINE.md

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 策略算法、仓位/杠杆参数、风险阈值、交易所业务行为、数据库模型（除修复无法导入所需最小变更）

## 4. 必须实现

1. 从 main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341 创建 refactor/full-system-convergence-v3；若仓库 HEAD 已变化，记录新 SHA 并先做差异审查。
2. 记录 git status、branch、HEAD、最近 20 个提交、Python/pip/OS 信息、文件清单和依赖锁定信息。
3. 将 python -m compileall -q beidou_* apps 与针对关键文件的 py_compile 放在 CI 第一门。
4. 修复 guard.py 等当前语法、缩进、导入错误；每个修复单独提交，不夹带功能改动。
5. 统一 Python 版本声明为 3.12，并让 README、pyproject、CI、Docker/脚本一致。
6. pytest --collect-only 必须成功；完整测试失败时生成失败清单，不得先修改测试。
7. 生成 docs/optimization/00_6b0d95dfa67465be421d9a4f5eaa5a406e7c3341.md 与 artifacts/evidence/baseline/manifest.json。

## 5. 接口与合同

- `不新增业务接口；仅建立 baseline evidence manifest。`

## 6. 数据迁移

无数据库迁移。不得生成生产数据。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
python -m py_compile beidou_core/guard.py
```
```bash
python -m compileall -q beidou_* apps
```
```bash
ruff check beidou_* apps tests scripts
```
```bash
ruff format --check beidou_* apps tests scripts
```
```bash
mypy beidou_* apps --no-error-summary
```
```bash
pytest --collect-only -q
```
```bash
pytest tests -q --maxfail=1
```

## 9. 验收标准

AC-BD-T00-01. 所有生产 Python 文件可编译，compileall 退出码为 0。
AC-BD-T00-02. pytest 收集成功，收集数量写入证据；不得有 collection error。
AC-BD-T00-03. 基线失败被逐项分类为 SOURCE / TEST / ENVIRONMENT / EXTERNAL / NOT_VERIFIABLE。
AC-BD-T00-04. 工作树无未记录改动；基线文件包含 commit、命令和原始证据哈希。
AC-BD-T00-05. G0 仅在 compile、collect、lint、format、typecheck、unit/integration/architecture 基础门全部可复现后标记 PASS。

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
- 生成 `artifacts/evidence/BD-T00/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
