# 生产验证边界与受控重启证据

本文件记录的是用户授权的隔离 worktree Paper/Shadow 重启验证，不是生产发布或
Paper promotion 证据。原始 Testnet PID 98924 保持 untouched。

## 环境补齐与 preflight

首次 Paper preflight 因缺少以下运行目录而阻断：

- `/Users/maguannan/beidou-worktrees/alpha-v3-20260821/.beidou`
- `/Users/maguannan/beidou-worktrees/alpha-v3-20260821/evidence/bootstrap`

已创建这两个工作树内目录并复跑：Python、项目布局、端口、运行目录、全部业务包导入、
Paper 配置、EnvironmentGuard、账户读取配置和 launchd plist 检查均 PASS。没有创建或
伪造 signed policy、账户保护、OOS 或 Paper 经济事实。

## 受控启动命令

```text
/Users/maguannan/ueds/.venv/bin/python -m beidou_launcher.cli start \
  --mode paper --symbols BTCUSDT --port 19090 \
  --startup-timeout 120 --monitor-interval 2 \
  --no-self-heal --max-restarts 0
```

执行两轮，观测到的进程分别为 PID `94053` 和 PID `94208`。两轮均完成启动并提供
`http://127.0.0.1:19090/status`、`/health`、`/ready`。

## 只读健康结果

两轮稳定观测结果一致：

| 信号 | 结果 |
|---|---|
| mode | `paper` |
| liveness | `HEALTHY` |
| lifecycle | `DEGRADED` |
| control action | `NO_NEW_RISK` |
| runtime market data / realtime heartbeat | PASS |
| HTTP server / algorithm probe / write interlock | PASS |
| `/health` | HTTP 200，`HEALTHY` |
| `/ready` | HTTP 503，`ready=false` |
| trading_ready | `false` |
| blockers | protection coverage、reconciliation |

`/ready=503` 是正确的 fail-closed 结果：运行读取到了真实环境中的
`SIGNED_POLICY_UNAVAILABLE`、未归属保护和 reconciliation UNKNOWN；系统没有把这些
事实转成零值或默认状态，也没有启动新的风险。

## 停止与回滚

- 两轮均通过 Ctrl-C 进入安全停止；日志显示 `write=OFF`、`can_write=False`，只取消
  0 个自有 pending order，并留下所有 unowned orders untouched。
- 两轮停止后 `lsof -nP -iTCP:19090 -sTCP:LISTEN` 均为空，状态文件为
  `.beidou/supervisor-state.json`，`phase=STOPPED`、`trading_ready=false`。
- 第二轮进程因安全防抖最终状态 `LOCKED` 退出码为 5；这是已有保护/对账 P0 blocker
  的 fail-closed 停止，不是启动崩溃或测试绕过。

## 范围结论

受控重启验证：`PASS_WITH_FAIL_CLOSED_RUNTIME`。它证明修改后 worktree 能够安全启动、
提供健康/探针接口、在 UNKNOWN/P0 blocker 下不放行风险并可安全停止；不证明 A7 经济
优越性、Paper promotion、Mainnet/live readiness，也不改变原始 Testnet 实例。
