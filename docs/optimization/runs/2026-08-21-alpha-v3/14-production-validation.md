# 生产验证边界与受控重启证据

本文件记录用户授权的主服务 Testnet 重启、运行时观察和安全停机。它不是生产发布、
Paper promotion 或 Mainnet/live readiness 证据。验证未执行人工下单、撤单或未知订单清理，
也没有触碰 Mainnet/live。

## 目标、版本与服务边界

- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 部署工作树：`/Users/maguannan/beidou`
- LaunchAgent：`gui/501/com.beidou.autopilot`
- 监听地址：`127.0.0.1:9090`
- 验证提交：`77fbbc3cdf6ca8068063343c053749d9e156be61`

本轮先受控停止旧实例（历史 PID `98924`），确认进程和 `9090` 监听关闭，再将 main
工作树更新到上述提交并推送。随后通过 LaunchAgent 启动新实例；启动观测到 wrapper PID
`11005`、Python PID `11009`，`/status` 返回的 supervisor commit 与目标提交完全一致。
PID 仅是本轮历史观测值，不能作为当前运行状态；验证结束后服务已停止。

## 重启后的健康与 fail-closed 证据

| 检查 | 新实例观测 |
|---|---|
| `/health` | HTTP 200，`HEALTHY` |
| 初始 `/ready` | HTTP 503，`ready=false`，`trading_ready=false` |
| 初始阻断原因 | `runtime.health.incidents`；active critical safety incident 存在 |
| 事件短暂清除后 | `/ready` 曾短暂返回 200，随后再次出现 active incidents |
| 最终控制状态 | `lifecycle=DEGRADED`，`control_action=NO_NEW_RISK`，`/ready=503` |
| 最终 active incidents | 2；系统继续阻断新风险 |
| 最终其他信号 | reconciliation `MATCHED`；事件流、用户流和实时数据检查可用，但 G5 仍为 FAIL，告警/生命周期等仍有 WARN |

本次代码修复验证了以下安全性质：active `HIGH/P1` 或 `CRITICAL/LOCKDOWN/P0` incident
存在时，runtime health 产生相应阻断，Testnet auto-reauthorize 不得把系统重新置为可交易；
查询异常也按 fail-closed 处理。

## 测试网副作用与停止

在 incident 暂时清除的窗口内，服务自动创建过 1 条 `OrderIntent` 并产生 1 次放置请求；
这不是本次人工执行的订单。随后新的 active incidents 出现，后续观测到的进入请求被
`NO_NEW_RISK` 拒绝，观察窗口内没有再出现新的进入写入。未打印或记录账户凭据、订单标识
或其他敏感值。

验证结束执行 LaunchAgent `bootout`，并确认 wrapper/Python 进程不存在、`9090` 无监听、
`launchctl print gui/501/com.beidou.autopilot` 显示服务不存在。没有执行人工订单清理，因此
本记录不声称任何外部订单已被撤销或账户状态已被修改。

## 执行包验证结果

| 验证项 | 结果 |
|---|---|
| unit | `3051 passed` |
| integration | `20 passed` |
| architecture | `263 passed` |
| 全量 tests | `3403 passed`；命令因 coverage gate 失败退出 |
| 全仓 coverage | `80.57%`，低于 `fail_under=85`，因此 `GLOBAL-CI=FAIL_COVERAGE` |
| V3 核心 line evidence | 执行包列出的核心实现文件在全量 line report 中为 100% line |
| broad alpha+portfolio branch run | `86.44%`，包含 compatibility/额外模块，不能替代核心 branch gate 报告 |
| compileall / Ruff / mypy / scans / package validation | 通过 |
| G5 / G-A7 | `FAIL/NOT_VERIFIABLE`；缺少独立可复核的签名策略与真实经济窗口证据 |

## 范围结论

受控重启验证：`PASS_WITH_FAIL_CLOSED_RUNTIME`；运行时发布结论：
`NO-GO_FOR_UNATTENDED_TESTNET_AND_LIVE`。本次证据证明新提交可启动、可观测、在 active
safety incident 下阻断恢复并可安全停机；不证明全仓 coverage 门禁、V3 全部 line/branch
100%、A7 经济优越性、Paper promotion 或 Mainnet/live readiness。
