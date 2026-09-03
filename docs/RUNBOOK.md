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
beidou research validate --strategy tsmom               # 写 reports/research/<strategy>-<date>.json 供 registry 引用
```

## 实盘（demo）

```bash
export BEIDOU_DEMO_API_KEY=...; export BEIDOU_DEMO_API_SECRET=...
beidou live run --profile config/live.demo.yaml --dry-run --cycles 3   # 只算不下单
beidou live run --profile config/live.demo.yaml                        # 长驻
beidou live status
beidou live flatten --profile config/live.demo.yaml                    # 一键市价平仓
touch .beidou/live/KILL_SWITCH                                          # 禁止新增风险（reduce-only 仍可用）
```

## 无人值守（macOS launchd）

见 `deploy/com.beidou.live.plist`；`launchctl load` 后进程崩溃 60s 内拉起，启动先对账不重复开仓。
