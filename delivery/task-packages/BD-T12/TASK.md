# BD-T12 — 真正的复式账本

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T10
- 目标：将单条 debit/credit 伪平衡模型替换为 transaction + postings 的不可变复式账本。

## 2. 问题证据

- JournalEntry 要求单条 debit==credit，无法表达多科目经济事件。
- 余额由 debit-credit 累加，不是完整会计模型。

## 3. 实施范围

- 涉及模块：beidou_safety/ledger, beidou_reporting, PostgreSQL
- 预计修改文件：beidou_safety/execution/ledger.py, beidou_safety/ledger/*, migrations/*ledger*, tests/ledger/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得 UPDATE/DELETE 已过账交易；不得用交易所账户值覆盖账本余额。

## 4. 必须实现

1. 定义 LedgerTransaction 与 Posting；同一交易至少两条 posting，按 currency 求和为 0。
2. 建立现金、保证金、持仓成本、已实现 PnL、未实现 PnL、手续费、funding、应收应付、调整科目。
3. Fill、fee、funding、transfer、adjustment 使用不同 transaction type 与幂等 source_event_id。
4. 账本 append-only；更正使用 reversal + replacement，不物理修改。
5. 投影可从 postings 重建；使用 Decimal 与币种精度，不用 float。
6. 每日 trial balance、账户余额和交易归因报告可导出。

## 5. 接口与合同

- `Ledger.post(transaction)->PostResult`
- `Ledger.reverse(transaction_id, reason)->Transaction`
- `LedgerProjection.rebuild(as_of)->Balances`

## 6. 数据迁移

新增 ledger_transactions、ledger_postings、account_chart、ledger_projection；旧记录标记 LEGACY_UNVERIFIED。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/ledger -q
```
```bash
pytest -q -k 'ledger and (balanced or reversal or duplicate_fill or funding or replay)'
```

## 9. 验收标准

AC-BD-T12-01. 每个 transaction 的 postings 按币种和方向求和为 0。
AC-BD-T12-02. 重复 fill/source_event 不重复记账。
AC-BD-T12-03. 任意重放得到相同余额、PnL、fee、funding。
AC-BD-T12-04. 无 UPDATE/DELETE 已过账交易的生产 API。

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
- 生成 `artifacts/evidence/BD-T12/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
