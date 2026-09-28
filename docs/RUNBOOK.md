# 运行手册（demo）

## 日常命令

| 目的 | 命令 |
| --- | --- |
| 拉取/刷新研究数据并选 universe | `beidou data sync` |
| 修补研究归档里的缺口（9.4）。默认干跑：列出每个缺口要取的日归档文件、REST 调用数和估计字节数，不联网、不写盘。`--apply` 才下载和合并，源头也没有的记进 `confirmed_gaps.json`（confirmed gap）。不要和每日 01:20 的数据任务同时跑，两者改写同一批 parquet | `beidou data repair [--symbols A,B] [--apply]` |
| 手动刷新实盘交易池（30 日成交量 + 滞回） | `beidou data pool refresh`（实盘循环每个 UTC 日也会自动做一次） |
| 重建时点成员表（研究用，先同步 878 个候选的日线；出日表：2026-09-04 起研究口径逐日重选，2026-09-23 起 `--refresh` 默认就是 `D`，传 `MS` 出的是月表）。**重建会挡住 armed 启动**，先读下面「成员表落后告警」 | `beidou data pool history --refresh D [--sync-members]` |
| 时点成员表落后几天（每小时巡检带 `--check` 跑它） | `beidou data pool lag [--check]` |
| 单策略回测 / 验证 | `beidou research backtest --strategy tsmom`；`beidou research validate --strategy tsmom --universe pit --prior-trials N`（`--min-tenure K` 只交易已入池 ≥K 次的老牌币） |
| exit overlay / 回撤节流证据 | `beidou research overlay --universe pit` |
| 信号 vs 构建归因（D-024，不计 ledger） | `beidou research decompose --strategy tsmom --universe pit --from 2021-01-01` |
| 独立小书证据（主书 + fraction × 小书，D-018） | `beidou research book --main tsmom --sleeve flow --sleeve-params '{"long_side": false}' --universe pit --robustness static --prior-trials N` |
| 探针书状态（D-019） | `beidou live status`（心跳 `probes`）、日报 `Probe books` 段；自动停书后 `state.json.stopped_books` 有记录；手动停书：registry 里该策略 `enabled: false` 后重启 |
| 启动实盘（launchd 已托管） | `launchctl load -w ~/Library/LaunchAgents/com.beidou.live.plist`；手动：`deploy/run_live.sh` |
| 状态 / 健康检查 | `beidou live status --check` |
| 核对实盘输出可复现（M-011） | `beidou live verify --check`（用公共数据 + `state.json` 离线重算上一周期的 contributions；差异必须为 0） |
| 检查唤醒时刻与 bar 边界的对齐 | `beidou live status --check`（对齐误差 > 60s 非零退出；整数个 bar 的偏移不算问题，D-025） |
| 因子挖掘（每轮记 514 行 ledger，先看 R1 预算） | `beidou research mine --strategy tsmom --universe pit --baseline tsmom`；只测量不计费：`--measure` |
| 定时刷新研究数据（每日 01:20，klines + 资金费率 + **现货** + **metrics 归档** + 池刷新，最后跑归档专属测试） | `cp deploy/com.beidou.data.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.beidou.data.plist` |
| 定时跑上面两项（每小时 :10） | `cp deploy/com.beidou.check.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.beidou.check.plist` |
| 一键平仓 | `beidou live flatten --yes` |
| 停止加仓（可逆） | `beidou live kill-switch --engage` / `--release` |
| 日报 / 周报 | `beidou report daily`；`beidou report weekly`（含 90% alpha 投入占比 `effort_share`） |

2026-09-16 补：上表此前漏掉整个 `governance` 组、三个 launchd 任务、`live soak`、`research mine`
与 `report weekly`。漏的不是边角——`governance` 是晋级线本身，而漏掉的三个任务里有两个正在这台
机器上跑。下面两节补上。

## 治理（`beidou governance`，18 个子命令）

自主开关是**每个工作副本**的运行期状态，不入库：`governance/ENABLED` 存在时这个 checkout 才允许
写 registry。没有它，`apply` 只会预演。

| 目的 | 命令 |
| --- | --- |
| 下一步该做什么（读证据 + 状态，只决定不执行） | `beidou governance next` |
| 把记录里已经发生的事折进状态 | `beidou governance advance` |
| 晋级计划 / family gate 读数 | `beidou governance plan`；`beidou governance gate` |
| 事务化写 registry（需要 `governance/ENABLED`） | `beidou governance apply` |
| 部署健康金丝雀（读 shadow soak，不判 alpha） | `beidou governance canary` |
| 规则重放：每份报告都要能被解释（AC-G0 未归因项必须为 0） | `beidou governance replay` |
| 状态 / 在位时长 / 事务链 / 机器判定 / 人工复核 | `beidou governance status\|tenure\|transactions\|verdicts\|review` |
| 进程持有的 registry 与磁盘上的是否分岔 | `beidou governance divergence` |
| 重开条件 / 批次窗口 | `beidou governance reopen`；`beidou governance window` |
| 60 天内会翻转的日期开关（只读，不动任何开关） | `beidou governance calendar` |
| 自主开关：建或删本 checkout 的 `governance/ENABLED` | `beidou governance enable`；`beidou governance disable` |

### startup gate 在写入时问（2026-09-27 起）

- `plan` 与 `apply` 写入前问 armed `live run` 的 startup gate，两半都问：evidence，以及 dataset 检查的 blocking
  那一半。advisory 不拒绝。此前只问 evidence 那一半。
- dataset 那一半读 `--data-root`，默认 `.beidou/data`，相对当前目录，与 `live run` 相同。所以要在主 checkout 里跑。
  在没有数据的 worktree 里跑，它会报 `membership: present then, absent now` 并拒绝。
- membership 重建之后，证据在新表上重出之前，`apply` 会回滚。bridge 过期后 armed 重启会在同一处被拒，这里只是提前说。
- canary 不再有 `startup_gate`，`--gate-refusals` 也删了。shadow 是 dry run，从不拒绝，这一项从来没有来源。
  想在 soak 开始时就知道候选过不过 startup gate，跑一次 `beidou governance plan --proposed <候选>`。

### L4 只为 soak 跑过的那份 registry 作保（2026-09-27 起）

- `plan` 与 `apply` 的 L4 多一项 `registry_soaked`。被评那一轮每个已决周期的 `registry` 摘要要只有一个，
  而且等于提议那份的摘要。摘要是 `engine.registry_digest`：策略、参数、权重、books、钉住的 universe，
  不含 evidence 指针和注释。
- 所以换了候选就要重新 soak：用下面「另外两个 launchd 任务」一节那条命令移走记录，launcher 从空记录起新的一轮。
  只改注释或 evidence 指针，摘要不变，不用重新 soak。
- 不一致时，`plan` 的 `canary:` 行写出两边的摘要。`governance canary` 不核这一项：它手里没有提议。
- 全部停用的提议建不出模型，也就没有摘要。它不晋级，L4 不会被问到，照常走到 startup gate。

## 另外两个 launchd 任务

| 任务 | 干什么 | 装载 |
| --- | --- | --- |
| `com.beidou.shadow` | L4 金丝雀 soak：拿 `config/alpha_registry.candidate.yaml` 在 armed 循环旁边跑 168 个 dry-run 周期，写 `.beidou/live-shadow-dry-run`，不碰账户、不重排 universe。一轮跑满就停，不论其中失败几个。停靠的是 `run_shadow.sh` 启动前问记录，不是 `KeepAlive`，见下文「shadow soak 怎么停」。读数：`beidou governance canary`，只评最近一轮 | `cp deploy/com.beidou.shadow.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.beidou.shadow.plist` |
| `com.beidou.paper-l3` | §5 L3 的七天累积器：`--paper` 在 mainnet 价位上撮合，`--state-dir .beidou/paper-l3`，无凭据、结构上不可能变成交易进程。读数：`beidou live soak --check`（**报告而不闸**：前六天按构造必然为假，接进 `failed` 等于每小时误报一周） | `cp deploy/com.beidou.paper-l3.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.beidou.paper-l3.plist` |

`com.beidou.proxy-probe` 曾是第三个，2026-09-22 撤除：它是临时测量，判读做出来了就该收。
结论与读数在 `docs/RESEARCH_LOG.md`「2026-09-22 · 代理路径：对照臂的判读结清」一节；
要装回去（例如复核换节点的效果）：

```bash
git show 3a4bf6fa:deploy/run_proxy_probe.sh > deploy/run_proxy_probe.sh && chmod +x deploy/run_proxy_probe.sh
```

```bash
git show 3a4bf6fa:deploy/com.beidou.proxy-probe.plist > ~/Library/LaunchAgents/com.beidou.proxy-probe.plist && launchctl load -w ~/Library/LaunchAgents/com.beidou.proxy-probe.plist
```

改了候选 registry 之后 soak **必须重启**才会生效（引擎只在启动时建模），且原 `cycles.jsonl` 要移走
而不是追加——一份 soak 记录只能描述一个候选：

```bash
launchctl bootout gui/$(id -u)/com.beidou.shadow && mv .beidou/live-shadow-dry-run .beidou/live-shadow-dry-run.$(date -u +%Y%m%d) && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.beidou.shadow.plist
```

一轮跑满之后，launcher 不会自己再起一轮。要再 soak（换了候选，或同一个候选再测一轮），也用上面这条命令。
记录移走之后，launcher 从空记录起一轮 168。

### shadow soak 怎么停（2026-09-26 更正）

上表那一行此前写的是：`KeepAlive` 只在崩溃时生效，soak 在 168 周期正常结束，那里重启等于静默开始第二次。
机制写错了：

- `live run --cycles 168` 只要有一个周期失败，就以 1 退出（`beidou_cli/live_cmd.py` 的 `done < cycles`）。
  代理 503 很常见，几乎每轮都有失败周期。
- plist 的 `KeepAlive {SuccessfulExit: false}` 把非零退出当崩溃，立刻重新拉起。`run_shadow.sh` 用 `exec`，
  退出码直接交给 launchd。
- 2026-09-23 就是这样。第一轮有 3 个 ERROR，08:00:26Z 以 1 退出。launchd 下一秒拉起新进程。
  第二轮追加进同一个 `cycles.jsonl`，construction 从 `ccd7bb9764b5` 换成 `b8f215ab706c`。
- `governance canary` 把两轮当一轮读。2026-09-26T17:40Z 的读数：`soak 249/168` PASS，`construction_stable`
  FAIL，总判 UNHEALTHY。两轮各自只有一个 construction，这个 FAIL 来自混读。
- 一轮零失败、以 0 退出也拦不住：下次登录或 `launchctl load` 时，`RunAtLoad` 会再起一轮。

现在让 soak 停在 168 的是 launcher：

1. `run_shadow.sh` 启动前先问 `beidou governance canary --remaining`。它读 dry-run 实际写的目录
   （`store_directory`），答最近一轮还差几个周期。
2. 答 0：最近一轮已满。打印一行，以 0 退出。launchd 不再拉起，`RunAtLoad` 再跑一次也一样。
3. 答 N > 0：只跑 N 个周期（`--cycles N`）。进程崩溃或机器重启之后，接着跑同一轮，不另起一轮。
4. 问不出来（命令本身失败），或答的不是个数：以 70 退出，不起循环。launchd 60 秒后重试，与崩溃时一样。

一轮 = 记录里连续 168 个尝试过的周期。OK 与 ERROR 都算。SKIPPED 不算，`engine.run` 也不数它。
canary 的 `soak` 按同一口径计数，`--remaining` 与打分共用一个切法（`beidou_governance/canary.py` 的 `rounds`）。

`governance canary`、`plan`、`apply` 只评最近一轮。`governance canary` 先逐轮列出行号、bar 区间与 ERROR 数，
前面的轮只列不评。

失败周期不影响停不停，交给 canary 的 `no_error_streak` 评。`live run` 的退出码没改：一轮里有失败，它照旧
以 1 退出。launchd 照旧拉起一次，launcher 看到这一轮已满，以 0 退出。所以 `launchctl list` 上最后的退出码
是 0。失败要读 canary，不读退出码。

plist 的键没改，只改了注释，已装载的那份不用重新装载。launchd 每次拉起都重新执行 `run_shadow.sh`，新逻辑在下一次拉起时生效。
launchd 执行的是主 checkout 里的那一份。

## 成员表落后告警（2026-09-23 起）

操作者 2026-09-23 裁定：时点成员表维持手动重建，不排定时任务。每小时巡检为此加了一行，报它落后几天
（`beidou data pool lag --check`）。

**含义。** 表末行之后的 bar，研究侧沿用末行的成员（`membership_at_bars` 前推）。落后 k 天，就是研究
最近 k 天用的是一个冻住的池子。刚重建完读数是 1，不是 0：末行是最后一根收盘日线，也就是昨天。
落后满 14 天告警。表不存在、读不了、是空的、不是日表，或末行晚于今天，也都告警。这些状态都说不出
表有多新。

**为什么是 14 天。** 在 09-18 重建的真实表上量过。取截止今天的 30 天窗口，最后 14 天前推时，
平均 3.42% 的成员位拿错了名字。13 天时是 2.98%。3% 是 P12 第 0 段用过的线：成员改动低于它，
不值得跑回测。量法与出处在 `beidou_data/pool.py` 的 `MEMBERSHIP_ALERT_DAYS`。
`tests/data/test_the_membership_table_says_how_far_it_trails.py` 在真实表上逐个重算这些数。

**收到告警怎么办：不要马上重建。** `membership` 是 dataset manifest 的阻断字段，重建会改动它。
armed 启动随即被数据集门挡住（`registry_dataset_problems`）。要等证据在新表上重出，才能再过这道门。
2026-09-18 那次单独重建，就是这样引出了 D-041 bridge。所以：

1. 重建和证据重出排进同一个安排，时间由操作者定。重出要花 ledger。
2. 重建要出日表。2026-09-23 起 `--refresh` 默认就是 `D`；传 `MS` 出的是月表，巡检会接着报「不是日表」。
3. 开跑前先确认没有别的会话在写 `.beidou/data`（RISK-LD05）。`--sync` 默认开，会给每个候选下载
   日线，走的是实盘循环那条代理，按 RESEARCH_LOG 里回填那条整点避让挑时间。
4. 表是原子写入的（2026-09-23 起）：巡检读到的要么是旧表，要么是新表。命令开跑时先印一行数据集门的提示。

告警每天推送一次，直到重建为止（操作者 2026-09-23 裁定）。巡检日志里仍每小时记一行 FAIL，变的只是推送。
这一条的去重用单独的状态文件 `alert-dedup-daily.json`：共用的 `alert-dedup.json` 会被每小时的检查写回时冲掉。

## 衰减与 bar sanity 两条告警（2026-09-23 起）

两条都由 `report daily --check` 发出，都只告警。机器不因它们改权重，也不降级、不退休。

### 衰减规则判 REVIEW（G1）

**含义。** 两个不重叠的 30 天窗口里，这条策略的归因夏普都低于它的证据报告里的回测 q10。
窗口从当前构造起算（`evidence_window`，与 M-010 同一起点）。构造一变，窗口重新起算。

**什么时候会出现。** 当前构造从 2026-09-17T16:00Z 起算，最早 2026-11-16T16:00Z 才有两个完整窗口。
在那之前，日报「Edge decay (M-010 vs backtest q10)」一节读 INSUFFICIENT_DATA，只印不告警。
10-13 若改构造，窗口从那天重新起算。

**读之前先看口径。** 实盘窗口是已实现归因，q10 是回测盯市（操作者 2026-09-23 裁定 A）。
两种口径的波动差得很远：flow 的 30 天 σ 在 09-12 读数，已实现 0.137%，盯市 3.239%。

**收到告警怎么办。** 复审这条策略，结论由操作者定。

1. 读日报「Edge decay (M-010 vs backtest q10)」一节：两个窗口的读数与 q10。
2. 对照同一份日报的「Drift vs expectation (attributed income, M-002/M-010)」与「Long-run attributed Sharpe (M-G06)」。
3. 结论与动作写进 `docs/RESEARCH_LOG.md`。生命周期里没有哪个事件读这条告警（`beidou_governance/lifecycle.py`
   的 `Event`），`governance advance` 不会因它降级。

### bar sanity（G6）

**含义。** 实盘循环每周期拿到闭合 bar 后查三项，结果写进 `cycles.jsonl` 的 `bar_sanity`。模型、护栏、
下单都读不到它，交易照常。

- OHLC 自相矛盾，或价格不是有限正数。全部归档 15,127,202 根里一根都没有，出现就是数据源的问题。
- frozen bar：连续 2 根以上 OHLC 全等且零成交。成员日上见过的是 LUNA 停牌，与 FTT、ALPACA 下架前的死尾巴。
- 跳变：收盘价对上一根收盘翻倍或减半（|ln| > ln 2）。flag 记两根之间有没有成交衔接。

**哪些推送。** 当天首次出现的 flag 才推。同一根 bar 在 1,442 根窗口里停留期间不会天天重报。
新进池的名字，历史里的旧异常在进池当天报一次。

- 告警：断档跳变（中间缺 bar，或一侧零成交；BNX 2023-02 的 ×1/55 就是这种）、OHLC 矛盾、frozen bar。
- 提示：有成交衔接的跳变。它像真实行情，成员日上约每年 2.6 次（操作者 2026-09-23 裁定）。
- 提示：检查本身有周期没读完。

**生效要等重启。** 检查在实盘循环里，要等下一次按纪律重启才开始记录。在那之前，
日报「Bar sanity (G6, alert only)」一节读 `cycles_checked 0`，并写明原因。

**收到告警怎么办。**

1. 告警正文逐条列出标的、时刻、哪一项，跳变还带倍数与有没有成交衔接。
2. 断档跳变：先查是不是同名重新计价或换了合约，看币安公告。本地归档的缺口用 `beidou data status` 看，
   修补用 `beidou data repair`（见上面日常命令表）。
   同名下两段序列的例子见 `beidou_data/store.py` 的 `gaps`：BNXUSDT 2023-02-22、PUMPUSDT 2025-07-10。
3. frozen bar：多半是停牌或下架前的死尾巴。先用 `beidou live status` 看循环是否持有它。
4. OHLC 矛盾：拿交易所网页上同一根 bar 比对。
5. 这条告警不拦 bar，也不改目标。要不要对这个标的动手，由操作者定。

## 归档专属测试告警（2026-09-28 起）

七个测试读 `.beidou/` 本身：从归档切出来的夹具、实盘记录 `cycles.jsonl`、数据集门。归档不在就跳过，
所以 CI 与 worktree 里它们从来不跑。它们带 `archive` marker，由每晚的 data job 在最后跑一次
`pytest -m archive`（`deploy/run_data.sh`）。

**含义。** 推送正文「北斗归档专属测试失败：……」列出失败或被跳过的用例，末尾是 pytest 的汇总行。来源有三种：

- 有用例失败：归档或实盘记录与测试钉住的内容不一致了。
- 有用例被跳过：归档不在测试找的位置。这也算失败，没跑的检查不能报通过。
- pytest 没跑起来：`.venv` 坏了或导入失败，正文是它最后一行输出。

**什么时候会出现。** 主 checkout 快进之后，每晚 01:20 那次 job 跑完时，直到修好为止。去重用共享的
`alert-dedup.json` 与小时窗口，所以一夜推一次，一小时内手动重跑不再推。完整的失败尾部在 data job 的日志
（`data.stdout.log`）。日报「归档专属测试（夜间 data job，只报告）」一节印最后一次结果和它的时间，只印不告警。

**收到告警怎么办。**

1. 先看是哪一条。`test_the_fixtures_are_the_archive_verbatim[BNXUSDT_2023-02-22.json]` 从 2026-09-25 起红过：
   夹具 48 行，归档 552 行。那天的 `data repair` 给 2023-02 重新计价接缝前的缺口补进了 504 根 bar。09-28 查明归档对，
   已按归档重切，见 RESEARCH_LOG「O-5：BNX 2023-02 的 fixture 与归档」一节。它再红，就是归档又变了，照第 2 条先判断。
2. 不要为了变绿去改夹具或归档。这条测试说的是「归档在夹具之后变了」，先判断哪一边对。
3. 在主 checkout 上复现：`.venv/bin/python -m pytest -m archive -rfEs`，约 4 秒。

## 治理裁决入库（2026-09-29 起）

夜间 `com.beidou.governance-gate`（本机 02:30，即 18:30Z）按设计往 `governance/verdicts.jsonl` 追一行。它追在主 checkout 的工作树里，**不会自己提交**。
2026-09-25 与 09-27 两行就这样在工作树里放了三天，一次 `git checkout -- .` 就会丢，直到 #239 才入库。
日报「治理裁决入库（只报告）」一节会印出未入库的行数和最新一行；它读 `governance/verdicts.jsonl` 的工作树与 HEAD 的差。不告警。

入库步骤（顺序不能反）：

1. 从最新 `origin/main` 开 worktree，把主 checkout 的 `governance/verdicts.jsonl` **原样复制**过去（`cp`，逐字节）。
   `git diff` 只应该是追加行；不是纯追加就停下，先查清是谁改了历史行。
2. 四道门；`beidou governance replay` 的未归因项为 0。提交信息写清每行的时刻、kind、subject、ruling 与理由原文（09-20 的 f2d94275 与 #239 是先例）。review 字段照原样留空，审阅另走 `beidou governance review`。
3. 开 PR，CI 绿后合入。
4. **合入后在主 checkout 收尾**：先核对主 checkout 的文件与 `origin/main` 逐字节相同（`git diff --quiet origin/main -- governance/verdicts.jsonl`），
   再 `git checkout -- governance/verdicts.jsonl && git merge --ff-only origin/main`。
   这一步不能省：本地改动哪怕与合入的内容逐字节相同，`git merge --ff-only` 也会以「本地改动会被覆盖」中止（2026-09-28 在临时仓库实测）。
   不收尾的话，之后每个会话快进主 checkout 都会被挡住。
   避开 18:30Z（gate 追加的时刻）与整点前后；快进前看一眼这次会带进哪些提交，里面有需要重启的构造变更就先不快进，交给操作者。

## D-041 bridge 到期（2026-10-13）

**切换之后（2026-09-27 的切换 PR，分支 `live/k0175-switch-after-1013`）**：k 改为 0.175，tsmom 指向 k = 0.175 上的
WEAK_PASS 证据，证据门清门；`tests/shipped_evidence.py` 的 exemption 已删除。bridge 那一段按设计留在脚本里，
过期后不再生效；`BRIDGE_UNTIL` 改由 `test_the_bridge_stays_expired_on_the_date_it_was_given` 钉住。构造冻结原定
10-13 到期，操作者 2026-09-27 裁定提前到当天结束，本切换随即合入（理由与代价见 `FREEZE_ENDS` 的注释与 RESEARCH_LOG
同日一节）；新构造要等下一次重启才进循环。本节以下是切换之前写的处置，证据再次不清门时仍然适用。

2026-10-13T00:00Z（北京 08:00）起，`deploy/run_live.sh` 不再给 armed 启动传 `--allow-unvalidated`，严格的
证据门回来。同一刻，`tests/shipped_evidence.py` 的 exemption 与构造冻结也到期。分析、选项与裁定表在
`docs/analysis/2026-09-25-october-13-readiness.md`；操作者 09-25 的裁定记在 RESEARCH_LOG「操作者四条裁定」一节。

- 在跑的进程不受影响。受影响的是 10-13 之后的下一次启动。
- bash 3.2 展开空数组的缺陷已由 #137 修掉，09-25 已快进主 checkout。launchd 读的就是主 checkout 里这份脚本。
- 证据还没清门时，下一次 armed 启动会以 `tsmom: evidence verdict FAIL does not allow live use` 失败，launchd
  每 60 秒重试一次。循环不在，持仓就没有退出检查：止盈止损从不在交易所挂单。
- 10-13 起 CI 至少 9 条测试红，都跟 exemption 到期有关。那不是代码坏了，处理方式跟着 D-041 的裁定走。

**10-12 之前**：

- 确认主 checkout 含 #137：`grep -n 'BRIDGE\[@\]+' deploy/run_live.sh` 能找到一行。
- 用 `ps -eo pid,lstart,command | grep "live run"` 确认在跑的进程是 10-13 之前起的。
- 证据修好之前，不做有意重启。

**10-13 之后循环起不来时**：

- 症状：`~/Library/Application Support/beidou/live.stderr.log` 里先有 `bridge EXPIRED`，接着是证据门的拒绝；
  每小时巡检报「心跳已过期」，阈值 4,000 秒（2026-09-25 起；循环停在某根 bar 之后，下一次 :10 就会报）。
- 先平仓：`beidou live flatten --yes`。它不经过证据门，会挂上持久的 kill switch。恢复要先
  `beidou live kill-switch --release`，再启动，那时仍要过证据门。
- 不要为了让循环起来去改 `BRIDGE_UNTIL`。挪日期就是延长 bridge，是治理裁定。切换之前它与 `EXEMPT_UNTIL`
  由测试钉成相等；exemption 删除之后，由 `test_the_bridge_stays_expired_on_the_date_it_was_given` 钉住。

## 改了 registry / profile 之后

实盘进程在启动时加载 registry、profile 与 universe；改动后必须重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.beidou.live
```

重启是幂等的（clientOrderId 按 bar 派生，先查后下），但**要挑周期之间的窗口**：exit overlay 在 bar 收盘判定，一根没跑的周期就是那根 bar 没有退出检查，而且不会补。安全窗口是整点后 5 分钟到下一个整点前 10 分钟；重启前先跑 `tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py` 与 `test_construction_identity.py`确认这次重启不改构造（纪律与理由见 `CLAUDE.md`「重启实盘循环」）。重启后看 `.beidou/live/heartbeat.json` 的 `phase`、`universe_size`、`leverage`。

### 采纳 exit overlay / 信号改动的最短干净窗口（K-EX14，2026-09-07 操作者裁定）

M-010（30 天 income 归因）在当前构造指纹下不满 30 天连续记录之前，不采纳任何 exit overlay 或信号改动——研究可以跑、结论可以写，但 `config/live.demo.yaml` 的 `exits` 与 registry 的信号参数不动。唯一例外：风险预算阶梯（P13）触发，那是预登记的降档，不是采纳。档位以 `beidou_governance/policy.py` 的 `drawdown_ladder` 为准。2026-09-27 随 k 0.175 按可动用口径重推，现为总权益口径的回撤 −28.03% / −40.05%（policy 0.3.6，D-035）；09-14 至 09-27 的 −49% / −70% 与更早的 −35% / −50% 都已作废。

窗口起点**不写在这里**：它随每一次构造变更移动，写死在正文里的日期只会过期（这一段最初写的 2026-09-06T10:19Z / 最早采纳日 2026-10-06 就是如此，`unit_mode` 进指纹后一次重启即作废）。要当前答案，读这两处之一——`beidou report daily` 的 evidence-window 一节（`since_ms` 是起点、`bars` 是已积累的周期数），或 `cycles.jsonl` 里 `construction` 最后一次变化的那根 bar。最早采纳日 = 该起点 + 30 天。

每次采纳都是构造变更，窗口重新计数；**只加指纹字段、不改行为也算**——2026-09-07 的 `unit_mode` 就是一例（`0dcd044d0158` → `b441ea62d021`，配置一个字符没改，见 RESEARCH_LOG 同日条目）。

## Profile 关键字段（`config/live.demo.yaml`）

- `portfolio.leverage: by_vol` —— 每个币的交易所杠杆按它自己的年化波动率分档（分交易对杠杆报告的方案 R1，操作者 2026-09-26 卡片「交易所也分档」；推导见 `docs/analysis/2026-09-26-per-symbol-leverage-first-principles.md` §5.2）。档位 = 离 `max_leverage × leverage_sigma_ref ÷ σ` 对数距离最近的那一档（`leverage_tiers`，1x–15x；k=0.175 下持有的落在 3x–15x）；要换档须连续 `leverage_hysteresis` 个周期（168，一周；24 在 T-11 回放里一天最多换 9 次，超了报告的上限 5）都要换；交易所档位表在当前名义上不收的档位当周期就降；整本书的初始保证金不超过 `auto` 要的，超了就把最低的档往上抬，**从不缩单**。它只决定开仓占用多少保证金——权重、订单、退出都在读它之前就定了。
  - 每天第一个周期全量重发一次：demo 的接口读不回杠杆，账户重置会一声不响地改回默认。下发被拒或网络出错：保留原设置、告警（`leverage-refused`），循环照常，迟滞满了再试一次。
  - 读数在 `cycles.jsonl` 每行的 `leverage_tiers`：`ideal`（波动率档位）、`clamped`（被档位表压住的）、`raised`（被保证金不变量抬起的）、`pending`（正在攒周期的）、`sent` / `refused`（这周期发了什么）、`set`（发完之后的设置）、`margin`（分档与 `auto` 下目标书的初始保证金占权益的比例，前者不得超过后者）。
  - **上线首日要人看一次（T-12）**：重启后第一个整点周期跑完，在交易所界面上逐个币核对杠杆，与 `tail -1 .beidou/live/cycles.jsonl | python3 -c "import json,sys; print(json.load(sys.stdin)['leverage_tiers']['set'])"` 一致。接口读不回，这一步只能人看。
  - **回滚**：这一行改回 `auto` 再按上面的纪律重启。启动时全量重发 5x，清掉分档的记忆（`leverage_tiered`、`leverage_streaks`）；构造指纹随之变化，监控窗口再清零一次。
- `portfolio.leverage: auto` —— 每个币的交易所杠杆按 `max_gross / margin_cap` 与档位上限推导（5x，D-016）；写死整数则固定。`by_vol` 之前的出厂值，也是它的回滚值。
- `portfolio.max_gross_denominator: usdt_equity` —— 守卫把 gross 截在 `max_gross` × USDT 余额（`usdt_equity`，交易所 USDT 的 `marginBalance`），不再是 × 总权益（操作者 2026-09-28 裁定 `risk-g11-denominator`）。账户约 43% 的权益是 BTC 抵押品时，上限约是 1.14 × 总权益。`margin_cap` 不用另改：D-016 按两者之比定杠杆，上限处的保证金自然是 USDT 余额的一个比例。`max_weight` 与日亏暂停仍按总权益。
  - 读不到 USDT 余额（或它不为正）时守卫只减不加，原因码 `NO_USDT_EQUITY`，边沿触发告警一次。先看账户接口的 `assets` 里有没有 USDT 那一行，不要把这一项改回 `equity` 来「修」。
  - 回测没有抵押品，这一项对回测和证据门都是空操作：证据摘要不变，启动门不受影响。
  - **回滚**：改回 `equity`，再按上面的纪律重启。构造指纹随之变化，监控窗口再清零一次。
- `portfolio.max_participation` —— 除完全平仓外，每一单 ≤ 该比例 × 近 24 根 bar 平均报价成交量。纯减仓单也会被截：`exempt_reductions` 默认关，要开就得与回测的 `ParticipationModel` 一起翻（`beidou_live/rebalancer.py` 的注释）。`margin_buffer` —— 保证金不足时按比例缩小加仓单，保留这部分可用余额。
- `pool.refresh: daily|never` —— 每日自动重排 universe；被移出的币会被 reduce-only 平掉，`cycles.jsonl` 的 `universe_update` 记录进出。
- `exits` —— 止损 / 移动止损 / 止盈（单位 = 入场时日波动率），0 关闭；`cooldown_bars` 冷却期。
- `drawdown_throttle` —— 权益回撤在 `start`→`stop` 之间把整本书线性缩到 `floor`。

## Registry 里的书（`config/alpha_registry.yaml`）

- `books.<name>.fraction` —— 独立小书的风险预算比例；策略用 `book: <name>` 归属，未写的属于主书。
- **资金费率**：round 6b（D-023）之后行情端口提供 `funding_history`，实盘面板与研究面板由同一份结算费率构成，消费资金费率的设置（tsmom 的 `crowding_window > 0`、`carry` 信号）**不再会被静默跳过**——信号自己声明需求，拿不到历史时 `AlphaModel.targets` 报错、`beidou live run` 拒绝启动（第六轮 KILL-027 的结构性关闭）。tsmom 现在跑 `crowding_window: 72`，修饰器已重新打开。打开它是**证据问题**而不是管线问题：按 D-013/D-020 在时点 universe 上重验，再更新 evidence 指针。经过见 `config/alpha_registry.yaml` tsmom 条目 `params` 之上的「2026-09-05 RE-ENABLED」一段。
- 探针书（D-019）：`evidence.verdict: ACCEPT`（来自 `beidou research book`）+ `probe` 块（`accepted_by`、`accepted_on`、`stop: {window_days, max_loss}`、`review_after_days`）。书级判定是 REJECT 也能跑，但 `probe` 块还要写明 `accepted_despite: REJECT` 与 `reason`（D-029）。启动时核对报告种类、对象与 fraction；缺任何一项 `beidou live run` 拒绝启动。当前：`flow_short`（flow 只做空，1/3 预算）。它引用的书级报告判 REJECT，靠 `accepted_despite` 运行。止损是 30 天归因 P&L ≤ −2% 权益（`max_loss: 0.02`）。复审期 30 天，自 `accepted_on` 起算，到期时日报标 REVIEW_DUE。

## 主机时钟漂移（2026-09-04 实测到 −3,612 秒）

检测：

```bash
python3 -c "import time,json,urllib.request;s=json.load(urllib.request.urlopen('https://fapi.binance.com/fapi/v1/time'))['serverTime'];print('drift %.1f s' % (time.time()*1000-s)/1000)"
```

影响与不影响：

- **不影响下单**：REST 客户端会自己测出偏移并写进 `clock_offset_ms`（实测 3,611,691），签名请求照常成功。
- **不影响过期护栏**：`expected_bar_ms - latest_bar_ms > stale_bars_max × interval` 是单向判断，慢钟只会让差值为负。
- **不影响唤醒时刻**（当偏移接近整数个 interval 时）：本地 bar 边界与真实 bar 边界落在同一批真实时刻，循环仍在每个真实整点后不久运行，用的是刚收盘的那根真实 bar。
- **影响记录**：`cycles.jsonl` 的 `bar` / `bar_open_ms` 与 `heartbeat.at` 会整体偏移，`bar` 与 `as_of_ms` 不再一致（`as_of_ms` 是真实的最新闭合 bar，可用它交叉核对）；日报的 UTC 日切也跟着偏移。
- 校正主机时钟需要操作者本人（系统设置 / 需要密码），Agent 不做这件事。校正后重启循环即可，重启是幂等的。

## 排障

- 心跳超时：`beidou live status --check`；看 `~/Library/Application Support/beidou/live.err.log`。
- **巡检的 status 与 report 两格一起失败，告警末三行是 Python traceback，verify 那格照常 ok**：报告层 import 时坏了。循环不 import 报告层（2026-09-28 起），照常交易；不需要重启，重启也修不好。代价在巡检这边：`live status --check` 在报告层那三个读数处就退出，时钟、两个 digest、心跳与成功率这一小时都没查。循环活没活，看 `beidou live status` 先印出的心跳 JSON，报告层坏了它照样印，报错在它后面。修好报告层并快进主 checkout，下一个 :10 两格自己转绿。
- 连续 12 个周期失败触发熔断（`beidou_live/engine.py` 的 `breaker_stop`）。告警送达就以 0 退出，launchd **不会**再拉起（`KeepAlive.SuccessfulExit=false`）；恢复是一次重启，按上文「改了 registry / profile 之后」的窗口与纪律做。没有任何通道收下告警时才非零退出，launchd 60s 后拉起。失败之间循环自己按指数退避，上限 1 小时。根因通常是网络或 -1021 时钟漂移（客户端自动重同步）。
- `cycles.jsonl` 每周期一行：`targets`、`orders`（含 `note`：`PARTICIPATION_CAPPED` / `MARGIN_SCALED`）、`exit_events`、`throttle`、`universe_update`、`skipped`。
- `state.json` 的 `exit_states`（入场价/极值/冷却）、`equity_hwm`、`universe`、`leaving` 在重启后恢复；入场价以交易所为准。
- `heartbeat.json` 的 `history_bars`（当前 1,442）与 `external_flows`；`cycles.jsonl` 的 `external_flows`（`rebaselined: true` = 该周期发生了非交易性现金流，日起点 / 高水位已重置，日报 drift 跳过该 bar）。
- 账户在 demo UI 里被重置后不需要任何操作：下一周期自动重建仓位；`beidou report daily` 的 External cash flows 段显示金额。
- `state.json.last_contributions` 是 NO_ACTION 的 hold 种子（D-022），不要手动删除；删除等于把所有未触发信号的仓位归零一次。
- `beidou live verify`：contributions 必须逐币复现（`ok: true`）；`target_diffs` 非零只是提示——exit overlay / 节流 / 护栏在模型之后动作。`bar_matched: false` 说明 `state.json` 来自另一根 bar，等下一周期再跑。2026-09-04 01:00Z 的实测：两本书差异均为 0.0。
- `cycles.jsonl` 的 `gross_before` 自 D-023 起按 `positionRisk` 的仓位求和；此前恒为 0（账户报文不带 positions 数组），满仓也显示为空仓。
- 时钟漂移下的两个工具（见上文《主机时钟漂移》）：`beidou live status --check` 会探测交易所服务器时间并在偏差超过 `--max-skew-seconds`（默认 60s）时非零退出；`beidou live verify` 以 `as_of_ms`（数据自带的 bar）而不是 `state.last_bar_ms`（本机时钟写的标签）为准比对，并在 `bar_label_skew_ms` / `clock_note` 里给出两者的差。
- **每周期的时钟测量**（D-023 / D-025）：循环每周期用行情端口已有的服务器时间调用测一次偏差，写进 `cycles.jsonl.clock` （`skew_ms` / `alignment_ms` / `whole_bars` / `jumped`）与心跳。**主机时钟是基准**，所以恒定的整数 bar 偏移不告警；只有对齐误差超过 `guards.max_bar_alignment_seconds`（默认 60）或发生跳变才告警，且是边沿触发。探测失败不影响周期。
- 当前实测：偏移 −3,612 s ≈ 整 1 根 bar，对齐误差 12 s，属于可接受状态；循环交易的始终是刚收盘的真实 bar，`cycles.jsonl` 的 `bar` 标注会比 `as_of_ms` 早 1 小时（前者出自本机时钟，后者出自数据），日报按 `as_of_ms` 分桶因而不受影响。
- **定时数据刷新**：`deploy/run_data.sh` 跑 `data sync` + `data spot` + `data metrics` + `data pool refresh`（`data spot` 2026-09-09 加入，`data metrics` 2026-09-27 加入；每日 01:20，`com.beidou.data.plist`）。2026-09-28 起最后再跑 `pytest -m archive`，失败推送，见「归档专属测试告警」一节。`data sync` 同步三类名字：24h 成交额前 2N、`universe.json` 里的池子、上次刷新刚移出的名字。后两类 2026-09-23 加入：此前只看 24h 排名，LSKUSDT 还在池里，K 线却停在 09-18。在此之前研究数据没有任何定时刷新，审计时落后 16 小时且有池成员完全没有本地数据——验证因此跑在「比被验证的书更早结束」的数据上。只读，不碰账户。`data metrics` 不带参数：取快照 store 里的全部币，截到昨天，每个币从自己的水位续传。M-011 每小时拿这份归档和快照比；此前归档只在 09-09 手动灌过一次，停在 09-07，见 RESEARCH_LOG「M-011 读了十九天的 09-07」一节。
- **定时健康检查**：`deploy/run_check.sh` 跑 `live status --check`、`live verify --check`、`report daily --check` 与 `data pool lag --check`（成员表落后，见上文「成员表落后告警」），失败推送到告警 webhook；`deploy/com.beidou.check.plist` 每小时 :10 触发。**装载它是操作者的动作**（上表命令）；只读，不写交易所。`report daily` 不带 `--date` 时渲染最新一行周期行所在的 UTC 日（`newest_day`），不是主机时钟的今天：00:10Z 那次给前一天定稿，含 23:00 那根 bar；当天的文件从 01:10Z 起才有。所以 `reports/daily/D.json` 最后写于 D+1 日 00:10Z，只差 01:00:2x 才写的归因行（每天 00:00 那次资金费结算）。这条规则生效（合入并快进主 checkout）之前写的归档，最后写于 D 日 23:10Z，缺 23:00 那根（RESEARCH_LOG 2026-09-28「归档日报缺每天 23:00 那根 bar」一节）。
