# 北斗一键启动与深度自检

## 目标

统一 `beidou`、`北斗`、`bd` 三个入口。直接执行任一命令即启动北斗，不再要求记忆 `python -m apps.autopilot`。

启动器不是 shell 包装层，而是独立监督器：

1. 启动前检查 Python、项目结构、配置、端口、证据目录、全部 19 个业务包、EnvironmentGuard、Testnet 凭据与签名密钥。
2. 构造后检查 29 个核心运行对象、8 个 Alpha DAG 节点、8 个因子生命周期、交易池和策略风险预算。
3. 在深度检查完成前拦截引擎内部 `RESUME`，保持 `NO_NEW_RISK`。
4. 使用真实公共行情执行无订单副作用的 8 节点 Alpha DAG 探针。
5. 运行中持续检查生命周期、行情源、HTTP 健康线程、实时/近线心跳、账户事实、错误增长、活动事故及 Testnet 持仓保护覆盖。
6. 单周期 P0 异常立即进入 `NO_NEW_RISK + DEGRADED`；连续 3 个周期仍为 P0 时进入 `LOCKED` 并停止引擎。
7. 所有检查结果原子写入 `.beidou/supervisor-state.json`，历史证据追加到 `evidence/bootstrap/supervisor-history.jsonl`。

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

# 状态与停止
beidou status
beidou stop
```

## 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 正常退出 |
| 2 | 前置检查或模块/算法接线被阻断 |
| 3 | 已存在运行实例 |
| 4 | 启动超时或运行引擎提前退出 |
| 130 | 用户中断 |

## 安全边界

- 不新增 Mainnet/Live 能力。
- 不绕过现有 EnvironmentGuard。
- 不把 HTTP 进程存活等同于交易就绪。
- 账户状态 UNKNOWN、行情不健康、算法缺失、风险预算缺失或持仓保护不完整时，禁止增加风险。
