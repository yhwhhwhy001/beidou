# 面向生产的重构方案 · 执行手册（可执行版）

2026-09-28。这是 `2026-09-28-production-refactor-deep-analysis.md`（#201）经 Opus 5.5 审查 PIVOT、操作者六条裁定（#203，§14）之后的**执行版**：每个工作包写到文件、行号、改动内容、测试断言、验收命令、谁合并、何时生效。行号以 `origin/main adb8fda4` 为准；执行时先 `git fetch` 再核一遍行号，并行会话每天合入约 6 个 PR。

**读法**：§1 总览一眼看全；§2 是 PR 顺序；§3 每个工作包一节，拿起来就能开工；§4 是操作者要做的事，带命令；§5 是对分析文档的四处更正；§6 是「执行完毕」的定义。

## 0. 前提与不变量（每个 PR 都适用）

| 项 | 规则 | 出处 |
| --- | --- | --- |
| 分支与 worktree | 从最新 `origin/main` 开分支，在独立 worktree 里改；不在主工作树切分支 | CLAUDE.md「并行工作」 |
| 四道门 | 用主 checkout 的 venv 绝对路径：`/Users/maguannan/beidou/.venv/bin/ruff format --check .`、`… ruff check .`、`… mypy`、`… python -m pytest -m "not network" -p no:cacheprovider`。**不加 `-x`，不再加 `-q`**（addopts 已含一个 `-q`） | CLAUDE.md、memory |
| D-PR05 验证协议 | 两条构造测试绿；PR 前后 `construction_fingerprint`、`registry_digest`、`policy_digest` 逐字相同（`beidou live status --check` 读心跳）；触碰 `beidou_live/report_*` 或 `reports.py` 的 PR 加 #135 协议（实盘状态快照上产物逐字节相同，脚本 `scratchpad/reports_split_byte_identity.py` 的形态）；`beidou live verify --check` 差异 0 | 分析 §6.4 D-PR05 |
| D-PR06 合并规则 | 卫生批（§3.1–3.6）不改任何控制，CI 绿即 `gh pr merge <n> --auto --merge`；**治理类（§3.7–3.11）由操作者合并**，agent 只开 PR | 分析 §6.4 D-PR06 |
| 不动的东西 | `beidou_alpha` 一行不改；`Policy` 任何字段；三个 digest 哈希的任何值；`trials.jsonl`；`.beidou/`；`beidou_exchange/guard.py`；任何日期常量；不新建 `~/Library/Application Support/beidou/env.sh` | 分析 §8.2、RISK-PR10 |
| 重启 | 只由操作者按 RUNBOOK 纪律做（整点后 5–50 分钟，先跑两条构造测试）；需要重启才生效的 WP（C6、R1）**搭同一次重启** | CLAUDE.md「重启实盘循环」、HC-4 |
| PR 描述 | 写「何时生效」（合入即 / 主 checkout 快进后 / 操作者装载后 / 下一次按纪律重启）与验收读数；开完立即 `mcp__ccd_pr__set_monitor`（auto_fix = address_comments = true，auto_archive 关） | CLAUDE.md「CI 监控开关」 |
| ratchet | 抬顶只在写理由的 commit 里；WP-C1 落地前理由仍写在常量旁，落地后写进 `docs/SOURCE_BUDGET_LOG.md` 并在常量旁留编号 | CLAUDE.md「改 ratchet 要带理由」（WP-C1 改写它） |
| 合入不等于生效 | 日报侧改动要等主 checkout 快进（操作者动作，见 §4）；循环侧要等重启 | RUNBOOK |

## 1. 总览

| # | 工作包 | 类型 | 触碰 | 估行数（净）/ 抬顶 | 谁合并 | 生效 | 依赖 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3.1 | WP-C7 接线点类型 | 卫生 | `beidou_cli/live_cmd.py:326` | ±0 / 否 | agent | 合入即（mypy 门） | 无 |
| 3.2 | WP-C6 armed 进程的 import 闭包 | 卫生 | `beidou_live/risk_budget.py`、`report_risk.py:60`、`engine.py:49-50`、新测试 | live ±0，tests +40 / 否 | agent | 下一次按纪律重启 | 无 |
| 3.3 | WP-C9 profile 键读者表 | 卫生 | `beidou_live/config.py`（+约 25）、新测试（+约 70） | live +25 / 用 headroom（40） | agent | 合入即 | 无 |
| 3.4 | WP-C8 exits 同输入测试 | 卫生 | 新测试（+约 110） | 0 源码 / 否 | agent | 合入即 | 无 |
| 3.5 | WP-P4 归档专属测试归位 | 卫生 | 7 条测试各 +1 装饰器、`pyproject.toml:117`、`deploy/run_data.sh`（+12）、`beidou_live/report_data.py`（+20）、新测试 | live +20 / 用 headroom | agent | 夜间 job：主 checkout 快进后的下一个 01:20；日报：快进后 | 无 |
| 3.6 | WP-P3 日期开关登记表 | 卫生 | `beidou_governance/calendar.py`（新，+约 130）、`beidou_cli/governance_cmd.py`（+约 40）、`beidou_live/report_governance.py`（+约 25）、两条新测试 | governance +130（抬顶）、cli +40（抬顶）、live +25 / 是 | agent | 命令合入即；日报快进后 | 无 |
| 3.7 | WP-C1 ratchet 记录搬迁 | 治理 | `tests/architecture/test_source_budget.py`（−约 4,300）、`docs/SOURCE_BUDGET_LOG.md`（新）、新测试、`CLAUDE.md:214-218`、RESEARCH_LOG 行号引用 | tests −4,300 +30；docs +4,300 / 否 | **操作者** | 合入即 | 排在 3.8 之前 |
| 3.8 | WP-C2 headroom 政策 | 治理 | `test_source_budget.py`（+约 30；一次按政策重定七个顶）、`CLAUDE.md` | 是（一次，理由 = 本政策） | **操作者**（先确认数字） | 合入即 | 3.7 |
| 3.9 | D-PR03 预算改只记录 + 周报排 job | 治理 | `test_source_budget.py:4374-4383`、`beidou_live/report_governance.py`（+约 30）、`deploy/com.beidou.weekly.plist` + `run_weekly.sh`（新） | live +30 / 用 headroom 或抬 | **操作者**（装载 plist 也是他） | 装载后每周日 | 无 |
| 3.10 | WP-R1 宿主外告警（D-P4 重开第一半） | 治理 | `beidou_live/deadman.py`（新，+约 45）、`engine.py`（+约 8）、`deploy/run_check.sh`（+6）、`docs/RUNBOOK.md`（+约 30）、`.gitleaks.toml`（+1 规则）、三条新测试 | live +53（抬顶） / 否 | **操作者** | 循环侧：搭一次重启；巡检侧：快进后 :10 | §4 的账号与变量先就位 |
| 3.11 | WP-P6 mainnet 准入设计文档（含生产定义） | 文档 | `docs/MAINNET_READINESS.md`（新，约 +280） | 0 代码 | **操作者** | 合入即 | 无 |
| 3.12 | WP-A1 数据族 parity 仪器 | Should | `beidou_live/report_data.py`（+约 80）、新测试 | live +80（抬顶） | agent | 快进后 | 3.5 之后（同一文件） |
| 3.13 | GAP-PR08 / GAP-PR10 两项零 ledger 测量 | 测量 | `scratchpad/`（入库）、RESEARCH_LOG 一节 | 0 | agent | — | 无 |
| §4 | 操作者清单 | 操作者 | `~/.zshrc`、第三方服务、CLAUDE.md 凭据句、Q2b 数字、BNX、一次重启、装 plist | — | — | — | — |

## 2. PR 顺序

- **第一波（可并行，互不触碰同一文件）**：3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6。六个 PR，各自开监控、CI 绿即合。3.5 与 3.12 都改 `report_data.py`，3.12 排在 3.5 合入之后。
- **第二波（操作者合并；串行）**：3.7 先于 3.8（同一文件：先搬走注释，再加政策与一次重定顶，避免两个 PR 在 4,000 行注释上打架）；3.9 独立；3.11 独立。
- **第三波**：§4 的账号与变量就位后开 3.10；3.2 与 3.10 合入后由操作者做**一次**按纪律的重启（两者一起生效）。
- **随时**：3.13 两项测量，读数进 RESEARCH_LOG；GAP-PR10 的结果决定要不要写「窗口内重试」的预登记。

## 3. 工作包规格

字段：目标 · 改什么 · 不改什么 · 测试 · 验收 · 生效与回滚 · 人类确认点。

### 3.1 WP-C7 接线点类型

- **目标**：`beidou_cli/live_cmd.py:326` 的 `venue: Any` 让 mypy 在唯一的接线点不对 `BinanceUsdmVenue` / `PaperVenue` 做 `Venue` 的结构检查（子代理 A §8）。
- **改什么**：`:326` `venue: Any` → `venue: Venue`；`from beidou_live.ports import Venue`（`live_cmd.py:61` 一带已有 `PaperVenue` 的 import，放在同组）。若 `Any` 不再被别处引用，`typing` 的 import 随之收缩（ruff 会报）。
- **不改什么**：`ports.py` 的 Protocol；两个 venue 实现——除非 mypy 报缺方法。
- **测试**：mypy 门即测试。若 mypy 报 `PaperVenue`/`BinanceUsdmVenue` 不满足 `Venue`，那是一条真发现：在同一 PR 里补齐实现（或把多余的 `VenueProbes` 可选项留给 `getattr`），**不许改回 `Any`**。
- **验收**：`mypy` 绿；`tests/live/test_the_ports_describe_what_the_engine_reads.py` 绿；PR 描述写 mypy 报了什么。
- **生效与回滚**：合入即；revert。
- **人类确认点**：无。

### 3.2 WP-C6 armed 进程的 import 闭包

- **目标**：`engine.py:49` `from beidou_live.reports import collateral_share` 把整个报告层（`reports.py` 再导出 8 个 `report_*` 模块，5,506 行）拉进 armed 进程的 import 闭包；报告层任何一处 import 时异常都能让循环起不来。
- **改什么**：
  1. `collateral_share`（`beidou_live/report_risk.py:60-78`，纯函数，无依赖）**逐字**搬到 `beidou_live/risk_budget.py`（引擎 `:50` 已 import 该模块；`risk_budget.py` 只 import `construction`、`cycle_record`，不会成环）。
  2. `report_risk.py` 改为 `from beidou_live.risk_budget import collateral_share`（保留地址，同一对象；`reports.py:126` 的再导出链不动，`test_the_report_layer_kept_its_addresses.py` 的「同一对象」断言照旧成立）。
  3. `engine.py:49` 删掉，`:50` 改为 `from beidou_live.risk_budget import RiskBudgetParams, attributed_drawdown_state, collateral_share`。
  4. 新测试 `tests/live/test_the_armed_process_does_not_import_the_report_layer.py`：用 `subprocess` 起一个干净解释器 `python -c "import beidou_live.engine, sys; print(sorted(m for m in sys.modules if m.startswith('beidou_live.report')))"`，断言输出为 `[]`（子进程，因为 pytest 进程里报告层早已被别的测试 import）。
- **不改什么**：任何报告产物；`reports.py` 的 `__all__`。
- **测试**：上面那条 + 既有 `test_the_report_layer_kept_its_addresses.py` 四条 + 两条构造测试。
- **验收**：#135 协议——在实盘状态快照上跑 `report daily` 的 render 路径（不发 webhook）前后逐字节相同；`beidou live status --check` 前后 `construction` 相同。
- **生效与回滚**：引擎文件变了，**搭下一次按纪律的重启**；PR 描述与 RUNBOOK 写明；revert。
- **人类确认点**：HC-4（重启由操作者做）。

### 3.3 WP-C9 profile 键读者表

- **目标**：`config/live.demo.yaml` 的 65 个键里，`risk_budget.*` 的 14 个只被日报路径读（`live_cmd.py:868`），引擎的 R8 尺子用 `RiskBudgetParams()` 默认值（`engine.py:1793`）；今天两者相等，但这是一组循环不读的旋钮，且 `test_the_digest_sees_every_live_knob.py` 不覆盖（E-PR35）。
- **改什么**：
  1. `beidou_live/config.py` 顶部加两张表（+约 25 行）：
     ```python
     #: 每个 profile 键（`section.leaf`）的读者。测试 `test_every_profile_key_has_a_reader` 逐键核对：
     #: 值是读它的模块名，模块源码里必须出现 `"leaf"` 字面量；REPORT_ONLY 的键只供 `report daily`，
     #: 引擎不读——改它们不改循环（E-PR35，2026-09-28）。
     PROFILE_KEY_READERS: dict[str, str] = {
         "venue.rest_url": "beidou_live.config", ...,
         "alerts.webhook_url": "beidou_cli.live_cmd", "alerts.webhook_url_2": "beidou_cli.live_cmd",
         "paths.state_dir": "beidou_live.config", "paths.reports_dir": "beidou_cli.live_cmd",
         "profile": "beidou_cli.live_cmd", "registry": "beidou_live.config", "universe": "beidou_live.config", "costs": "beidou_live.composition",
     }
     REPORT_ONLY_KEYS: frozenset[str] = frozenset({"risk_budget.deescalate_at", ... 14 个 ...})
     ```
     （`risk_budget.min_liq_distance` 在 `config.py:153` 被引擎读，不在 REPORT_ONLY 里。）
  2. 新测试 `tests/live/test_every_profile_key_has_a_reader.py`（+约 70）：`yaml.safe_load` 读 shipped profile 展成 `section.leaf`；断言每个键要么在 `PROFILE_KEY_READERS`（且 `importlib.import_module(reader)` 的源码里出现 `"<leaf>"` 字面量），要么在 `REPORT_ONLY_KEYS`（且 `beidou_live/risk_budget.py` 的 `RiskBudgetParams` 有同名字段）；两张表里没有多余的键（表 ⊆ yaml 键）。再一条：在 yaml 副本里加一个 `guards.made_up` 键，断言测试红（用 `tmp_path` 写副本、参数化调用同一断言函数）。
- **不改什么**：任何键的值；`LiveConfig`。
- **验收**：测试绿；`REPORT_ONLY_KEYS` 恰好 14 项；日报逐字节相同（未改产物）。
- **生效与回滚**：合入即；revert。
- **人类确认点**：无。

### 3.4 WP-C8 exits 同输入测试

- **目标**：`ExitOverlay.apply`（`beidou_live/exits.py:25`，逐 symbol、以场地持仓为方向真值、首次入场价固定）与 `apply_exits`（`beidou_alpha/overlays/exits.py:469`，整帧）都基于 `exit_step`，但没有一条测试把两者放在同一输入上比（子代理 A §3）。
- **改什么**：新测试 `tests/live/test_the_live_overlay_is_the_backtest_overlay_on_one_symbol.py`（+约 110）：
  - 合成一个单币 `close` 序列（沿用 `tests/alpha/test_the_vectorised_exit_engine_is_the_same_machine.py` 的 fixed-seed 面板生成器）与一列决策权重（含 0 → ±w 进入、同向加减仓、反向翻转）。
  - 回测侧：`apply_exits(weights_frame, close_frame, params)` 得逐 bar 调整后权重与事件。
  - 实盘侧：逐 bar 调 `ExitOverlay(params, interval_ms).apply(targets={sym: w_t}, positions={sym: Position(qty=sign(上一 bar 调整后权重), entry_price=首次入场价)}, bars={sym: 截至 t 的 frame}, states=上一 bar 的 states, bar_open_ms=t)`；`Position` 的 `entry_price` 按「首次入场价，同向加减仓不重锚」构造——这正是 `_reconcile` 的规则（E-047），不是新假设。
  - 断言：两侧调整后权重按 `_bit_for_bit`（`tests/alpha/test_causality.py:26`）相同；事件类型序列相同。
  - **范围写死**：不含 D-045 的「退出单没成交、场地上仍持仓」路径——那是实盘独有的语义，另一条既有测试（`test_a_failed_exit_does_not_re_enter_the_position.py`）守着；参数集用 `PARAM_SETS` 里的 shipped 与 trailing 两组。
- **验收**：测试绿；若红，先判是构造差异还是语义差异，语义差异写进 RESEARCH_LOG 而不是改任一侧。
- **人类确认点**：无。

### 3.5 WP-P4 归档专属测试归位

- **目标**：7 条读 `.beidou/` 的测试在 CI 与 worktree 里 skip、只在主 checkout 上跑，`test_the_fixtures_are_the_archive_verbatim[BNXUSDT_2023-02-22.json]` 自 09-25 起红、没人看见（E-PR08）。给它们一个执行位置，不改夹具、不改归档。
- **改什么**：
  1. `pyproject.toml:117-120` markers 加一行：`"archive: reads .beidou/ on the operator's machine; skipped elsewhere and run by the nightly data job"`。
  2. 给 7 条测试加 `@pytest.mark.archive`（执行前先 `grep -rn -E 'ROOT / "\.beidou"|\.beidou/' tests --include='*.py'` 复核清单）：`tests/live/test_bar_sanity_is_seen_and_never_traded_on.py:112`（parametrize 上方）、`tests/data/test_dataset_gate.py:110`、`tests/live/test_a_gap_is_asked_for_once_and_never_filled_in.py:374`、`tests/live/test_the_market_benchmark_cannot_be_built_with_hindsight.py:249`、`tests/live/test_a_restart_does_not_take_over_a_bar_a_failed_cycle_lost.py:116`、`tests/governance/test_the_record_reaches_the_state_exactly_once.py:302`、`tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py:123`（子代理 C §3 的清单，行号以当时为准）。**默认门 `-m "not network"` 不改**：marker 是叠加，这些测试在默认门里的行为（skip / 跑）与今天一样。
  3. `deploy/run_data.sh` 在 `data status` 之后、`exit "$fail"` 之前加：
     ```bash
     echo "[$(stamp)] archive tests"
     if output="$("$REPO/.venv/bin/python" -m pytest -m archive -p no:cacheprovider 2>&1 | tail -n 15)"; then
       echo "[$(stamp)] ok   archive tests"; echo "$output" | tail -n 1
     else
       echo "[$(stamp)] FAIL archive tests"; echo "$output"; fail=1
     fi
     ```
     它跑在夜间 job 里，失败让 `run_data.sh` 以 1 退出；`com.beidou.data` 的日志有记录。**推送告警**：`run_data.sh` 没有 `notify`，加一个与 `run_check.sh:33-60` 同形的 `notify`（同一 `WebhookAlerts`、同一 dedup 文件、key `archive-tests`）——这是 +约 12 行，不要复制第三份逻辑，直接照 `run_check.sh` 那段调 `beidou_live.alerts`。
  4. `beidou_live/report_data.py` +20：日报一节「归档专属测试」读 `~/Library/Application Support/beidou/data.stdout.log` 最后一次 `archive tests` 的行，印 `通过 / 失败 / 未跑（job 未装载或今天还没到 01:20）`。
  5. 新测试：`tests/cli/test_the_nightly_job_runs_the_archive_tests_and_the_default_gate_is_unchanged.py`——文本检查 `run_data.sh` 含 `pytest -m archive`；`pytest --collect-only -m "not network" -q` 的用例数与加 marker 前相同（把加前的数写进测试常量，PR 描述里说明取法）。
- **不改什么**：任何夹具、任何归档、`test_the_fixtures_are_the_archive_verbatim` 的断言（HC-8 由操作者定哪边对）。
- **验收**：`pytest -m archive` 在 worktree 里 7 条 skipped、在主 checkout 上各有确定结果；次日日报有那一节；夜间 job 日志有 `archive tests` 行。
- **生效与回滚**：主 checkout 快进后的下一个 01:20；revert。
- **人类确认点**：HC-8（BNX 哪边对，独立于本 PR）。

### 3.6 WP-P3 日期开关登记表

- **目标**：日期开关散在四类文件里，09-25 靠一份 63 KB 的分析拼出「三个开关同时翻转」（E-PR16）。
- **改什么**：
  1. `beidou_governance/calendar.py`（新，+约 130）：`@dataclass(frozen=True) class DatedSwitch: at: datetime; source: str（文件:行或 yaml 路径）; reader: str（谁读它：test / job / command / yaml check）; consequence: str; status: Literal["pending","inert","past"]`；`def dated_switches(*, repo: Path, registry_path: str = "config/alpha_registry.yaml", now: datetime) -> list[DatedSwitch]` 从这些位置读（**读文件，不 import 测试模块**）：
     - `deploy/run_live.sh` 的 `BRIDGE_UNTIL="…"`（正则），reader `run_live.sh`，consequence「到期后 armed 启动不带 --allow-unvalidated」，status：#163 之后 inert（registry 已指向 WEAK_PASS 证据；判定方法：读 registry tsmom 的 `evidence.verdict` ∈ {PASS, WEAK_PASS} → inert）。
     - `beidou_governance/policy.py` 的 `SINGLE_WINDOW_MINE_OPENING_ENDS`（import `beidou_governance.policy` 读常量），reader `tests/governance/test_the_single_window_mine_opening_is_returned.py`，status：`Policy().max_mine_rounds_per_window == STANDING_MINE_ROUNDS` → inert。
     - `governance/reopen.yaml` 每条 `check: date_after` 的 `args.date`（今天三条：`:187` 10-03、`:372`/`:439` 10-13），reader `beidou governance reopen`。
     - `governance/window_changes.yaml` 每条 `earliest_window`（`:19`、`:47` 10-03，`:81` 09-27 已 past），reader `beidou governance window`。
     - registry 每个 **enabled** 且带 `probe` 块的策略：`accepted_on + review_after_days`（`:394`/`:399` flow → 2026-10-03），reader 日报 `REVIEW_DUE`。
     - `tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py` 的 `FREEZE_ENDS`（正则读文本），reader 该测试，status past。
  2. `beidou_cli/governance_cmd.py` +约 40：`beidou governance calendar [--days 60] [--json]`，按日期升序打印 `date | source | reader | consequence | status`。
  3. `beidou_live/report_governance.py` +约 25：日报「未来 7 天日期翻转」一节（无翻转时印「无」）。
  4. 新测试 `tests/governance/test_every_dated_switch_is_on_the_calendar.py`（+约 50）：钉时钟 2026-10-06 跑 `dated_switches`，断言含 10-13 的两条 `date_after`；再用 `re.finditer(r"20\d\d-\d\d-\d\d")` 扫 `deploy/*.sh`、`beidou_governance/policy.py`、`governance/*.yaml` 里**出现在赋值或 `date:` 值位置**的日期字面量，断言每一个都在 `dated_switches` 的 source 集合里（注释里的日期不算：只匹配 `=`、`:` 后的引号内日期）；另一条 `tests/governance/test_the_calendar_is_read_not_remembered.py`：往一个 tmp 副本的 `reopen.yaml` 加一条 `date_after: 2026-11-01`，断言登记表里出现它。
- **不改什么**：任何日期的值；不给任何开关加自动动作（登记表只读只报）。
- **验收**：`beidou governance calendar` 今天列出 10-03（reopen `:187`、window_changes ×2）与 10-13（reopen ×2、bridge inert）；10-06 起日报出现 10-13 那两条；两条测试绿。
- **生效与回滚**：命令合入即；日报快进后；revert。
- **抬顶**：governance +130（headroom 40）、cli +40（headroom 26）——同一 PR 抬并写理由（WP-C1 落地前理由仍写在常量旁）。

### 3.7 WP-C1 ratchet 记录搬迁（Q2a = 是）

- **目标**：`tests/architecture/test_source_budget.py` 4,395 行里 4,316 行是抬顶理由注释；搬到 `docs/SOURCE_BUDGET_LOG.md`，测试文件缩到约 200 行，冲突面缩小；**触碰频率不变**（KILL-02(b)）。
- **改什么**：
  1. 迁移脚本 `scratchpad/source_budget_log_migration.py`（入库，它是这次搬迁的记录）：读 `CEILING = {`（`:1954`）到 `}` 之间的文本；按包条目切块——每个 `"beidou_x": N,` 行之前连续的 `#` 行是该条目的理由块；`CEILING` 之上、`PLAN_BUDGET` 之下的长注释是「总述」块。输出 `docs/SOURCE_BUDGET_LOG.md`：`# source budget ratchet 抬顶记录` → `## 总述（原文）` → 每包一节 `## beidou_live` 等，块内原文**逐字**（含 `# ` 前缀去掉、缩进保留），块与块之间以原文顺序排列，不合并、不改字。脚本最后把 `docs/SOURCE_BUDGET_LOG.md` 反向拼回一份 `test_source_budget.py` 并 `diff` 原文——**必须为空**（FM-PR5），差异输出进 PR 描述。
  2. 测试文件保留：模块 docstring、`PACKAGES`、`PLAN_BUDGET`、`CEILING`（每条目上方留一行 `# 抬顶记录：docs/SOURCE_BUDGET_LOG.md#beidou-live`）、`_lines`、两条测试。
  3. 新测试 `tests/architecture/test_every_ceiling_points_at_its_record.py`：每个 CEILING 条目上一行是 `# 抬顶记录：docs/SOURCE_BUDGET_LOG.md#<anchor>`，且 `docs/SOURCE_BUDGET_LOG.md` 含该 `## <包名>` 标题；`docs/SOURCE_BUDGET_LOG.md` 存在且 ≥ 4,000 行（防止被「整理」）。
  4. `CLAUDE.md:214-218`「改 ratchet 要带理由」改为：
     > `CEILING_SECONDS` 与 source budget 表都是 ratchet：**抬顶只允许发生在写明理由的那个 commit 里**。理由写进 `docs/SOURCE_BUDGET_LOG.md` 对应包的一节（带日期、增量、测量数据），常量旁只留指向那一节的一行。2026-09-28 之前的理由都在那个文件里，原文逐字。不要为了过顶把注释 golf 掉——第九次抬顶的注释记下了原因：a ratchet with no headroom stops being a ratchet and becomes a tax on the first honest change, paid in deleted comments。
  5. RESEARCH_LOG 里引用 `test_source_budget.py:<行号>` 的地方（先 `grep -c 'test_source_budget.py:' docs/RESEARCH_LOG.md`）**不改原文**，在 `docs/SOURCE_BUDGET_LOG.md` 顶部放一张「旧行号 → 节」对照表（脚本生成）。
- **不改什么**：任何 CEILING 值；任何理由的字。
- **验收**：反向拼回 diff 为空（PR 描述贴命令与输出）；四道门；`wc -l tests/architecture/test_source_budget.py` ≤ 300。
- **生效与回滚**：合入即；revert（一次）。
- **人类确认点**：HC-5，**操作者合并**。选并行会话安静的时段开 PR（RISK-PR04）。

### 3.8 WP-C2 headroom 政策（Q2b = 好；数字待确认）

- **目标**：把「惯例约 40 行」写成政策，防囤积；代价是约 78% 的历史抬顶事件不再逐次写理由（分析 §14.3 的测量）。
- **改什么**（在 3.7 合入之后）：
  1. `test_source_budget.py` 加：
     ```python
     # 2026-09-28 操作者裁定（Q2b）：headroom 政策。小包维持 40 行惯例，大包按顶的 1% 放宽；
     # 08-28 起 420 次分包抬顶里 328 次（78%）落在它之内。防囤积：任何一次抬顶最多抬到「实测 + 政策」。
     HEADROOM_FLOOR = 40
     HEADROOM_RATE = 0.01
     def headroom_policy(ceiling: int) -> int:
         return max(HEADROOM_FLOOR, round(HEADROOM_RATE * ceiling))
     def test_no_ceiling_hoards_more_headroom_than_the_policy_allows() -> None:
         for package in PACKAGES:
             assert CEILING[package] - _lines(package) <= headroom_policy(CEILING[package]), package
     ```
  2. 同一 commit 把七个顶重定到 `实测 + headroom_policy(顶)`（理由 = 本政策，写进 `docs/SOURCE_BUDGET_LOG.md` 的「2026-09-28 政策」一节）。
  3. `CLAUDE.md` 那一段加一句：「headroom 由 `headroom_policy` 定义，抬顶抬到『实测 + 政策』为止；再往上就是囤积，测试会红。」
- **不改什么**：抬顶仍只在写理由的 commit 里。
- **验收**：新测试绿；`ceiling − measured ≤ policy` 七包成立。
- **人类确认点**：**数字（1%、40）由操作者在 PR 上确认**；操作者合并。

### 3.9 D-PR03 预算改只记录（Q5 = 是）+ 周报排 job

- **目标**：`test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined`（`:4374-4383`）每天断言「已越界」却不推动决定；操作者已裁定增长率是他要的。M-PR01（非 alpha 增长率、整周 alpha 投入占比）要有一个记录它的位置。
- **改什么**：
  1. 该测试改名 `test_the_plans_budget_is_a_record_not_a_gate`，只断言 `PLAN_BUDGET` 字面量未变（它是历史记录）；docstring 写「2026-09-28 操作者裁定 Q5=是：非 alpha 的增长率被接受，缺口不再断言」。
  2. `beidou_live/report_governance.py` +约 30：周报「Plan budget gap」一节：七包行数、非 alpha 合计、alpha 树占比、近 7 天非 alpha 增长（行/天，`git log --since` 逐包算或读上周周报的数），只印不告警。
  3. `deploy/com.beidou.weekly.plist`（每周日 03:00）+ `deploy/run_weekly.sh`（`beidou report weekly`，同 `run_check.sh` 的 env 读法；`report weekly` 不发 webhook，只写 `reports/weekly/`），`tests/architecture/test_every_launchd_plist_is_valid_xml.py:37-40` 的清单加它。
- **验收**：测试绿；周报有该节；plist 过 XML 测试。
- **生效与回滚**：操作者装载 plist（§4）；revert。
- **人类确认点**：HC-5，操作者合并并装载。

### 3.10 WP-R1 宿主外告警（Q3 = 重开 D-P4，第一半）

- **目标**：宿主离线时同机巡检也死，告警为 0（E-PR20/37）；09-06 O-X1 的第二通道在本机未配置。按 09-05 DL-Q8 的 ①②⑥⑦ 做告警侧；③④⑤（远端可挂起的 kill switch、`flatten --raw`、只读 key 看门狗主机）是 WP-R2，后置。
- **前置（§4 先做）**：操作者选一个 ping 式 dead-man 服务，建两个 check（`beidou-live`：周期 60 分钟、grace 75 分钟；`beidou-check`：周期 60 分钟、grace 20 分钟），把两个 ping URL 作为 `export BEIDOU_DEADMAN_LOOP_URL=…`、`export BEIDOU_DEADMAN_CHECK_URL=…` 写进 `~/.zshrc`（凭据实际所在处；**不新建 `env.sh`**）；补 `export BEIDOU_ALERTS_WEBHOOK_URL_2=…`。
- **改什么**：
  1. `beidou_live/deadman.py`（新，+约 45）：`def ping(url: str, *, timeout: float = 5.0) -> bool`——`httpx.get`（或 POST，按服务），**吞掉一切异常**、永不抛、返回是否 2xx；`def loop_url() -> str` 读 `os.environ.get("BEIDOU_DEADMAN_LOOP_URL", "")`。不进 `LiveConfig`（它是令牌不是旋钮，`test_the_digest_sees_every_live_knob.py` 不该看见它）。
  2. `engine.py` `_finish_cycle`（`:1918` 附近）末尾、`store.heartbeat(...)` 之后 +约 8 行：`if record.get("phase") == "OK" and (url := deadman.loop_url()): record["deadman"] = deadman.ping(url)`——只在 OK 周期发（DL-Q8 ①：推送 OK，不是心跳文件年龄）；ping 结果进 `cycles.jsonl` 那一行（可审计）；失败不影响周期。
  3. `deploy/run_check.sh` 在 `exit "$failed"` 之前 +6 行：`if [ -n "${BEIDOU_DEADMAN_CHECK_URL:-}" ]; then curl -fsS -m 10 --retry 2 "$BEIDOU_DEADMAN_CHECK_URL" >/dev/null || echo "[$(stamp)] dead-man ping failed"; fi`——**无论 `$failed`**，它证明的是「巡检还活着」。
  4. `.gitleaks.toml` 加一条规则匹配所选服务的 URL 形态（例如 `https://<host>/[0-9a-f-]{36}`），并在 `tests/architecture/test_secret_scanning_is_alive.py` 的金丝雀里加一个伪造 URL 必须被抓——避开 memory 记的两个静默坑（RE2 不支持 lookahead；allowlist 的 regex 默认匹配 secret）。
  5. `docs/RUNBOOK.md` +约 30：一节「宿主外告警（D-P4 重开，2026-09-28）」——两个 check 的语义、维护前在服务端暂停 check（> 2 小时的有意停机）、演练步骤（`launchctl bootout gui/$(id -u)/com.beidou.live` → 计时 → 服务端应在 2 个整点 + 15 分钟内推送 → `bootstrap` 回来 → 应推送恢复；巡检 check 用 `bootout com.beidou.check` 同法）、两条 falsifier（循环存活但某周期迟到 < 15 分钟不得触发；巡检死而循环活只触发 `beidou-check`）。
  6. 三条新测试：`test_the_dead_man_ping_never_raises_and_never_blocks`（假 transport 抛异常/超时，`ping` 返回 False 且 < 6 s）；`test_the_loop_pings_only_after_an_ok_cycle`（用 `FakeVenue` 跑一个 OK 周期与一个 ERROR 周期，记录 `record["deadman"]` 只在 OK 行出现；URL 用 `monkeypatch.setenv` 指向本地假服务）；`test_the_check_job_pings_regardless_of_its_result`（文本：ping 段在 `exit "$failed"` 之前且不在任何 `if [ "$failed" …` 分支里）。
- **不改什么**：现有 webhook 告警；`Policy`；`LiveConfig`；构造。
- **验收**：演练一次通过（AC-PR1a）；30 天假阳性 ≤ 2（AC-PR1b）；`cycles.jsonl` OK 行带 `deadman: true`。
- **生效与回滚**：循环侧搭下一次按纪律的重启（与 3.2 同一次）；巡检侧快进后的下一个 :10；revert + 服务端删 check。
- **人类确认点**：HC-1（凭据位置：变量进 `~/.zshrc`）、HC-2（建账号、放 URL）、HC-4（重启）、HC-7（本裁定即重开）；**操作者合并**。

### 3.11 WP-P6 mainnet 准入设计文档（Q1 = B；含生产定义）

- **目标**：Q1=B「目前 B，没问题以后将会是 C」。写设计文档，**零代码，`guard.py` 一字不动**；同时给 D-PR02 的「生产定义」一个落点（分析 §14 遗漏的那一项）。
- **改什么**：`docs/MAINNET_READINESS.md`（新，约 +280），章节：
  1. **生产的三层定义**（D-PR02）：L-A demo 无人值守（读数：M-PR02、M-PR03、M-PR06、M-PR07）；L-B mainnet 小额校准（本文档的准入门）；L-C mainnet 目标资金。当前层：L-A 运行中，L-B 设计中，L-C 未开。
  2. **09-05 已写下的解除条件，原样引**（`docs/analysis/2026-09-05-system-quality-deep-analysis.md` §12.6「真实资金：HOLD 且 DEFERRED」那一段，与附录 D「mainnet pre-flight backlog」的 P0 七项：冲击成本模型 + `vol_target` 重推 + 10 万 USDT 容量门槛；保证金模式断言；全新 `state_dir`；账户空仓空挂单断言；告警 webhook 端到端演练 + 第二通道；密钥——建 `env.sh`（600）并移除 `run_live.sh:18` 对 `~/.zshrc` 的回退、分权 API key、IP 白名单；`--armed` + `max_equity_usdt` 双重确认、host 白名单改码）与 KILL-R13「只在操作者显式重开 mainnet 时启动」。
  3. **B → C 的准入门**：每项一行——今天的读数 / 来源 / Owner / 何时可读（例：M-010 30 天干净窗口从 09-27 重启 #59 起算，最早 10-27；滑点带内 M-Q08；L3 7 天；冲击系数只能在小额真钱上校准，所以 C 的第一阶段本身就是校准，写明资金上限的选法）。
  4. **`guard.py` 放行的实现形态（不实现）**：常量 `ALLOWED_HOSTS` 保持；将来放行 = 新常量 `MAINNET_HOSTS` + 只在「签字文件 `governance/MAINNET_ENABLED` 存在 ∧ 显式旗标 `--mainnet` ∧ profile 名含 mainnet」三者同时成立时并入；任何改 `guard.py` 的 PR 列 HC-3，排除自动合并。
  5. **密钥与 kill-switch 分离**、**成本模型校准方案**（冲击系数用前 N 笔真实成交回归）、**回退**（`--armed` 拒绝 + flatten 路径）。
- **验收**：AC-PR6a——每项准入条件带读数/来源/Owner；`git diff --stat` 里没有 `.py`。
- **人类确认点**：HC-3、HC-9；**操作者合并**。

### 3.12 WP-A1 数据族 parity 仪器（Should）

- **目标**：metrics/spot 列的「实盘覆盖 bars ÷ 读它的信号或挖掘叶的最长 lookback」没有日常读数（C-PR06）。
- **改什么**：`beidou_live/report_data.py` +约 80：对 `metrics_snapshot/` 每列（`sum_toptrader_long_short_ratio` 等）与 `spot_klines/` 的覆盖 bars，除以 `beidou_alpha.mining.expr` 里读该列的叶的最大 `lookback()` 与 `SIGNALS` 里 `needs_metrics/needs_spot` 为真的信号的 `warmup_for(params)`；印比值与「够 / 不够」；口径与 `engine.metrics_refusal` 相同（读同一函数算 `required_bars`）。新测试用合成 store。
- **抬顶**：live +80 写理由（WP-C1 落地后进 `SOURCE_BUDGET_LOG.md`）。
- **生效**：快进后。

### 3.13 两项零 ledger 测量

- **GAP-PR08**（ratchet 文件的冲突成本）：脚本读 `git log --merges --since=2026-08-28` 里的合并提交，对每个合并检查父提交对 `test_source_budget.py` 的三方冲突（`git merge-tree`），数「需要人解冲突」的次数；再读 `gh pr list --state merged --json createdAt,mergedAt,files`，比较 diff 含该文件与不含的 PR 的开到合中位时长。读数进 RESEARCH_LOG；阈值：每周 ≥ 1 次可归因冲突 → WP-C1 的价值成立，否则它只是整洁。
- **GAP-PR10**（失败周期窗口内重试的收益）：读 `.beidou/live/cycles.jsonl` 09-15 起的 7 条 ERROR 行的时刻与错误类型；对每条，看同一 bar 的再平衡窗口（`rebalance_window_seconds`，日报读数 90.66 s）内有没有后续成功的 venue 调用（同一 `cycles.jsonl` 的下一行 OK 时刻、或 `proxy-probe.jsonl` 09-22 前的簇长分布）；估「T 秒后重试一次」能救回的比例。只读；阈值 ≥ 4/7 → 写「窗口内重试」预登记（改实盘行为，另一次裁定）。

## 4. 操作者清单（带命令；全部是仓库外或治理类动作）

| # | 动作 | 命令 / 位置 | 解锁 |
| --- | --- | --- | --- |
| O-1 | CLAUDE.md:23 凭据句改成可核的现状。建议改为：「凭据由 `deploy/*.sh` 读：`~/Library/Application Support/beidou/env.sh` 存在就只读它，否则读 `~/.zshrc` 里的 `export BEIDOU_*` 行。**今天是后者**；`env.sh` 是 mainnet 前置项（09-05 附录 D），建它之前必须把全部 `BEIDOU_*` 变量迁过去，否则下一次重启以 78 退出。」 | 编辑 CLAUDE.md（治理类，你合） | RISK-PR10 关闭 |
| O-2 | 补第二告警通道 | `~/.zshrc` 加 `export BEIDOU_ALERTS_WEBHOOK_URL_2=<第二个 bot 的 URL>`；验证：`bash -c 'source ~/.zshrc; beidou live alert-test'`（命令存在于 `live` 组） | O-X1 的第二通道 |
| O-3 | 建 dead-man 账号与两个 check，URL 进 `~/.zshrc` | `export BEIDOU_DEADMAN_LOOP_URL=…`、`export BEIDOU_DEADMAN_CHECK_URL=…`；**不建 `env.sh`** | 3.10 |
| O-4 | 确认 headroom 数字 | 3.8 的 PR 上留言「1% / 40 行」或改数 | 3.8 |
| O-5 | BNX 2023-02 夹具与归档哪边对 | `beidou data status`（看 BNXUSDT 缺口记录）+ 币安公告；答案写 RESEARCH_LOG | GAP-PR09 |
| O-6 | 一次按纪律的重启（3.2 与 3.10 合入并快进主 checkout 之后） | 整点后 5–50 分钟：`.venv/bin/python -m pytest tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py tests/live/test_construction_identity.py` → `launchctl kickstart -k gui/$(id -u)/com.beidou.live` → 看心跳 `construction` 不变、`cycles.jsonl` OK 行带 `deadman: true` | 3.2、3.10 生效 |
| O-7 | 快进主 checkout（日报侧改动生效） | 一步到位：`git -C ~/beidou merge --ff-only origin/main`（memory：切/快进主 checkout 要一步到位） | 3.3–3.6、3.9、3.12 的日报侧 |
| O-8 | 装载周报 plist | `cp deploy/com.beidou.weekly.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.beidou.weekly.plist` | 3.9 |
| O-9 | 合并治理类 PR | 3.7、3.8、3.9、3.10、3.11 各一个 | — |

## 5. 对分析文档的更正（随本 PR 落）

1. §1 Gate Summary 的 G6 一行：「CLOSED 11、MITIGATED 4、ACCEPTED 2」→ **「CLOSED 12、MITIGATED 4、ACCEPTED 1」**（与 §7.4 逐行计数、§7 Gate Review、Final Kill Decision 一致）。
2. §2.2 C-PR01 的 [R 修订] 末尾加一句指向 §14.6（PARTIAL → SUPPORTED（Medium）的依据是 §14.2 的剖析）。
3. D-PR02「生产定义」的落点：`docs/MAINNET_READINESS.md` §1（本手册 3.11），不另建 `PRODUCTION.md`。
4. M-PR01 的记录器：本手册 3.9（周报「Plan budget gap」一节 + 周日 job）。
5. CLAUDE.md 凭据句：本手册 §4 O-1（操作者动作，附建议文字）。

## 6. 「执行完毕」的定义

- 第一波六个 PR 合入，且 O-7 快进后的第一份日报出现「归档专属测试」「未来 7 天日期翻转」两节。
- 第二波五个治理类 PR 由操作者合入；`wc -l tests/architecture/test_source_budget.py` ≤ 300；七包 `ceiling − measured ≤ headroom_policy`；周报有「Plan budget gap」。
- O-6 那次重启之后：心跳 `construction` 不变；`cycles.jsonl` OK 行带 `deadman: true`；`import beidou_live.engine` 的子进程不含 `beidou_live.report_*`。
- 演练：`bootout` 循环 2 个整点 + 15 分钟内收到宿主外推送（AC-PR1a）。
- 30 天后回填分析 §10.3：M-PR02 = 0；M-PR03 两列；M-PR01 只记录；M-PR07 演练读数。
- 两项测量的读数进 RESEARCH_LOG，各自的阈值判断写明。

## 7. 执行记录（2026-09-28，会话 ce8d8edb）

操作者当日下令「开工，使用多 agent 并行执行」。十个子代理各在独立 worktree 里做一个工作包，只在本地提交；
push、开 PR、`set_monitor`、auto-merge 与冲突都由协调者统一处理。下面只写可观测事实，读数以各 PR 描述为准。

### 7.1 工作包状态（截至 12:30Z）

| # | 工作包 | PR | 状态 | 生效条件 |
| --- | --- | --- | --- | --- |
| 3.1 | WP-C7 接线点类型 | #209 | 已合入 | 合入即（mypy 门） |
| 3.2 | WP-C6 引擎不 import 报告层 | #210 | 已合入 | 下一次按纪律的重启 |
| 3.3 | WP-C9 profile 键读者表 | #211 | 已合入 | 合入即 |
| 3.4 | WP-C8 exits 同输入测试 | #214 | 已合入 | 合入即；查出一处分叉，修法待裁定（7.3） |
| 3.5 | WP-P4 归档专属测试归位 | #212 | 已合入 | 主 checkout 快进后的下一个 01:20（本机） |
| 3.6 | WP-P3 日期开关登记表 | #216 | 已合入 | 命令合入即；日报在快进后 |
| 3.7 | WP-C1 ratchet 记录搬迁 | #218 | **待操作者合并** | 合入即；合并前协调者按那一刻的 main 重跑脚本 |
| 3.8 | WP-C2 headroom 政策 | 未开 | 等 3.7 的去留与 O-4 | — |
| 3.9 | D-PR03 预算只记录 + 周报 job | #213 | **待操作者合并** | 装载 plist（O-8）后每周日 |
| 3.10 | WP-R1 宿主外告警 | 未开 | 等 O-3（选服务、URL 进 `~/.zshrc`） | — |
| 3.11 | WP-P6 mainnet 准入设计 | #217 | **待操作者合并** | 合入即 |
| 3.12 | WP-A1 数据族 parity | #220 | CI 中（auto-merge） | 快进后的日报 |
| 3.13 | GAP-PR08 / GAP-PR10 | #215 | 已合入 | — |
| O-1 | CLAUDE.md 凭据句（含 SECURITY.md 两处） | #208 | **待操作者合并** | 合入即 |
| — | 测试够不着真实告警通道（新增） | #219 | 已合入 | 合入即；**O-2 须在它之后做，现已满足** |

主 checkout 在 2026-09-28 12:21:42Z 被快进到 870ec80f（`git reflog show main`）。所以 O-7 在这一刻已发生。
快进后 `live status --check` 退出码 0：construction `2ee491c13971`；磁盘上的 registry `7f8adb754962` 与治理规则 `9cc96461276f`
都与正在运行的循环一致。实盘进程仍是重启 #60 的那个，C6 要到下一次重启才生效。

### 7.2 执行中查出的本手册错误（按事实更正）

1. §2「第一波互不触碰同一文件」不成立。3.3、3.5、3.6 共用 beidou_live 的余量（合计约 +70，余量 40），抬顶也都改
   `test_source_budget.py`。`verify` 是 `strict=false`，各 PR 在余量内 CI 都绿，合在一起 main 才红。
   执行时改为：净增 > 5 行的 PR 在同一提交里抬本 PR 的增量，把语义溢出变成文本冲突，由协调者按合入那一刻的 main 重量。
   当日按此解冲突 6 次，main 没红过。
2. §3.2 的「目标」说过头。`beidou_cli/live_cmd.py` 在模块顶层 import 报告层，armed 进程经它仍载入全部 10 个 report 模块。
   C6 收窄的是引擎模块的依赖方向。后半的定价在 #210 描述里，已开成后续任务卡片。
3. §3.3：`profile: demo` 没有任何代码读（另立 `UNREAD_KEYS`）；`costs` 的读者是 `execution_fidelity`，不是 `composition`。
4. §3.5 第 5 条：把「收集用例数」写成常量，任何新测试都会让它变红。改成不变式：addopts 与 `ci.yml` 都不排除 `archive`。
5. §3.6：bridge 的 inert 判定改为问启动门本身（`governance_cmd._gate`），不看 registry 的 verdict 一行。
   另找到一个手册没列的开关：`beidou_governance/admission.py` 的 `WINDOW_ANCHOR`（每 30 天翻一次批次窗口）。
6. §3.7：GitHub 的 anchor 保留下划线，是 `#beidou_live`，不是 `#beidou-live`。带行号的旧引用在 `docs/analysis/`，
   `docs/RESEARCH_LOG.md` 里没有。
7. §3.11 与分析文档：M-010 的 30 天窗口从重启 **#60** 起算（#59 之后又换过一次构造），最早 2026-10-27。
8. §3.12：「叶的 lookback 与读者 warmup 取大」和「与 `metrics_refusal` 同口径」两条会给出相反判定。
   执行按启动门算（今天要 1,442 bars），叶自己的 lookback 印在「谁读它」一栏。
9. §3.13 GAP-PR08：`test_source_budget.py` 冲突约 9.83 次/周，按字面过了阈值。但 45 个冲突块全含 CEILING 数值行，
   WP-C1 一次也消不掉，它的收益只剩可读性。这条读数写进了 #218 的描述首节。
10. O-1 的范围：SECURITY.md 泄漏处置第 3 步写着「新 key 写进 `env.sh`」，照做会在事故中途静默关掉告警，一并改进 #208。
11. 协调者派工时写「agent 的 shell 会读 `~/.zshrc`」，不成立：Claude 的 Bash 工具是非交互 zsh，环境里没有任何 `BEIDOU_*`。
    带着真实变量跑的只有操作者的交互 shell 与 `deploy/run_*.sh`。

### 7.3 顺带发现（逐个 PR 描述扫出，本轮未改）

| 发现 | 出处 | 去向 |
| --- | --- | --- |
| 实盘 exit overlay 在 cooldown 内「反向→翻回」会被 D-045 分支平掉，回测继续持有。潜伏，实盘 0 次 | #214；RESEARCH_LOG 同日一节；strict xfail 钉住 | **待操作者裁定修法 A**（`beidou_live/exits.py:129` 加一个条件，搭下一次重启） |
| 报告层 import 时异常会让所有 `beidou` 子命令起不来（含 armed 的 `live run`） | #210 | 后续任务卡片 |
| 日报文字还写着「构造冻结到 2026-10-13」「冻结中」，冻结已于 09-27 结束 | #216（`reports.py:395`、`:787`） | 后续任务卡片 |
| `governance reopen` 遇到不带时区的日期会抛 TypeError | #216（`reopen.py` 约 :140） | 后续任务卡片 |
| 归档日报每天可能不含 23:00 那根 bar | #215 | 后续任务卡片（先核实） |
| `report weekly` 自 09-16 起没有调用者，一份周报都没产出 | #213 | #213 排了 job |
| M-PR01 的「整周 alpha 投入占比」实际按最近 40 个提交算，不是按周 | #213 | 未改 |
| 循环读、shipped profile 没写、走默认值的 4 个键：`portfolio.exempt_reductions`、`portfolio.max_order_notional`、`pool.dropped_after`、`alerts.dedup_window_seconds` | #211 | 未改 |
| 启动门按桶数算 metrics 覆盖，不按列：long/short 列比 open interest 晚五天开始记录 | #220 | 改门是另一次裁定 |
| metrics 读者要等快照攒满 1,442 bars，其中 720 是上市天数过滤，与 metrics 无关（约 11 月中） | #220 | 未改 |
| `live_cmd` 在 import 时按值绑定 `APP_SUPPORT`（`ALERT_DEDUP_STATE`、`verify-failures.jsonl`），conftest 的重定向够不着。只读核过，目前测试没有写进真实目录 | #212、#219 | 未改 |
| `BinanceRestClient` 的 `guard` 是可选参数，不传就没有 host 检查；今天没有生产调用者不传 | #217 | 写进 mainnet 设计的 §4.2（同一 PR 堵口） |
| 构造冻结那条测试缺记录时 `return` 而不是 `pytest.skip`，所以在 CI 里读作 passed | #212 | 未改（会改默认门读数） |

### 7.4 操作者待办（更新）

- **合并**：#208（O-1）、#213（D-PR03）、#217（P6）、#218（C1，先读首节的 GAP-PR08 读数）。#213 与 #218 改同一文件，建议先 #213。
- **O-2**：#219 已合入，现在可以导出 `BEIDOU_ALERTS_WEBHOOK_URL_2`。
- **O-3**：选 dead-man 服务、建两个 check、URL 进 `~/.zshrc`（不建 `env.sh`），3.10 随后开工。
- **O-4**：确认 headroom 数字（提议 max(40, 1% × 顶)），3.8 随后开工。
- **O-5**：BNX 夹具与归档哪边对。快进已发生，从今晚本机 01:20 起夜间 data job 每晚推送一次 FAIL，直到定下来。
- **O-6**：一次按纪律的重启，载入 C6（与 R1 同批；若裁定修法 A，也同批）。
- **O-8**：#213 合入且主 checkout 再快进后，装载 `com.beidou.weekly.plist`。
- **裁定**：WP-C8 的修法 A。
