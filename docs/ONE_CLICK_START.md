# 北斗安全入口、显式启动与深度自检

## 目标

`beidou_cli` 是默认安全门面。安装后执行以下任一裸命令只显示帮助，
不会构建运行时、读取交易凭据、启动监督器或产生网络/数据库写入：

```bash
beidou
北斗
bd
```

运行时构建必须使用显式的 `beidou execution start`，并且只在当前任务获得
人工批准后临时设置本地授权标记。`beidou start` 仅是受完全相同门禁约束的
兼容别名。遗留 Python 入口仍受固定模式和 launcher 门禁约束；它们不授予
Testnet 写入、无人值守或自动重启权限。旧 launchd 入口不授予运行权限。

普通启动器不得进入 Testnet。唯一 Testnet 验证入口是
`python -m apps.testnet_verify`，它还有独立的确认、额度、kill switch、
账户身份和对账门禁。入口存在不代表当前获得 Testnet 写入授权。

## 受控运行时流程

1. 检查 Python `>=3.12,<4.0`、Git commit、工作区、项目结构、端口和证据目录。
2. 导入并检查 19 个业务包，执行 EnvironmentGuard、凭据和 Testnet 签名密钥门禁。
3. 使用权限为 `0600` 的 PID 锁阻止重复实例；启动不会自动 SIGTERM/SIGKILL 旧实例。
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
# 裸命令：只显示帮助，不启动
beidou

# 只读诊断和状态
beidou doctor --mode safety_only
beidou status

# 仅在本次本地 paper/shadow 任务已经获得人工批准时：
BEIDOU_EXECUTION_AUTHORIZATION=EXPLICIT_LOCAL_APPROVAL \
  beidou execution start --mode paper --symbols BTCUSDT

# 兼容别名，使用同一授权门禁
BEIDOU_EXECUTION_AUTHORIZATION=EXPLICIT_LOCAL_APPROVAL \
  beidou start --mode paper --symbols BTCUSDT

# Alpha 的本地离线计算不会启动交易运行时
beidou alpha evaluate --closes '<至少51个逗号分隔的正数>'

# 仅查看唯一 Testnet 验证器的参数；不会授权或执行订单
python -m apps.testnet_verify --help

# 只在存在已验证的运行实例时，按 PID、证据时效和命令身份停止
beidou stop
```

重复启动会直接拒绝并保留现有实例；如需停止，必须先由人工确认订单/保护事实，再使用 `beidou stop`。
`beidou stop` 只有在 PID、最新监督器证据和进程命令身份一致时才发送 `SIGTERM`，避免 PID 复用误杀其他进程。

仓库中的 launchd plist 只是禁用自动调度的安全模板，不是部署授权。旧
`com.beidou.autopilot`、`com.beidou.watchdog` 和
`com.beidou.testnet-verify` 用户任务已被隔离；不得自动加载、kickstart 或设置
`KeepAlive`。在版本化制品、受控密钥提供器、独立审核、恢复演练和新的部署授权完成前，
不得把任何模板加载为无人值守服务。

## 健康与证据

- `beidou status`：读取监督器快照并同时验证进程身份与证据时效；历史
  `RUNNING` 可被降级为 `STALE`，没有证据时才是 `NOT_RUNNING`。
- `beidou doctor`：只读执行启动、依赖、策略和部署漂移检查；存在阻断项时退出码为 2。
- `/ready`：监督器已授权、无当前 P0/P1 blocker、控制面与新鲜事实满足 readiness；进程 RUNNING 或 HTTP 200 本身不构成 ready。
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
- 不把离线 Alpha 计算通过、单次 Testnet 成交或 30/30 执行探针当作
  Alpha 盈利、Economic Truth E0-E6 或生产就绪证据。
- 当前全局 Testnet kill switch 必须保持 engaged；没有新的精确授权不得移除。
- 当前仓库仍处于 PIVOT/HOLD；本模块不授予 Testnet-ready、无人值守、生产或盈利认证。
