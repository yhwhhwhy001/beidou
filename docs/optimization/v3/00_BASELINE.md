# 北斗 V3 当前执行基线

## 决策

```text
Project: Beidou
Decision: PIVOT
Paper: HOLD
Shadow: HOLD
Testnet: HOLD
Mainnet: PROHIBITED (本阶段不定义实盘授权)
Code baseline: 52cd5558fe0336e1553df5bb63f9dadcb1f0cecc
Execution branch: codex/full-system-convergence-v3
Captured at: 2026-08-09T02:15:41Z
```

本文件只记录 V3 执行基线，不把旧版本的 PASS 声明升级为当前证据。旧 G5/G7 文件保留为调查材料，当前不具备认证效力。

## 当前证据

- 工作树在分支创建前干净；当前宿主为 Apple Silicon macOS 26.6。
- `.venv` 与系统运行时为 Python 3.14.6；项目声明与执行书要求固定 Python 3.12，尚未完成工具链收敛。
- `python -m compileall -q beidou_* apps`：PASS。
- `pytest --collect-only -q`：PASS，963 tests collected（包含本次新增 9 项回归测试）。
- Ruff：FAIL，当前基线仍有大量既有问题（执行前基线 216 errors）。
- mypy：FAIL；当前命令会尝试解析未跟踪的 SQLite 二进制文件，关键模块仍存在类型豁免。
- 审查时 LaunchAgent 仍运行 `beidou start`，存在 KeepAlive/重启风暴风险；本执行轮未停止、重启或操作交易所。
- 审查时 supervisor 报告 `trading_ready=false`、实时心跳 P0、订单链 P0；该本地状态不能代替交易所事实快照。

## 本轮已落地

1. 删除引擎内置 Testnet 签名密钥回退；缺少 `BEIDOU_SIGNING_KEY` 时不生成风险批准。
2. Testnet 预检将签名密钥缺失从 WARN 提升为 P0 FAIL。
3. Exchange write interlock 同时检查 `_can_write`、监督器 authority、无 blocker 和 `RESUME`。
4. P0/P1 运行事实不再归类为可绕过的 transient；运行失败撤销自动恢复授权。
5. `/ready` 与监督器实际交易就绪状态绑定；进程存活由 `/health` 独立表示。
6. G7 tracker 要求真实经过时间、最小样本、非零保护样本，并在 P0 时使窗口失效。
7. 安全默认配置与健康 HTTP 默认绑定 `127.0.0.1`；Shadow 明确为零写。

## 未完成与硬阻塞

- PostgreSQL 事务 Outbox 尚未接入主运行路径；当前引擎仍保留 SQLite/内存事实源。
- ExchangeAdapter 尚未成为唯一网络写边界；完整订单查询、撤单、Algo 保护和用户流未收敛。
- G5 真实异常矩阵与 G7 真实 30 日窗口尚未重新开始。
- Alpha 数据因果、PIT、Purged WFO/CPCV、DSR/PBO/FDR 和成本容量证据不存在。
- 当前 Testnet 实例尚未按“先取证、后安全停机”处置；下一步不得直接执行旧 `beidou stop`。

## Gate

```text
G0 Context: PASS_WITH_CONDITIONS
G1 Requirements: APPROVED
G2 Architecture: APPROVED_WITH_CONDITIONS
G3 Tasks: READY
G4 Development: IN_PROGRESS
G5 Debugging: UNRESOLVED
G6 Review: FAIL (历史红队反证仍未关闭)
G7 System verification: IN_PROGRESS
G8 Acceptance: BLOCKED
G9 Release: BLOCKED
G10 Production: NOT_AUTHORIZED
```
