# 北斗全项目优化审查基线

更新时间：2026-08-09 15:50（本地工作树；安全覆盖尚未部署）

当前 HEAD（最近观测）：`62ed6ef85d4b41b553e5dd380afbda56f047f225`；工作树有 1 个未提交的安全覆盖（交易池/因子不得启动即 ACTIVE）。该 SHA 正被外部 Claude Testnet 进程使用，运行事实不纳入本轮证据。

## 决策

| 层级 | 决策 |
|---|---|
| Project | PIVOT：先收敛事实链与证据语义 |
| Paper/Shadow | HOLD |
| Testnet | HOLD |
| Mainnet | PROHIBITED |
| 24×7 持续盈利 | 不作保证；只有净成本、容量、OOS 和真实窗口证据才可讨论 |

## 当前事实

- 分支 `codex/full-system-convergence-v3`，工作树 DIRTY；外部 `beidou start --startup-timeout 900` 仍在运行（Testnet mock signing-key），其提交与运行时均不属于本轮授权证据。
- 最近本地监督状态为 `ENGINE_STARTING`、`trading_ready=false`、`passed=false`，没有形成 READY 证书。
- 新代码已将 UNKNOWN、未跟踪在途单、无交易所 ACK 的保护、缺失持仓事实、无闭合 bar、无研究 provenance 置为阻断。
- 本轮尚未授权停机、重启、部署、交易所写入、真实资金或 Git 推送。

## 验收原则

1. 任何状态不能由单一来源自证：本地状态、交易所快照、用户流/REST gap-fill、账本必须可比较。
2. UNKNOWN 只能减少风险；不能猜测为 FILLED、CANCELED、ACTIVE、PASS 或盈利。
3. 代码测试通过不等于 G5/G7 通过；旧证书、健康端点和模拟成交不构成实盘证据。

## 交付顺序

`P0 取证/冻结 → 唯一执行事实链 → PIT/研究门 → Paper/Shadow 真实性 → G5/G7 实机窗口 → 生产运维恢复演练`。

## 本轮本地验证快照

| 门 | 结果 |
|---|---|
| 全量回归 | `.venv/bin/pytest -q -W error::ResourceWarning`：1034 passed, 1 skipped（1035 collected） |
| CI 覆盖率 | `.venv/bin/pytest tests/ -q --cov --cov-report=term --cov-fail-under=85`：**FAIL，56.66% < 85%** |
| Ruff lint/format | PASS（276 files） |
| CI 包范围 mypy | PASS |
| compileall | PASS |
| `git diff --check` | PASS |
| 测试质量/禁止模式/硬编码扫描 | PASS |

以上是本地代码证据（E2），不替代真实交易所、Testnet、经过时间的 Paper/Shadow、PostgreSQL/PITR 或 G5/G7 证书；当前总决策仍为 `HOLD / NOT READY FOR UNATTENDED TRADING`。
