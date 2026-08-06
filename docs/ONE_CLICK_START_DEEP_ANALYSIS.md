# 北斗一键启动模块：深度分析与工程实施说明

## 1. 结论

| 项目 | 结论 |
|---|---|
| Interaction Mode | **Yellow**：代码证据足够实施，缺少完整 CI、真实 Testnet、故障注入和持续运行证据 |
| 实施决策 | **IMPLEMENT WITH GATED RELEASE** |
| 权威模块 | **仅保留 `beidou_launcher`** |
| Mainnet / Live | 不新增，继续阻断 |
| 无人值守认证 | 未授予 |

主分支已存在一个基于“子进程 + HTTP 双快照”的一键启动器。独立审查后发现它不能在深度验证前阻止子进程自动 `RESUME`，也不能拦截零写模式启动恢复过程中的交易所写请求。因此本次不再增加第二套启动包，而是把 `beidou_launcher` 重构为唯一的进程内 fail-closed 监督器。

## 2. 关键发现

| 编号 | 代码事实 | 风险 | 处理 |
|---|---|---|---|
| F-01 | 原启动器仅采集 `/ready`、`/status`、`/metrics` | 健康端点无法证明账户、对账和保护真实有效 | 增加独立事实探针 |
| F-02 | 引擎启动后会自动 `RESUME` | 深度检查前存在风险增加窗口 | 安装 RESUME 互锁 |
| F-03 | Paper/Shadow 启动恢复仍可能调用 Algo Order DELETE/POST | 零写模式可能意外写交易所 | 在引擎 API 边界拦截变更方法 |
| F-04 | `feed.is_healthy()` 仅依据错误计数 | 无行情也可能被判健康 | 必须实际观测 Ticker + Orderbook |
| F-05 | Alpha/因子对象存在不等于算法已执行 | 产生假启动证据 | 真实公共行情只读 DAG 探针 |
| F-06 | 本地 ProtectionManager 不是交易所事实 | 保护单提交失败仍可能显示已保护 | 查询当前 `openAlgoOrders` 并精确核对数量 |
| F-07 | `_last_recon` 时间不代表对账成功 | MISMATCHED 仍可能有心跳 | 直接要求 ReconciliationResult=`MATCHED` |
| F-08 | 仅按 PID 停止存在 PID 复用风险 | 误杀无关进程 | 校验 PID、状态新鲜度和命令身份 |
| F-09 | 残留 RUNNING 状态可能被误读 | 展示陈旧健康 | status 计算进程存活和证据年龄 |

## 3. 放行条件

只有以下条件同时成立，监督器才允许 `RESUME`：

1. Git commit 可解析；Testnet 工作区干净。
2. Python、项目结构、端口、目录、配置和 19 个业务包通过。
3. EnvironmentGuard、账户凭据和 Testnet 签名密钥通过。
4. 29 个核心对象全部构造。
5. Alpha DAG 恰好包含预期 8 个节点，无缺失、无多余、拓扑有效。
6. 8 个因子完整并处于可运行生命周期。
7. 交易池和风险预算有效。
8. 真实行情只读探针执行 8 节点并生成 proposal hash。
9. 独立账户快照新鲜且 Schema 完整。
10. 对账状态为 `MATCHED`，双方事实不超过 120 秒。
11. Testnet 每个非零持仓均有当前服务器保护单，数量与预期完全相等。
12. 无 P0 事故，行情和实时/近线心跳正常。

## 4. 目标结构

```text
beidou_launcher/
├── cli.py          # beidou / 北斗 / bd；doctor/status/stop
├── preflight.py    # 环境、Git、配置、凭据、端口和包导入
├── registry.py     # 核心对象、Alpha、因子、交易池和风险预算
├── runtime.py      # 行情、账户、对账、保护、心跳和事故
├── supervisor.py   # 写互锁、RESUME 互锁、降级、恢复和锁定
├── state.py        # PID 锁、原子状态、历史证据和安全停止
├── models.py       # 检查和报告类型
├── checks.py       # 旧接口兼容 facade
└── manifest.py     # 权威常量
```

## 5. 状态机

```text
STARTING
→ PREFLIGHT
→ CONSTRUCTION_VALIDATION
→ STARTUP_VALIDATION
→ RUNNING

RUNNING + blocker
→ NO_NEW_RISK
→ DEGRADED
→ RECOVERING
→ VALIDATING
→ RUNNING

连续 3 个周期 blocker
→ LOCKED
→ STOPPED
```

人工 `NO_NEW_RISK` / `EXIT_ONLY` 被识别为 PAUSED，不自动恢复；只有监督器自身触发的暂停，在所有证据重新通过后才允许恢复。

## 6. 验收标准

| AC | 条件 | 优先级 |
|---|---|---:|
| AC-01 | `beidou`、`北斗`、`bd` 等价 | P0 |
| AC-02 | 第二实例阻断，陈旧 PID 清理 | P0 |
| AC-03 | Testnet 脏工作区、端口占用、配置或凭据失败时不启动 | P0 |
| AC-04 | 非 Testnet 交易所变更请求被拦截 | P0 |
| AC-05 | 深度检查前始终 `NO_NEW_RISK` | P0 |
| AC-06 | 29 个对象、8 Alpha、8 因子、交易池和风险预算完整 | P0 |
| AC-07 | 真实行情只读 DAG 探针生成 proposal hash | P0 |
| AC-08 | 独立账户快照失败或过期时阻断 | P0 |
| AC-09 | 对账非 `MATCHED` 时阻断 | P0 |
| AC-10 | Testnet 保护单不足或重复时阻断 | P0 |
| AC-11 | HTTP readiness 与监督器证据一致 | P0 |
| AC-12 | 单周期降级、连续三周期锁定 | P0 |
| AC-13 | 人工暂停不被擅自 RESUME | P0 |
| AC-14 | 状态原子写、历史只追加 | P1 |
| AC-15 | stop 校验 PID、证据和进程身份 | P0 |
| AC-16 | Mainnet/Canary/Live 继续阻断 | P0 |

## 7. 已执行专项验证

- Python 语法编译通过。
- 9 项专项测试通过。
- `python -m beidou_launcher --help` 通过。
- TOML 命令别名、AST 和 120 字符行宽检查通过。
- 回归覆盖：PID 锁、阻断语义、三个别名、安全停止、零写互锁、保护精确覆盖、对账不匹配阻断。

## 8. 未完成门禁

Draft PR 在以下证据完成前不得转为 Ready：

1. 仓库全量 `pytest`。
2. `ruff check` 与 `mypy --strict`。
3. 具备 Hatchling 的环境执行 `pip install -e ".[dev]"` 并验证真实 console scripts。
4. GitHub Actions 全部通过。
5. 真实 Binance Testnet E2E。
6. 断网、401、429、5xx、行情空响应、nearline 卡死、保护恢复失败等故障注入。
7. 至少 24 小时持续运行。

当前不得宣称 Testnet-ready、无人值守、生产就绪、Mainnet 就绪或具备盈利能力。
| Interaction Mode | **Yellow**：代码证据足够实施，但缺少完整 CI、真实 Testnet、故障注入和持续运行证据 |
| 实施决策 | **IMPLEMENT WITH GATED RELEASE** |
| 主分支处理 | 不直接修改；通过独立功能分支和 Draft PR 交付 |
| Mainnet / Live | 不新增，继续阻断 |
| 无人值守认证 | 未授予 |

采用外围监督器而不是重写 `AutonomousEngine`：复用现有生命周期、控制面、健康服务和 EnvironmentGuard，在其外部增加单实例、前置门禁、零写互锁、RESUME 互锁、真实算法探针、事实核验、持续健康监督和证据留存。

## 2. 代码级发现

| 编号 | 代码事实 | 风险 | 处理 |
|---|---|---|---|
| F-01 | 现有入口需执行 `python -m apps.autopilot` | 操作复杂、无统一状态管理 | 增加 `beidou`、`北斗`、`bd` |
| F-02 | CLI 有 `--port`，引擎却固定构造 9090 | 参数与运行事实不一致 | 监督器在健康服务启动前注入端口 |
| F-03 | 原 readiness 只看生命周期和 `feed.is_healthy()` | 算法、账户、对账、保护可能未就绪 | 增加独立综合门禁并接管 HTTP callbacks |
| F-04 | `feed.is_healthy()` 只看错误计数 | 未取得行情也可能健康 | 必须实际观测 Ticker + Orderbook |
| F-05 | 引擎所有模式都读取签名账户 | 非写模式仍可能晚失败 | 前置检查按实际行为要求凭据 |
| F-06 | 引擎基础启动后自动 `RESUME` | 深度检查前存在放行窗口 | 安装 RESUME 互锁 |
| F-07 | Paper/Shadow 启动恢复代码仍可能执行 Algo Order DELETE/POST | 零写模式存在意外写路径 | 在引擎 API 边界拦截全部变更方法 |
| F-08 | Alpha Graph 和因子注册不等于算法已运行 | 可能产生“对象存在即健康”的假阳性 | 用真实公共行情执行只读 DAG 探针 |
| F-09 | 本地 ProtectionManager 对象可能存在，但交易所下单失败 | 未保护持仓被误报为安全 | 查询当前 `openAlgoOrders` 并精确核对预期数量 |
| F-10 | `_last_recon` 更新时间不能证明对账成功 | 对账失败也可能看似有心跳 | 直接要求 ReconciliationResult=`MATCHED` 且事实新鲜 |
| F-11 | 单凭 PID 执行 stop 可能遇到 PID 复用 | 误杀无关进程 | 验证 PID、状态证据、新鲜度和命令身份 |
| F-12 | 状态文件可能残留 RUNNING | 展示陈旧健康 | `status` 增加进程存活与证据新鲜度计算 |

## 3. 一键启动成功的定义

只有以下条件同时成立，监督器才允许控制面进入 `RESUME`：

1. 当前 Git commit 可解析；Testnet 工作区必须干净。
2. Python、项目结构、端口、目录、配置和全部 19 个业务包通过检查。
3. EnvironmentGuard 通过，凭据与 Testnet 签名密钥满足要求。
4. 29 个核心对象均已构造。
5. Alpha DAG 恰好包含预期的 8 个组件，无缺失、无多余、组件校验通过且拓扑完整。
6. 8 个因子恰好完整，并处于可运行生命周期。
7. 交易池至少一个 ACTIVE 标的，策略风险预算关键参数均为有效正值。
8. 真实公共行情探针执行全部 8 个 Alpha 节点，并生成 proposal hash。
9. 至少一个标的真实取得 Ticker + Orderbook，实时循环心跳有效。
10. 独立签名账户快照成功且不超过 45 秒。
11. ReconciliationEngine 返回 `MATCHED`，双方事实快照不超过 120 秒。
12. Testnet 所有非零持仓均有当前交易所 `openAlgoOrders` 保护，服务器保护单数量与本地预期数量完全一致。
13. 无 P0 活动事故，错误增量未越界。
14. HTTP 健康线程运行，监督器证据可写。

## 4. 模块结构

```text
beidou_bootstrap/
├── cli.py          # beidou / 北斗 / bd；doctor/status/stop
├── preflight.py    # 环境、Git、配置、凭据、端口、包导入门禁
├── registry.py     # 核心对象、Alpha、因子、交易池、风险预算检查
├── runtime.py      # 行情、账户、对账、保护、心跳和事故检查
├── supervisor.py   # 启动编排、写互锁、RESUME 互锁、降级、恢复、锁定
├── state.py        # PID 锁、原子状态、历史证据、安全停止
└── models.py       # 检查和报告类型
```

## 5. 状态机与故障处理

```text
STARTING
  → PREFLIGHT
  → CONSTRUCTION_VALIDATION
  → STARTUP_VALIDATION
  → RUNNING

RUNNING + blocker
  → NO_NEW_RISK
  → DEGRADED
  → RECOVERING
  → VALIDATING
  → ACTIVE/RUNNING

连续 3 个周期 blocker
  → LOCKED
  → STOPPED
```

人工设置的 `NO_NEW_RISK` 或 `EXIT_ONLY` 被识别为 `PAUSED`，监督器不会擅自自动恢复；只有监督器自身因故障触发的暂停，在全部阻断重新验证通过后才允许恢复。

## 6. 验收标准

| AC | 验收条件 | 优先级 |
|---|---|---:|
| AC-01 | 安装后 `beidou`、`北斗`、`bd` 均解析到同一入口 | P0 |
| AC-02 | 无参数执行等价于 `start` | P0 |
| AC-03 | 第二实例被拒绝，陈旧 PID 可清理 | P0 |
| AC-04 | Testnet 脏工作区、端口占用、配置失败或凭据缺失时引擎不启动 | P0 |
| AC-05 | 19 个业务包逐项导入，缺一即阻断 | P0 |
| AC-06 | 非 Testnet 的交易所写请求被互锁拦截并留下证据 | P0 |
| AC-07 | 深度检查完成前控制面始终 `NO_NEW_RISK` | P0 |
| AC-08 | 29 个核心对象、8 个 Alpha、8 个因子、交易池和风险预算全部通过 | P0 |
| AC-09 | 真实行情只读探针执行 8 节点并生成 proposal hash | P0 |
| AC-10 | 未取得 Ticker + Orderbook 时不得 trading-ready | P0 |
| AC-11 | 独立账户查询失败或快照过期时阻断 | P0 |
| AC-12 | 对账非 `MATCHED`、事实缺失或过期时阻断 | P0 |
| AC-13 | Testnet 保护单数量不足或重复时均阻断 | P0 |
| AC-14 | `/ready`、`/trading-ready`、`/status` 与监督器状态一致 | P0 |
| AC-15 | 单周期阻断进入 DEGRADED，连续 3 次进入 LOCKED 并停止 | P0 |
| AC-16 | 人工暂停不会被监督器自动 RESUME | P0 |
| AC-17 | 状态文件原子写、历史证据只追加 | P1 |
| AC-18 | stop 只有在 PID、证据和进程身份一致时发送 SIGTERM | P0 |
| AC-19 | Mainnet/Canary/Live 仍被 EnvironmentGuard 阻断 | P0 |
| AC-20 | 原有 `python -m apps.autopilot` 保留 | P1 |

## 7. 已执行验证

- Python 语法编译：通过。
- 模块专项单元测试：9 项通过。
- CLI `python -m beidou_bootstrap --help`：通过。
- `pyproject.toml` 解析与三个命令别名：通过。
- 专项回归覆盖：PID 锁、阻断语义、命令别名、安全停止、零写互锁、交易所保护单精确覆盖、对账不匹配阻断。

## 8. 仍需完成的发布门禁

以下证据当前尚未获得，Draft PR 不应转为 Ready：

1. 仓库全量 `pytest`。
2. `ruff check` 与 `mypy --strict`。
3. 在具备 Hatchling 的环境执行 `pip install -e ".[dev]"`，验证三个真实 console scripts。
4. GitHub Actions 全部通过。
5. 真实 Binance Testnet E2E：账户、持仓、订单、保护、重启、停止和对账。
6. 断网、401、429、5xx、行情空响应、nearline 卡死、健康线程退出、保护恢复失败等故障注入。
7. 至少 24 小时持续运行证据。

因此当前结论是：**代码可进入独立审查，但不得宣称 Testnet-ready、无人值守、生产就绪、Mainnet 就绪或具备盈利能力。**
