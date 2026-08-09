# 北斗全项目优化审查基线

更新时间：2026-08-09（本地工作树，代码未提交/未部署）

当前 HEAD：`8523ac9053c91018609816e262cc74ce3d7b37e6`；当前工作树包含未提交的本轮收敛切片。

## 决策

| 层级 | 决策 |
|---|---|
| Project | PIVOT：先收敛事实链与证据语义 |
| Paper/Shadow | HOLD |
| Testnet | HOLD |
| Mainnet | PROHIBITED |
| 24×7 持续盈利 | 不作保证；只有净成本、容量、OOS 和真实窗口证据才可讨论 |

## 当前事实

- 分支 `codex/full-system-convergence-v3`，工作树 DIRTY；当前运行 PID 仍是旧 SHA，不能把 HEAD 或工作树视为已在线。
- 2026-08-09T06:39:01Z 只读运行采样仍显示旧 SHA；监督器 `trading_ready=false/passed=false`、实时心跳约 229s 陈旧、BNBUSDT 保护覆盖 P0 `MISSING_SL/MISSING_TP`，但 `/health=HEALTHY`、`/ready=true`/`ACTIVE`/`RESUME`，`/trading-ready=503`。这是证书语义 P0，而不是优化空间。
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
| 全量回归 | `.venv/bin/pytest -q`：1043 passed, 1 skipped（1044 collected） |
| CI 覆盖率 | `.venv/bin/pytest tests/ -q --cov --cov-report=term-missing`：**FAIL，57.04% < 85%**；并有 ResourceWarning |
| Ruff lint/format | PASS（274 files） |
| CI 包范围 mypy | PASS |
| compileall | PASS |
| `git diff --check` | PASS |
| 测试质量/禁止模式扫描 | PASS；硬编码扫描 0 阻断、10 条非阻断告警 |

以上是本地代码证据（E2），不替代真实交易所、Testnet、经过时间的 Paper/Shadow、PostgreSQL/PITR 或 G5/G7 证书；当前总决策仍为 `HOLD / NOT READY FOR UNATTENDED TRADING`。
