# 北斗一键启动与深度自检

## 目标

统一 `beidou`、`北斗`、`bd` 三个入口。安装后直接执行任一命令，即进入相同的一键启动、自检和持续监督流程；原有 `python -m apps.autopilot` 保留，不被删除。

这个模块不是简单的 shell 包装，而是位于 `AutonomousEngine` 外围的 fail-closed 启动监督器。

## 启动流程

1. **前置门禁**：检查 Python `>=3.12,<4.0`、Git commit、工作区状态、项目结构、端口、证据目录、19 个业务包、统一配置、EnvironmentGuard、账户读取凭据；Testnet 额外检查签名密钥。
2. **单实例保护**：使用权限为 `0600` 的 PID 锁，拒绝重复实例并清理陈旧 PID。
3. **零写互锁**：Research、Paper、Shadow、Safety-only 模式拦截引擎边界上的 `POST/PUT/PATCH/DELETE`，防止启动恢复代码意外写交易所。
4. **RESUME 互锁**：深度检查完成前，所有内部 `RESUME` 请求均被改写为 `NO_NEW_RISK`。
5. **模块与算法检查**：验证 29 个核心运行对象、8 个 Alpha DAG 节点、8 个因子生命周期、交易池及策略风险预算。
6. **真实算法探针**：使用真实公共 K 线、Ticker 和 Orderbook 执行无订单副作用的 8 节点 Alpha DAG，并生成 StrategyKernel proposal hash。
7. **事实门禁**：独立读取账户快照；要求内部账户、持仓和订单对账结果为 `MATCHED`；Testnet 使用当前 `openAlgoOrders` 验证每个持仓的止损/止盈保护单数量。
8. **持续监督**：监测生命周期、控制面、HTTP 健康线程、实时/近线心跳、行情事实、账户事实、对账、错误增量、活动事故和保护覆盖。
9. **自动降级**：单周期 P0/P1 阻断立即进入 `NO_NEW_RISK + DEGRADED`；连续 3 个周期仍未恢复则进入 `LOCKED` 并停止。
10. **证据留存**：当前状态原子写入 `.beidou/supervisor-state.json`，历史证据只追加写入 `evidence/bootstrap/supervisor-history.jsonl`。

监督器会把 `/ready`、`/trading-ready` 和 `/status` 与自身证据和控制面状态绑定，避免 HTTP 存活被误报为交易就绪。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 使用

```bash
# 默认 paper 模式，默认完整交易池
beidou
北斗
bd

# Testnet
export BEIDOU_BINANCE_API_KEY='...'
export BEIDOU_BINANCE_API_SECRET='...'
export BEIDOU_SIGNING_KEY='至少16字符的独立签名密钥'
beidou --mode testnet --symbols DEFAULT

# 仅运行启动诊断
beidou doctor --mode testnet

# 状态与安全停止
beidou status
beidou stop
```

`beidou stop` 只有在 PID、最新监督器状态证据和进程命令身份三者一致时才发送 `SIGTERM`，避免 PID 复用导致误杀其他进程。

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 正常退出 |
| 2 | 前置检查或模块/算法接线被阻断 |
| 3 | 已存在运行实例 |
| 4 | 启动超时、自检失败或引擎提前退出 |
| 5 | 持续 P0/P1 异常导致锁定 |
| 6 | 引擎任务异常退出 |
| 130 | 用户中断 |

## 安全边界

- 不新增 Mainnet、Canary 或 Live 能力。
- 不绕过现有 EnvironmentGuard。
- 不把查询失败转换为空账户、空仓位或健康状态。
- 不把进程存活、HTTP 200、对象存在或历史 `algoId` 当作当前交易就绪证据。
- 账户 UNKNOWN、行情不健康、对账非 `MATCHED`、算法不完整、风险预算无效或持仓保护不完整时，禁止增加风险。
- 当前仓库仍处于 PIVOT/HOLD 状态；本模块不授予 Testnet-ready、无人值守或生产认证。
