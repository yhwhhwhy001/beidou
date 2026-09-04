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
| 探针书状态（D-019） | `beidou live status`（心跳 `probes`）、日报 `Probe books` 段；自动停书后 `state.json.stopped_books` 有记录；手动停书：registry 里该策略 `enabled: false` 后重启 |
| 启动实盘（launchd 已托管） | `launchctl load -w ~/Library/LaunchAgents/com.beidou.live.plist`；手动：`deploy/run_live.sh` |
| 状态 / 健康检查 | `beidou live status --check` |
| 核对实盘输出可复现（M-011） | `beidou live verify --check`（用公共数据 + `state.json` 离线重算上一周期的 contributions；差异必须为 0） |
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

## Registry 里的书（`config/alpha_registry.yaml`）

- `books.<name>.fraction` —— 独立小书的风险预算比例；策略用 `book: <name>` 归属，未写的属于主书。
- **资金费率**：round 6b（D-023）之后行情端口提供 `funding_history`，实盘面板与研究面板由同一份结算费率构成，消费资金费率的设置（tsmom 的 `crowding_window > 0`、`carry` 信号）**不再会被静默跳过**——信号自己声明需求，拿不到历史时 `AlphaModel.targets` 报错、`beidou live run` 拒绝启动（第六轮 KILL-027 的结构性关闭）。当前 tsmom 仍跑 `crowding_window: 0`：重新打开它是**证据问题**而不是管线问题，需要按 D-013/D-020 在时点 universe 上重验并更新 evidence 指针。
- 探针书（D-019）：`evidence.verdict: ACCEPT`（来自 `beidou research book`）+ `probe` 块（`accepted_by`、`accepted_on`、`stop: {window_days, max_loss}`、`review_after_days`）。启动时核对报告种类、对象与 fraction；缺任何一项 `beidou live run` 拒绝启动。当前：`flow_short`（flow 只做空，1/3 预算，30 天 −1% 自动止损，2026-12-02 复审）。

## 排障

- 心跳超时：`beidou live status --check`；看 `~/Library/Application Support/beidou/live.err.log`。
- 连续错误 ≥ 12 次进程退出，launchd 60s 后拉起；根因通常是网络或 -1021 时钟漂移（客户端自动重同步）。
- `cycles.jsonl` 每周期一行：`targets`、`orders`（含 `note`：`PARTICIPATION_CAPPED` / `MARGIN_SCALED`）、`exit_events`、`throttle`、`universe_update`、`skipped`。
- `state.json` 的 `exit_states`（入场价/极值/冷却）、`equity_hwm`、`universe`、`leaving` 在重启后恢复；入场价以交易所为准。
- `heartbeat.json` 的 `history_bars`（当前 1,442）与 `external_flows`；`cycles.jsonl` 的 `external_flows`（`rebaselined: true` = 该周期发生了非交易性现金流，日起点 / 高水位已重置，日报 drift 跳过该 bar）。
- 账户在 demo UI 里被重置后不需要任何操作：下一周期自动重建仓位；`beidou report daily` 的 External cash flows 段显示金额。
- `state.json.last_contributions` 是 NO_ACTION 的 hold 种子（D-022），不要手动删除；删除等于把所有未触发信号的仓位归零一次。
- `beidou live verify`：contributions 必须逐币复现（`ok: true`）；`target_diffs` 非零只是提示——退出层 / 节流 / 护栏在模型之后动作。`bar_matched: false` 说明 `state.json` 来自另一根 bar，等下一周期再跑。2026-09-04 01:00Z 的实测：两本书差异均为 0.0。
- `cycles.jsonl` 的 `gross_before` 自 D-023 起按 `positionRisk` 的仓位求和；此前恒为 0（账户报文不带 positions 数组），满仓也显示为空仓。
