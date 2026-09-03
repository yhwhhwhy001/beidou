# 运行手册

## 安装

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## 数据

```bash
beidou data sync --universe config/universe.yaml        # 下载/增量更新 mainnet 1h K 线与资金费率到 .beidou/data/
```

## 研究

```bash
beidou research backtest --strategy tsmom --from 2022-01-01
beidou research diagnose --strategy flow                # 信号级诊断：按前瞻期的 IC（Newey-West t）、翻转次数、纯信号回测
beidou research validate --strategy tsmom               # 写 reports/research/<strategy>-validation-<date>.json 供 registry 引用
beidou research correlate --strategies tsmom,flow       # 策略净收益相关性与边际 Sharpe
```

## 实盘（demo）

```bash
export BEIDOU_DEMO_API_KEY=...; export BEIDOU_DEMO_API_SECRET=...
beidou live run --profile config/live.demo.yaml --dry-run --immediate --cycles 1   # 只算不下单，立刻跑一根 bar
beidou live run --profile config/live.demo.yaml --paper --immediate                # 无需密钥：mainnet 真实数据 + 进程内模拟成交（状态在 .beidou/paper/）
beidou live run --profile config/live.demo.yaml --immediate                        # 长驻：先跑上一根闭合 bar，再按小时对齐
beidou live run ... --allow-unvalidated                                            # registry 里的策略还没有验证报告时的显式放行
beidou live status [--paper] [--check]                                             # heartbeat/state；--check 在心跳过期或连续报错时返回非零，可接 cron/launchd 告警
beidou live kill-switch --engage | --release                                       # 禁止/恢复新增风险（reduce-only 仍可用）
beidou live flatten --profile config/live.demo.yaml --yes                          # 一键市价平仓
beidou report daily [--paper] --date 2026-09-04                                    # 日报（权益、按策略/币种归因、成本、护栏事件、实盘 vs 验证预期的 drift）
```

状态文件在 `.beidou/live/`：`state.json`（上一根 bar 的目标与贡献）、`trades.jsonl`（每笔订单，含 clientOrderId/目标权重）、`attribution.jsonl`（按策略归因）、`cycles.jsonl`、`heartbeat.json`。

## 无人值守（macOS launchd）

```bash
mkdir -p ~/Library/Application\ Support/beidou
printf 'export BEIDOU_DEMO_API_KEY=...\nexport BEIDOU_DEMO_API_SECRET=...\n' > ~/Library/Application\ Support/beidou/env.sh && chmod 600 ~/Library/Application\ Support/beidou/env.sh
cp deploy/com.beidou.live.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.beidou.live.plist     # 启动；KeepAlive 在崩溃后 60s 拉起
launchctl unload -w ~/Library/LaunchAgents/com.beidou.live.plist   # 停止
```

启动先对账（交易所仓位为唯一真值、撤销残留挂单、按 bar 派生的 clientOrderId 先查后下），因此重启不会重复开仓。日志在 `~/Library/Application Support/beidou/live.*.log`。让机器保持唤醒：`caffeinate -i` 或系统设置。
