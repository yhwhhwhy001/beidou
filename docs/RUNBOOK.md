# 运行手册（demo）

## 日常命令

| 目的 | 命令 |
| --- | --- |
| 拉取/刷新研究数据并选 universe | `beidou data sync` |
| 手动刷新实盘交易池（30 日成交量 + 滞回） | `beidou data pool refresh`（实盘循环每个 UTC 日也会自动做一次） |
| 重建时点成员表（研究用，先同步 878 个候选的日线） | `beidou data pool history [--sync-members]` |
| 单策略回测 / 验证 | `beidou research backtest --strategy tsmom`；`beidou research validate --strategy tsmom --universe pit --prior-trials N`（`--min-tenure K` 只交易已入池 ≥K 次的老牌币） |
| 退出层 / 回撤节流证据 | `beidou research overlay --universe pit` |
| 独立小书证据（主书 + fraction × 小书，D-018） | `beidou research book --main tsmom --sleeve flow --sleeve-params '{"long_side": false}' --universe pit --robustness static --prior-trials N` |
| 启动实盘（launchd 已托管） | `launchctl load -w ~/Library/LaunchAgents/com.beidou.live.plist`；手动：`deploy/run_live.sh` |
| 状态 / 健康检查 | `beidou live status --check` |
| 一键平仓 | `beidou live flatten --yes` |
| 停止加仓（可逆） | `beidou live kill-switch --engage` / `--release` |
| 日报 | `beidou report daily` |

## 改了 registry / profile 之后

实盘进程在启动时加载 registry、profile 与 universe；改动后必须重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.beidou.live
```

重启是幂等的（clientOrderId 按 bar 派生，先查后下）。重启后看 `.beidou/live/heartbeat.json` 的 `phase`、`universe_size`、`leverage`。

## Profile 关键字段（`config/live.demo.yaml`）

- `portfolio.leverage: auto` —— 每个币的交易所杠杆按 `max_gross / margin_cap` 与档位上限推导（当前 5x）；写死整数则固定。
- `portfolio.max_participation` —— 加仓单 ≤ 该比例 × 近 24 根 bar 平均报价成交量；`margin_buffer` —— 保证金不足时按比例缩小加仓单，保留这部分可用余额。
- `pool.refresh: daily|never` —— 每日自动重排 universe；被移出的币会被 reduce-only 平掉，`cycles.jsonl` 的 `universe_update` 记录进出。
- `exits` —— 止损 / 移动止损 / 止盈（单位 = 入场时日波动率），0 关闭；`cooldown_bars` 冷却期。
- `drawdown_throttle` —— 权益回撤在 `start`→`stop` 之间把整本书线性缩到 `floor`。

## 排障

- 心跳超时：`beidou live status --check`；看 `~/Library/Application Support/beidou/live.err.log`。
- 连续错误 ≥ 12 次进程退出，launchd 60s 后拉起；根因通常是网络或 -1021 时钟漂移（客户端自动重同步）。
- `cycles.jsonl` 每周期一行：`targets`、`orders`（含 `note`：`PARTICIPATION_CAPPED` / `MARGIN_SCALED`）、`exit_events`、`throttle`、`universe_update`、`skipped`。
- `state.json` 的 `exit_states`（入场价/极值/冷却）、`equity_hwm`、`universe`、`leaving` 在重启后恢复；入场价以交易所为准。
