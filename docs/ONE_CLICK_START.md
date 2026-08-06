# 北斗一键启动与全量自检

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

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
