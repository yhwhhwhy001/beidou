# 北斗一键启动与深度自检

## 目标

`beidou_launcher` 是北斗唯一权威的一键启动模块。安装后执行以下任一命令，均进入同一套启动、自检和持续监督流程：

```bash
beidou
北斗
bd
```

原有 `python -m apps.autopilot` 保留用于兼容，但正式启动应通过上述统一入口。

## 启动流程

1. 检查 Python `>=3.12,<4.0`、Git commit、工作区、项目结构、端口和证据目录。
2. 导入并检查 19 个业务包，执行 EnvironmentGuard、凭据和 Testnet 签名密钥门禁。
3. 使用权限为 `0600` 的 PID 锁阻止重复实例。
4. Research、Paper、Shadow、Safety-only 模式在引擎 API 边界拦截 `POST/PUT/PATCH/DELETE`。
5. 深度检查完成前拦截引擎内部 `RESUME`，保持 `NO_NEW_RISK`。
6. 精确验证 29 个核心对象、8 个 Alpha DAG 节点、8 个因子、交易池和风险预算。
7. 使用真实公共 K 线、Ticker 和 Orderbook 执行无订单副作用的 8 节点 Alpha DAG，生成 proposal hash。
8. 独立读取签名账户快照，要求 ReconciliationEngine 返回 `MATCHED`。
9. Testnet 查询当前 `openAlgoOrders`，要求每个持仓的服务器保护单数量与本地预期完全一致。
10. 持续监测生命周期、控制面、行情、账户、对账、保护、心跳、错误和活动事故。

单周期 P0/P1 阻断会立即触发 `NO_NEW_RISK + DEGRADED`；连续 3 个周期仍异常则进入 `LOCKED` 并停止。人工设置的 `NO_NEW_RISK` 或 `EXIT_ONLY` 被视为 PAUSED，监督器不会擅自恢复。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 使用

```bash
# 默认 Paper，默认完整交易池
beidou

# Testnet
export BEIDOU_BINANCE_API_KEY='...'
export BEIDOU_BINANCE_API_SECRET='...'
export BEIDOU_SIGNING_KEY='至少16字符的独立签名密钥'
beidou --mode testnet --symbols DEFAULT

# 诊断、状态和停止
beidou doctor --mode testnet
beidou status
beidou stop
```

`beidou stop` 只有在 PID、最新监督器证据和进程命令身份一致时才发送 `SIGTERM`，避免 PID 复用误杀其他进程。

## 健康与证据

- `/ready`：引擎和监督器均处于 RUNNING。
- `/trading-ready`：全部深度证据通过且控制面为 RESUME。
- `/status`：包含监督器阶段、阻断项和检查结果。
- `.beidou/supervisor-state.json`：当前原子状态。
- `evidence/bootstrap/supervisor-history.jsonl`：只追加历史证据。

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 正常退出 |
| 2 | 前置或装配门禁阻断 |
| 3 | 已有实例 |
| 4 | 启动超时、自检失败或引擎提前退出 |
| 5 | 持续异常导致 LOCKED |
| 6 | 引擎任务异常退出 |
| 130 | 用户中断 |

## 安全边界

- 不新增 Mainnet、Canary 或 Live 能力。
- 不绕过 EnvironmentGuard。
- 不把查询失败转换为空账户、空仓位或健康状态。
- 不把进程存活、HTTP 200、对象存在或历史 algoId 当作交易就绪证据。
- 当前仓库仍处于 PIVOT/HOLD；本模块不授予 Testnet-ready、无人值守、生产或盈利认证。
