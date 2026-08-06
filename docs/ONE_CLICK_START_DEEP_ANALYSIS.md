# 北斗一键启动模块：深度分析与工程实施说明

## 1. 分析结论

### Interaction Mode

**Yellow**：仓库代码证据足以设计并实现启动监督器，但当前环境无法执行仓库完整依赖、真实 Binance Testnet、GitHub Actions 和长时间运行验证。因此代码交付可以完成，生产/实盘就绪结论不得给出。

### Final Decision

**IMPLEMENT WITH GATED RELEASE**：新增独立启动监督器，不重写 AutonomousEngine；监督器在现有引擎外围建立单实例、前置检查、RESUME 互锁、算法首轮执行验证、持续健康监测、自动降级与证据留存。

## 2. 现状证据与核心缺口

| 编号 | 代码事实 | 风险 |
|---|---|---|
| F-01 | 已有 `apps/autopilot/__main__.py`，但只能通过 `python -m apps.autopilot` 启动 | 使用成本高，缺少统一命令和状态管理 |
| F-02 | CLI 接收 `--port`，但 `AutonomousEngine` 内部固定构造 `HealthServer(port=9090)` | 参数声明与运行事实不一致 |
| F-03 | 引擎 readiness 仅检查生命周期 ACTIVE 与 `feed.is_healthy()` | 不能证明因子、Alpha DAG、风险预算、账户、对账、保护等真实就绪 |
| F-04 | `MarketDataFeed.is_healthy()` 仅依据累计错误数 `<10` | 尚未获得任何真实行情时也可能返回健康 |
| F-05 | 引擎在所有模式启动时均调用签名账户接口 | 非写模式“无需凭据”的抽象与实际启动行为冲突 |
| F-06 | 引擎启动后进行基础检查并自动 `RESUME` | 深度自检未完成前存在风险增加窗口 |
| F-07 | Alpha Graph 与 8 个因子在引擎构造时注册，但现有健康接口不验证其完整性与首轮执行 | “代码存在”可能被误判为“算法已启动” |
| F-08 | 生命周期、健康服务、控制面和 NO_NEW_RISK 已存在 | 可复用，不应再建平行状态机 |

## 3. 第一性原理定义

“一键启动成功”不能定义为进程未退出，也不能定义为 HTTP `/health` 返回 200。对于自动交易系统，启动成功必须同时满足：

1. **环境可识别**：运行模式、配置来源、Git commit、端口、凭据和签名密钥可验证。
2. **系统可构造**：所有必需业务包和核心对象均成功导入与实例化。
3. **算法可执行**：8 个 Alpha 组件完整、拓扑无环、组件校验通过；8 个因子注册并处于当前模式允许的生命周期。
4. **数据真实到达**：至少一个交易标的同时取得真实 ticker 与 orderbook，不能仅依赖“错误数未超限”。
5. **账户事实可用**：账户查询成功，失败不得等同于空账户。
6. **安全保护完整**：Testnet 已有持仓必须具备对应保护对象；UNKNOWN 视为失败。
7. **风险默认关闭**：深度启动门禁通过前，所有增加风险的 Intent 必须被 NO_NEW_RISK 拒绝。
8. **持续可证伪**：实时循环、近线策略循环、错误增量、事故与健康线程必须持续监测；异常必须触发可观测的降级或锁定。
9. **证据可追溯**：每次检查必须留下结构化证据，状态文件写入必须原子化。

## 4. 需求清单

### BD-BOOT-001 统一命令入口（P0）

安装后以下命令必须等价，且无参数时直接启动：

- `beidou`
- `北斗`
- `bd`

同时支持 `doctor`、`status`、`stop`。

### BD-BOOT-002 单实例保护（P0）

- 使用 PID 文件原子创建。
- 活跃 PID 存在时拒绝第二实例。
- 陈旧 PID 自动清理。
- PID 文件权限为仅当前用户可读写。

### BD-BOOT-003 启动前置检查（P0）

必须检查：Python 3.12、项目结构、健康端口、证据目录、19 个业务包、统一配置、EnvironmentGuard、账户读取凭据；Testnet 额外检查 `BEIDOU_SIGNING_KEY`。

### BD-BOOT-004 RESUME 互锁（P0）

- 引擎深度启动检查完成前拦截其内部 `RESUME`。
- 拦截期间保持 `NO_NEW_RISK`，退出、撤单、查询路径不受阻。
- 只有监督器授权后，`RESUME` 才能真正执行。

### BD-BOOT-005 模块与算法完整性（P0）

必须验证：

- 29 个核心引擎对象均已构造；
- 8 个 Alpha 组件 ID 完整；
- 每个组件 `validate() == True`；
- DAG 可拓扑排序；
- 8 个因子完整且生命周期满足当前模式；
- 交易池至少一个 ACTIVE 标的；
- 策略风险预算存在；
- 启动时强制执行首轮 REALTIME；
- 使用真实公共行情执行无订单副作用的 Alpha DAG 只读探针；
- 生成 StrategyKernel proposal hash，证明算法链已真实执行。

### BD-BOOT-006 运行时健康监督（P0）

持续检查：

- 生命周期状态；
- 行情 ticker+orderbook 实际观测；
- 健康 HTTP 线程；
- REALTIME 心跳不超过 30 秒；
- NEARLINE 心跳不超过 420 秒；
- 错误增量与累计错误；
- 账户事实快照；
- 活动事故；
- Testnet 持仓保护覆盖。

### BD-BOOT-007 自动降级与锁定（P0）

- 任一 P0/P1 阻断：立即执行 `NO_NEW_RISK` 并将 ACTIVE 降级为 DEGRADED。
- 连续 3 个监测周期仍存在阻断：进入 LOCKED，停止引擎。
- 不自动重启到 ACTIVE。

### BD-BOOT-008 证据与状态（P1）

- 当前状态：`.beidou/supervisor-state.json`，临时文件后原子替换。
- 历史证据：`evidence/bootstrap/supervisor-history.jsonl`，只追加。
- 每条检查包含 ID、状态、严重度、消息、证据、耗时和 UTC 时间。

### BD-BOOT-009 安全停止（P1）

`beidou stop` 读取 PID 并发送 SIGTERM，由现有引擎执行优雅关闭流程。

### BD-BOOT-010 安全边界（P0）

- 不新增 Mainnet、Canary 或 Live 模式。
- 不绕过 EnvironmentGuard。
- 不将账户查询失败转换为空账户。
- 不以 HTTP 200、进程存活或对象存在作为算法已启动的唯一证据。

## 5. 目标架构

```text
beidou / 北斗 / bd
        │
        ▼
┌──────────────────────────────┐
│ One-Click Startup Supervisor │
├──────────────────────────────┤
│ Instance Lock                │
│ Preflight + EnvironmentGuard │
│ Module/Algorithm Registry    │
│ RESUME Interlock             │
│ Startup Evidence Gate        │
│ Runtime Health Monitor       │
│ Fail-Closed Controller       │
│ Evidence Writer              │
└──────────────┬───────────────┘
               │ authorized only after PASS
               ▼
       Existing AutonomousEngine
```

## 6. 异常流程

| 场景 | 处理 |
|---|---|
| 端口占用 | 启动前阻断，退出码 2 |
| 已有实例 | 拒绝第二实例，退出码 3 |
| 包导入失败 | 启动前阻断，列出具体包和异常 |
| 配置未知或 Mainnet URL | EnvironmentGuard 阻断 |
| 缺少账户凭据 | 在引擎启动前阻断，避免晚失败 |
| Testnet 缺签名密钥 | P0 阻断 |
| 引擎提前退出 | LOCKED，退出码 4 |
| 启动超时 | NO_NEW_RISK + LOCKED，退出码 4 |
| 行情错误数未超限但无真实数据 | 判定失败，不接受假健康 |
| Alpha 节点缺失或拓扑异常 | P0 阻断 |
| 近线循环未执行 | P0 阻断，不放行 RESUME |
| Testnet 存在未保护持仓 | P0 阻断 |
| 运行中单次异常 | NO_NEW_RISK + DEGRADED |
| 连续 3 次阻断 | LOCKED 并停止 |

## 7. 验收标准

| AC | 验收条件 | 证据 |
|---|---|---|
| AC-01 | `pip install -e .` 后三个命令均可解析 | 安装日志、`which`、命令输出 |
| AC-02 | 无参数执行等价于 start | CLI 测试 |
| AC-03 | 第二实例被拒绝 | PID 锁测试 |
| AC-04 | 端口占用时引擎未构造 | 集成测试与日志 |
| AC-05 | 19 个业务包逐项导入，缺一即阻断 | doctor JSON 输出 |
| AC-06 | 深度检查完成前控制面始终 NO_NEW_RISK | 控制状态变更日志 |
| AC-07 | 8 个 Alpha 组件、8 个因子完整，真实行情只读探针生成 proposal hash | supervisor evidence |
| AC-08 | 未取得 ticker+orderbook 时不得 trading-ready | 故障注入测试 |
| AC-09 | Testnet 未保护持仓阻断 RESUME | Testnet 集成测试 |
| AC-10 | 连续 3 次 P0 异常后 LOCKED 并停止 | 故障注入日志 |
| AC-11 | 状态文件原子写入，历史文件只追加 | 文件系统测试 |
| AC-12 | `status` 和 `stop` 可用 | CLI 集成测试 |
| AC-13 | 原有 `python -m apps.autopilot` 不被删除 | 回归检查 |
| AC-14 | Mainnet/Live 仍被阻断 | EnvironmentGuard 测试 |

## 8. 测试要求

1. **单位测试**：模型阻断语义、注册表完整性、陈旧 PID 清理、算法完整/缺失判定。
2. **集成测试**：CLI 安装入口、端口占用、配置缺失、EnvironmentGuard、状态原子写入。
3. **引擎集成测试**：Fake-free 的本地 Paper/Testnet 环境，验证首轮 tick、proposal hash 和 RESUME 互锁。
4. **Testnet E2E**：真实 Binance Testnet 凭据，验证账户、持仓、保护覆盖、停止与重启恢复。
5. **故障注入**：断网、行情空响应、账户 401/429/5xx、健康线程退出、nearline 卡死、保护恢复失败。
6. **长时间验证**：至少 24 小时监督器运行证据；无人值守声明需更长独立认证，不由本模块自动授予。

## 9. 当前可验证范围

已完成：模块源代码、语法编译、纯本地单元测试。

未完成且不得伪称完成：仓库全量 pytest、ruff、mypy、可编辑安装、GitHub Actions、真实 Testnet E2E、24 小时运行、Mainnet 或盈利能力验证。
