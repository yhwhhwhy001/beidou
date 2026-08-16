# 北斗逐模块深度优化 — 阶段 0 上下文与证据基础

- 运行 ID：`2026-08-16-deep-module-optimization`
- 日期：2026-08-16
- 仓库：`/Users/maguannan/beidou`，分支 `main`
- 扫描起始时 HEAD：`d91035aac84f7219013010496e7d360b85b8ea7a`（工作树含 4 个未提交文件：订单终态单调守卫 BD-FIX）
- **当前 HEAD（扫描结束复核）：`fe62da152affd9e9f0be5e11cdcbee390e63b2fd`** —— 单调守卫修复已于 18:58 左右被提交（引擎 18:59:18 启动时 preflight 记录的就是该提交；运行中引擎已加载该修复）
- 工作树：干净（仅本 run 的未跟踪文档目录）
- 遗留 worktree：`/Users/maguannan/beidou-worktrees/full-system-optimization-20260815`（9e5279d，codex/full-system-optimization，上轮遗留，未触碰）
- 并发注意：系统中存在其他 claude/codex 进程；本 run 修改集中在明确文件范围，提交前 diff 复核
- Python：3.14.6（CI 声明 3.12，漂移见 M21）

## 授权边界（本阶段实际执行范围）

- 只读侦察：已执行（8 个并行 Explore 代理 + 直接文件读取 + 活体进程/日志/PG 只读观察）
- 代码修改：本阶段 0 处
- 交易所写/订单/持仓操作：0 处
- 进程重启/部署：0 处
- 后续模块执行沿用 CIOS 安全契约：读优先、fail-closed、未明确授权不动订单/杠杆/资金

## 活体运行态证据（2026-08-16 18:57–19:05 CST 观察）

| 项 | 证据 |
|---|---|
| 引擎进程 | PID 77694（PPID=1，launchd `com.beidou.autopilot`），18:59:18 CST 启动，cwd=/Users/maguannan/beidou；旧实例 PID 34149（13:59 启动）已退出，**无当前双实例** |
| 启动 commit | supervisor-state.json 记录 `fe62da152`（启动时该提交存在，后被 reset；运行中进程已含单调守卫代码） |
| 健康 | `/health` HEALTHY（版本 2.0.0，9090 端口由 77694 监听） |
| 对账 | `[recon] MATCHED: independent durable facts verified`（反复出现） |
| 阻断 | 15 条重复 P0 blocker `runtime.safety.protection_coverage: MISSING_SL, MISSING_TP`（SLERF/ETH/TUTU/USU/ATOM/SOPH/XRP/STEEM/BANK/EPIC/CATI/TRADOOR/SCRT/DOGE/MOVE 等）→ supervisor DEGRADED、trading_ready=false |
| 事故 | 18:59:22 CRITICAL "Protection ownership unknown"；18:59:23 CRITICAL "Execution fact persistence blocked"；18:59:30 HIGH "Supervisor DEGRADED" |
| 意图 | outbox unacked=14 pending=14 processed=0（资格门拦截，fail-closed 生效） |
| 矛盾 | `[nearline-diag] retry: symbols=15 positions=0`（保护重试侧看不到持仓）vs 覆盖检查侧 15 持仓 —— 保护事实源两侧不一致，待 M12/M13 定位 |
| 告警 | 每监督周期重复发送 HIGH 告警（同一 check_id 15 条不合并）→ 告警风暴 |
| .env | `BEIDOU_DEV_FAST_START=1` 已设置（其门禁语义待 M00 核实） |

## 扫描证据来源

- 8 个并行 Explore 侦察报告（beidou_data / beidou_research / beidou_strategy / beidou_safety+beidou_exchange / lifecycle+autonomy+observability+control+certification+production / infra+security+delivery+CI+deploy / launcher+apps+core 主链 / 全库危险模式机械扫描）
- 直接读取：00_EXECUTION_MASTER.md、01_AGENT_OPERATING_PROTOCOL.md、README、delivery.yaml、docs/optimization/（00_BASELINE、16_REMAINING_GAPS、runs/2026-08-15 终审）、config/policies/*.json、.beidou/supervisor-state.json、.beidou/domain-trading-readiness.md、git reflog、launchd 日志、进程表
- 基线命令：`ruff check .`（38 errors）、`ruff format --check .`（23 files）、全量 pytest（后台运行中，结果见 04-test-baseline.md 补记）
- 原则：侦察报告结论为「线索」，各模块正式修复前须在模块内复核（file:line 已验证的除外）
