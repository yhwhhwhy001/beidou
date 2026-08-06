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
# 北斗一键启动与全量自检

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
安装后以下命令等价：

```bash
beidou
北斗
bd
```

默认使用 `paper` 模式和 `BTCUSDT,ETHUSDT`。主网、`live`、`canary` 不在可选枚举中。

## 常用命令

```bash
# 一键启动：启动前自检 → 启动 Autopilot → 双快照运行验证 → 持续巡检
beidou

# 指定模式与交易池
beidou --mode shadow --symbols ALL
beidou --mode testnet --symbols BTCUSDT,ETHUSDT

# 只执行启动前诊断，不启动进程
beidou doctor --mode paper

# 查询或停止
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
## 启动前检查

Launcher 会检查：

1. Python 版本、项目结构、运行目录写权限、9090 健康端口。
2. 19 个 Beidou 包能否导入。
3. 关键行情、交易所、风险、审批、Outbox、订单状态机、账本、对账、保护、策略、因子、控制面、生命周期和自愈组件是否存在。
4. `ConfigProvider` 实际解析结果、Testnet URL、API 凭据和 Testnet 签名密钥。
5. 构造真实 `AutonomousEngine`，检查关键对象是否完成依赖装配。
6. 检查 Alpha DAG 的 8 个组件是否全部注册且 `validate()` 返回真。
7. 检查至少 8 个因子处于 ACTIVE/CHALLENGER 生命周期，交易池非空。

任何关键检查为 FAIL，Autopilot 不会被启动。

## 启动后检查

Launcher 不把“进程存在”当作系统正常。健康端点可访问后会采集两个间隔 6 秒的快照并验证：

- `/ready` 为真；
- 生命周期为 `ACTIVE`；
- 实际运行模式与命令一致；
- `tick_count` 确实增长；
- `error_count` 没有增长；
- 运行中的因子数量不少于 8；
- 没有 P0/CRITICAL 活动事故；
- 控制面状态符合模式：Paper/Testnet 为 `RESUME`，其他零写模式为 `NO_NEW_RISK`。

启动后 Supervisor 每 10 秒重复验证。连续三次异常才判定为持续故障，降低瞬时网络抖动的误报。

## 自愈与 Fail-Closed

- `paper`、`shadow`、`research`、`safety_only`：默认最多自动重启两次。
- `testnet`：发现持续故障后停止运行，不自动重新开放风险增加路径。
- 所有启动、自检和运行时报告写入 `evidence/BD-STARTUP/`。
- 子进程输出写入 `logs/beidou-autopilot.log`。
- 单实例信息写入 `.beidou/supervisor.json`，防止重复启动。

## 所需环境变量

当前 Autopilot 在所有模式的启动阶段都会读取账户快照，因此需要：

```bash
export BEIDOU_BINANCE_API_KEY="..."
export BEIDOU_BINANCE_API_SECRET="..."
```

Testnet 风险审批还需要：

```bash
export BEIDOU_SIGNING_KEY="..."
```

密钥内容不会写入启动报告或日志。
