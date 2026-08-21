# 生产验证边界与受控重启证据

本文件记录用户授权的隔离 Paper 重启、运行时观察和安全停机。它不是生产发布、
G5 Testnet 真实执行、Paper promotion 或 Mainnet/live readiness 证据。验证未执行人工下单、
撤单或未知订单清理，也没有启动主服务 LaunchAgent。

## 目标、版本与服务边界

- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 部署工作树：`/Users/maguannan/beidou`
- 启动方式：`python -m apps.strategy_engine`（强制 Paper、前台、`--no-self-heal`）
- 监听地址：`127.0.0.1:19090`
- 验证提交：`1a14e0908bc2376ef4ea585fb3f4f43a366e8d74`

本轮先确认历史 Testnet LaunchAgent 和 `9090` 均未运行，再在合并后的 `main` 工作树以单一
`BTCUSDT` 启动 Paper 进程。进程启动并进入运行监控，稳定观测 `/health=200 HEALTHY`，但
supervisor 状态为 `DEGRADED`、`/ready=503`，安全阻断保持生效。进程随后安全停止，`19090`
已关闭；没有启动 LaunchAgent 或触碰历史 Testnet 实例。

## 重启后的健康与 fail-closed 证据

| 检查 | 新实例观测 |
|---|---|
| `/health` | HTTP 200，`HEALTHY` |
| `/ready` | HTTP 503，`ready=false`，`trading_ready=false` |
| 阻断原因 | `SIGNED_POLICY_UNAVAILABLE`、保护归属 UNKNOWN、reconciliation UNKNOWN、active critical incidents、missing protection coverage |
| 只读信号 | WebSocket market data active with REST fallback；启动自检记录 PASS；`can_write=false`；pending/unacked outbox intents=0 |
| 最终控制状态 | supervisor/lifecycle=`DEGRADED`、`NO_NEW_RISK`；保护放置/清理因缺 fresh matched reconciliation 而跳过；端口关闭 |
| G5 / G-A7 | 仍为 `FAIL/NOT_VERIFIABLE`，本次 Paper 运行不替代真实 Testnet/OOS 证据 |

本次代码修复验证了以下安全性质：active `HIGH/P1` 或 `CRITICAL/LOCKDOWN/P0` incident
存在时，runtime health 产生相应阻断，Testnet auto-reauthorize 不得把系统重新置为可交易；
查询异常也按 fail-closed 处理。

## 测试网副作用与停止

本次隔离 Paper 运行的 `can_write=false`，outbox 无 pending/unacked intent；启动恢复发现
unowned protection/order facts 时保持 UNKNOWN 并跳过清理/放置，未执行人工下单、撤单或未知
订单清理。未将账户凭据、订单标识或其他敏感值写入本报告。

验证结束发送停止信号，确认 Paper 进程退出、`19090` 无监听；历史 LaunchAgent 已处于
不存在状态。没有执行人工订单清理，因此本记录不声称任何外部订单已被撤销或账户状态已被
修改。

## 执行包验证结果

| 验证项 | 结果 |
|---|---|
| unit | `3180 passed` |
| integration | `20 passed` |
| architecture | `263 passed` |
| unit / integration / architecture | `3180 / 20 / 263 passed` |
| 全量 tests | `3532 passed`；coverage gate 通过 |
| 全仓 coverage | `85.02117828263381%`，达到 `fail_under=85`，`GLOBAL-CI=PASS` |
| V3 精确 coverage | `3497/3497` statements、`1070/1070` branches，line/branch `100%/100%` |
| compileall / Ruff / mypy / scans / package validation | 通过 |
| G5 / G-A7 | `FAIL/NOT_VERIFIABLE`；缺少真实 Testnet 运行包和 sealed OOS/Paper 经济窗口 |

## 范围结论

受控重启验证：`PASS_WITH_FAIL_CLOSED_RUNTIME`；运行时发布结论：
`NO-GO_FOR_UNATTENDED_TESTNET_AND_LIVE`。本次证据证明代码门禁和 V3 精确覆盖率通过，并可在
active safety incident 下阻断恢复和安全停机；不证明 G5/G-A7 真实经济接受、Paper promotion
或 Mainnet/live readiness。
