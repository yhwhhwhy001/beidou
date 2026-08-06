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
