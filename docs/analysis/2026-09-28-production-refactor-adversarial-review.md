> 本文件是 2026-09-28 Opus 5.5 独立子代理对《北斗 V5 面向生产的重构方案》冻结稿（§0–§6）的 Phase 7 对抗审查稿，**原样存档，一字未改**。作者的处置见 `2026-09-28-production-refactor-deep-analysis.md` §7。审查稿里的行号以冻结稿与当时的工作树 `40cbe17c` 为准。

# Phase 7 独立对抗审查：北斗 V5 面向生产的重构方案（冻结稿 §0–§6）

> 审查者：Opus 5.5 独立子代理（`claude-opus-5-5[1m]`）· 2026-09-28 · deep-analysis V3.8 附录 D · 等级 L
> 冻结输入：`beidou-production-refactor-frozen.md`，677 行，109,318 字节，sha256 `1a3e51c1…e318ea863c`，mtime 2026-09-28T03:08:43+08:00（= 09-27T19:08:43Z）。
> 顺序：产物自检表核对 → 独立性声明 → 最强反方论点 → 复核记录 → 角色与攻击记录 → Pre-Mortem / Inversion → Kill Register → 人类确认点 → G6 → Final Kill Decision。
> 本文不含任何账户数字。引用实盘状态时只取重启、周期、构造摘要这类运维字段。

---

## 0. 产物自检表核对（附录 D.1 第 6 条，第一步）

分两遍做。第一遍（A）在读 §0、§1、§4–§6 之前，只核 §P1–§3 能覆盖的行。第二遍（B）在写下预写反方论点之后。两遍只核「标 PASS 的在正文里找不找得到」，不判内容对错。内容问题进 §6 Kill Register。

| # | 产物 | 作者状态 | 核对结果 | 指向 |
| --- | --- | --- | --- | --- |
| 1 | Phase 级头部 | PASS | 找到（文首第 5–7 行）。六灯、等级、本档不覆盖、上限、输出状态、授权齐 | — |
| 2 | 四问 / 来源与偏差 / 预设清单六列 / Early Kill | PASS | 找到（§P1.1–P1.4），预设清单六列齐。K8「不成立」的依据是尚未写出的 §9 | KILL-16(c) |
| 3 | Context Intake | PASS | 五问找到，带「若 A / 若 B」。Q3 的答案已在仓库（09-06 D-P4），违反 SKILL 3.2 规则 4；L 级首轮未暂停 | KILL-05、KILL-13 |
| 4 | Evidence Ledger 全字段、每条可复核位置 | PASS | E-PR01–34 在 §2.1，E-PR35 在 §5.2。E-PR34 的位置是「见 §5.2」，子代理报告不在盘上；E-PR04 写「本机 tokenize 脚本」无路径。审查者独立复现了两者的数字（§3）。自检表写 35 条，K2 与 G2 写 34 条 | KILL-16(b)(d) → G2 |
| 5 | Claim Register 全字段；P0/P1 Axiom Trace 是回答 | PASS | 七条 Claim 的 Axiom Trace 都是回答，不是标签。缺 Owner 字段 | KILL-16(f) → G2 |
| 6 | G1–G7 每门 ≥1 条追溯命题 | PASS | §1 第三列找到。G4 只挂 C-PR05，而 C-PR05 的 A4 回答讲的是「现状挤占 alpha」，不是推荐方案的成本与价值。G4 名下没有一条命题检查本方案，这道门在内容上空转 | KILL-11 → G4 |
| 7 | Assumption Register；高×高有实验对齐 Gap | PASS | 找到。A-PR01、A-PR03 自标「高×中」，汇总行却列为高×高；A-PR00/06/07 字段不全 | KILL-16(g) |
| 8 | Gap Plan | PASS | GAP-PR01–07 找到。§5.2 引用的 GAP-PR09 未定义。GAP-PR01、03、04 用仓库里已有的证据就能关 | KILL-16(a)、KILL-05、KILL-06、KILL-14 |
| 9 | Problem Statement / 5W2H / Causal Chain | PASS | 找到（§3.1–3.3） | — |
| 10 | Problem Reframing ≥2 + `D-xxx` | PASS | F-A–F-D 与 D-PR00 找到。F-A 的「区分证据」测的是别的命题 | KILL-03 → G1 |
| 11 | Success Definition 在 Option 之前，`M-xxx` 三类 | PASS | 位置在 §6.1 之前，三类齐。但 §3.4 的 F-D 行已写出方案方向（ratchet 搬迁与 headroom 政策），M-PR01 定义在方案要改的文件上；M-PR03 读反；F-C 的标题损失没有指标 | KILL-02、KILL-06、KILL-17 → G1 |
| 12 | JTBD / Stakeholder（含受损方）/ Scenario / Journey / Edge | PASS | 找到，受损方三行 | 补偿见 KILL-15 |
| 13 | Strategic Fit / Relative Value（四必答）/ Business Case / CoD | PASS | 找到；4.4 N/A 带理由（第二遍） | 内容见 KILL-04、07、11 |
| 14 | As-Is/To-Be / Constraint / Impact Radius / Pre-check | PASS | 找到（§5.2–5.5，第二遍） | — |
| 15 | Risk Register | PASS | RISK-PR01–09 找到；Memo 引用的 RISK-PR02 已定义。缺 Owner 字段 | KILL-16(f) |
| 16 | Option / Failure Modes / Recommendation / Decision Log | PASS | 找到；§5.4 引用的 D-PR05 已定义 | — |
| 17 | 每个 Phase 的 Prerequisites / Produces / Gate / Downstream | PASS | P1 与 §2–§6 首行三项、节末 Gate Review 都在 | — |
| 18 | Phase 7 全部产物 | PENDING | 即本文 | — |
| 19–23 | MoSCoW / 四类 Contract / Source Trace / Validation / Checkpoint | PENDING | 冻结稿不含，允许。但 K5、K8 与 Memo「下一动作」已经引用这些未写出的内容 | KILL-16(c) |
| 24 | Final Decision 7 词 + H-ID | PASS | 找到（Weak GO，H3）。H8 未命中的理由写「凭据位置 env.sh」，与本机现状不符 | KILL-01 |

小结：24 行里 18 行标 PASS，结构上都能找到对应标题，没有空行。结构问题集中在引用完整性（KILL-16）。「找得到但撑不起 PASS」的集中在三道门：G1（第 10、11 行）、G2（第 4、5 行）、G4（第 6 行）。

---

## 1. 独立性声明（附录 D.1）

| 项目 | 内容 |
| --- | --- |
| 冻结输入及版本 | 上方文件头所列：677 行，sha256 `1a3e51c106c439922e58d481920b68cd4ed779ce0baafaedc1fee1e318ea863c`。读取从 2026-09-28T04:0xZ 开始。仓库工作树 `40cbe17c`；审查时 `origin/main` 已到 `37e56ff7`（多 #195/#197/#198/#199），只读 diff，未切分支 |
| 审查者与原作者关系 | 部分独立：不同上下文，未读作者对话、草稿与推理；模型为 Opus 5.5。同一操作者、同一仓库、同一台机器。系统注入的 memory 索引与作者共享，是一处共同锚点。派发 prompt 由作者会话写出，点名了几项检查（如 E-PR34、H3）；审查者只把它当检查清单，不当结论 |
| 可访问证据范围 | 仓库全部文件（只读）；`.beidou/live/{state,heartbeat}.json` 与 `cycles.jsonl`（只取运维字段）；`reports/daily/2026-09-27.md`、`reports/weekly/2026-09-16.md`；`docs/analysis/` 与 `docs/RESEARCH_LOG.md`；`launchctl list`；`~/Library/Application Support/beidou/` 的**目录列表**（未打开其中任何文件）；V5 计划 `~/.claude/plans/nifty-gliding-petal.md`；一条 memory 文件（跳过含 key 的行）。跑过一次 `pytest -m "not network"`（加 `-p no:cacheprovider`、不写字节码，跑后 `git status` 与跑前相同） |
| 已知限制 | ① 文件标题与文首已写出结论，锚定无法完全消除；预写反方论点在读 §0、§1、§4–§6 之前完成，但读过文首 16 行。② 同目录有作者草稿 `p1.md`–`p4_draft.md`、`p_frozen_tail.md` 与七个脚本，**未读**。③ 没跑 ruff 与 mypy；没核 `gh pr list` 的 191；没逐项核 E-PR05、13、15、16、24–29、31–33。④ 子代理原始报告不在盘上，E-PR34 只能抽核数字 |
| 不得执行的外部动作 | 不改冻结稿；不写仓库与 `.beidou/`、`trials.jsonl`；不跑 `research validate/mine/book/overlay`、`live run`；不重启任何进程；不读 env 与凭据内容；不建第三方账号；不提替代方案 |

---

## 2. 最强反方论点

### 2.1 预写（04:07Z，读 §0、§1、§4–§6 之前；原文照录，未改）

方案挑的是「可动的杠杆」而不是「挡住生产的约束」：按作者自己的证据，离真实资金最近的阻塞是 F-B（在跑的两条书一条 WEAK_PASS、一条 REJECT 靠 accepted_despite，功效表 tsmom 桶真 Sharpe 1.0 过门 9.6%，冲击系数 E5，KILL-Q12），作者承认它「成立」却按推理把它移出主问题；推荐的 WP 则是在一台没有经证实 edge 的系统上再加一层基础设施，这正是作者自己引用的 V5 C-006 失败模式（「重构本身再次膨胀成治理工程」），而 Success Definition 的领先指标 M-PR01 定义在方案要改的那个文件上，所以方案可以在不让系统离生产更近一步的情况下宣告成功。

### 2.2 回看 §4–§6 与复核之后

- **M-PR01 那一半被证实并加重。** 275 次触碰里 265 次改的是 CEILING 数值。M-PR01 数的是增长事件。WP-C1 动不了它，只有 WP-C2 放宽 headroom 能压低它（KILL-02）。
- **C-006 那一半被证实。** 非 alpha 近 7 天约 945 行/天。D-PR03 的「今天 +5%」按这个速度 1.7 天用完，按前一周 258 行/天也只撑 6.4 天（KILL-02(e)）。
- **F-B 那一半要收窄。** 作者在 §4.1 的辩护是「alpha 侧的下一步由日历时间决定，不由工程会话日决定」。审查者用 V5 计划 M-003 的第三个量去反驳，结果反而支持作者：09-10 起 5 个改信号的提交里 4 个不碰任何非 alpha 源码。所以分歧不在「alpha 被工程卡住」，而在「既然 alpha 等的是时间，这 5 个会话日与操作者的注意力该不该花在这组 WP 上」。
- **回看后新增、比预写更硬的一条：** 两条主臂的头号工作项都建在读错或过期的读数上。F-C 臂的 WP-P1 重开一条 09-06 已具名 ACCEPTED 的决定却不引用它，部署步骤还会停掉实盘循环；WP-P2 要解的「重启丢 bar」09-15 起没发生过，同期真正丢 bar 的是 7 次失败周期（6 次代理 503）。

最终采用的一句话版本写在 §9。

---

## 3. 复核记录：数字、读法与引用

结论先写：**抽到的数字全部复现，错在读法与引用。** 这与校准表「缺的不是数据是读法」同形。每条给出命令或位置，作者可以逐条重跑。

### 3.1 数字与读法

**R-01 · E-PR01 行数** · 命令：冻结稿文末的 `find … | xargs cat | wc -l` 循环 · 七包 10,936 / 15,318 / 8,623 / 3,642 / 4,287 / 747 / 289，tests 60,226，scratchpad 13,782 · **一致**。

**R-02 · E-PR03 触碰比例** · `git log --since=2026-08-28 --no-merges` 计数 · 861 与 275 复现；同窗口 `docs/RESEARCH_LOG.md` 315 次 · **数字一致**。小错：CEILING 在工作树 `40cbe17c` 上是 `:1954`，作者写 `:2007`。

**R-03 · E-PR03 的读法** · 对 275 个提交逐一解析父子两版的 `CEILING` 字面量 · **265 个改了 CEILING 数值**，8 个没改，2 个解析失败；按 diff 行分类，纯注释提交只有 6 个 · **读法错**：这 275 次触碰几乎都是「某个包长过了顶」这一事件本身，不是独立于增长的税。

**R-03b · 按改动行数看 ratchet 文件的份量** · 仓库自己的 `effort_share` 口径（去掉 `reports/`，增删行相加）按整周算 · ratchet 文件占 09-09–09-16 的 3.2%、09-14–09-21 的 3.0%、09-21–09-28 的 1.8% · C-PR03 的「变更成本最大的可测项」只在「触碰频率」一种单位下成立。

**R-03c · 抬顶历史对 WP-C2 政策** · 同一解析，统计 30 天里每包每次抬幅 · 420 次分包抬顶，**367 次（87%）不超过 WP-C2 的政策 headroom**（max(40, 2% × 顶)）；中位抬幅 cli 23、live 48、alpha 55、governance 43、data 84 行。

**R-04 · E-PR04 注释占比** · 审查者自写 tokenize 分类（代码 / 注释+docstring / 空行）· alpha 29.5%、live 27.6%、governance 25.2%、data 24.1%、cli 18.0%、exchange 18.6%、shared 10.7% · **逐位一致**。

**R-05 · E-PR06 实盘状态** · 只读 `state.json` 的 `started_at`、`restarts`、`restarted_at`、`cycles` · `2026-09-03T07:58:44Z`、60、`2026-09-27T16:35:47Z`、609（作者读时 598，差在时间）· **一致**。

**R-06 · E-PR07 的 2,154 s** · `reports/daily/2026-09-27.md:35-49`；`beidou_live/report_execution.py:188-305`；`cycles.jsonl` 的 SKIPPED 行 · 数字一致。**读法错**：`worst_restart_late_seconds` 取自重启写下的 SKIPPED 行；这一条来自 16:35:53Z 的重启，原因「restart outside the rebalance window; this bar was already rebalanced」。那根 bar 已经再平衡过，没有漏；重启落在整点后约 36 分钟，正是 CLAUDE.md 的安全窗口。

**R-07 · E-PR08 四道门** · `.venv/bin/python -m pytest -m "not network"`（加 `-p no:cacheprovider`，`PYTHONDONTWRITEBYTECODE=1`）· **1 failed, 2812 passed in 155.85s**；失败用例同为 `test_the_fixtures_are_the_archive_verbatim[BNXUSDT_2023-02-22.json]`，552 对 48 · **一致**。补一句：552 − 48 = 504，这是一个数据问题在先、流程问题在后的红——哪一边对，冻结稿交给了一个未定义的 GAP-PR09。

**R-08 · E-PR11 功效表** · `docs/RESEARCH_LOG.md:13678-13750` · 32.7%、9.6%、se 0.4390、「没有第四条是『再找一个候选』」原文都在 · **一致**。

**R-09 · E-PR12 零 ledger 的看** · `docs/RESEARCH_LOG.md:17870-18058` 第 85–86 行 · 「这次的 8 次看要算进 N」原文在 · **一致**。但 WP-A2 的默认与这句话相反（KILL-09）。

**R-10 · E-PR14 family gate** · `git diff governance/verdicts.jsonl`；`~/Library/Application Support/beidou/governance-gate.stdout.log` 末尾 · 现在有**两行**未提交：09-25T18:30:05Z `refuse`，**09-27T18:30:06Z `allow`「OOS 1.8329 vs 1.5739 at N=343」**；日志同日 `PASS tsmom`。作者读时（约 17:0xZ）只有第一行，读法当时正确；**冻结时（19:08Z）已过期 38 分钟**。

**R-11 · E-PR18** · `beidou_exchange/guard.py:16` 是 `frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})` · **一致**。

**R-12 · E-PR19 的 19.71%** · 复现：`report weekly` 默认 `--commits 40`（`beidou_cli/live_cmd.py:1005`），按报告 mtime 取最近 40 个提交，结果是 alpha 0 / research 1,888 / infrastructure 7,693 = **19.71%，逐位一致**。**读法错**：这 40 个提交跨 09-15T17:50 至 09-16T16:53（+08:00），约 23 小时，不是一周。同一规则按整周算：32.5%、36.2%、21.5%。而且这条规则把 scratchpad 研究脚本、research CLI、`alpha_registry.yaml`、`governance/` 都算作基础设施。

**R-13 · E-PR20 部署** · `deploy/run_live.sh:11-22`、`run_check.sh:11-15,33-36`，以及 `run_data.sh`、`run_forward_board.sh`、`run_governance_gate.sh`、`run_shadow.sh` 的同一段 · 七个 plist 同机，**一致**。**读法漏了一处**：六个脚本都是「env.sh 存在就只 source 它；否则从 `~/.zshrc` 取 `^export BEIDOU_` 行」。`~/Library/Application Support/beidou/` 的目录列表里**没有 env.sh**。凭据此刻来自 `~/.zshrc`，不是冻结稿 §1、§5.3、§5.5 写的 env.sh。仓库与已装载的 plist 都没有 `EnvironmentVariables`。一个没核的例外：若 launchd 全局环境（`launchctl setenv`）另有这些变量，停机不会发生——审查者不读环境变量的值，所以没核；memory 记的是「新建 env.sh 会停掉实盘循环」。

**R-14 · E-PR23** · `beidou_cli/research_cmd.py` 153 行、0 个顶层 def；scratchpad 42 个脚本 import `beidou_cli`、41 个 import 下划线名、32 个走 `research_cmd` · **一致**。

**R-15 · E-PR34 抽核七项** · `report_*` 10 文件 5,506 行；仪器合计 8,949 行；`monkeypatch.setattr` 70 次 / 35 文件；`LiveConfig` 35 个字段；`live.demo.yaml` 14 个顶层键、61 个二级键 · **全部一致**。子代理的数字这次没有抄错。

**R-16 · E-PR35** · `engine.py:1788-1797` 用 `RiskBudgetParams()` 默认值；`live_cmd.py:868` 在报告路径读 YAML；`risk_budget` 块 15 键 · **一致**，是一条真发现。

**R-17 · 非 alpha 增长率（审查者新增）** · `git ls-tree` + `git show` 逐包计数 · 非 alpha：09-14 24,488 → 09-21 26,293 → 09-25 27,948 → HEAD 32,906。近 7 天约 945 行/天，前 7 天约 258 行/天；live 近 7 天约 680 行/天。

**R-18 · V5 计划 M-003 第三个量（审查者新增，对作者有利）** · 「改一个 signal 触碰的非 alpha 文件数，目标 0」（V5 计划第 422 行；第 505 行自记「未测，仍无仪器」）· 30 天窗口去掉 M0 奠基提交后 25 个信号提交，9 个为 0、中位 1；**09-10 起 5 个里 4 个为 0**。这条读数支持 C-PR01 的「耦合」一半。作者没有用这把 V5 自带的尺子。

**R-19 · 重启丢 bar（审查者新增）** · `cycles.jsonl` 全部 SKIPPED 行 · 40 条，全是重启行；27 条注明「this bar was already rebalanced」；13 条没有，其中 **12 条在 09-06 至 09-14**，第 13 条（09-23）按 `report_execution.py:197-200` 的注释是一次代理 503 周期失败丢的 bar。**09-15 起没有一次重启因为时机丢 bar。**

**R-20 · 真正丢 bar 的是失败周期（审查者新增）** · `cycles.jsonl` 的 ERROR 行 · 全期 10 条；**09-15 起 7 条**：6 条 `ProxyError: 503`，1 条 `VenueError`（positionRisk 重试 4 次失败）。按 RESEARCH_LOG 2026-09-15 那节（第 10926–10930 行），一次失败的周期就是那根 bar 既没有再平衡也没有退出检查。近 13 天丢的 7 根 bar 全部来自传输层，0 根来自重启。

### 3.2 引用：引了数字还是引了结论

**C-01 · E-PR21（09-17 alpha 模块审查）** · 冻结稿引「无行为级 bug」与「Final PIVOT」。原文同一句还写着三处口径缺陷、两处观测缺口、四处设计性质；§14.1 记 ② 下沉「部分达成」，「M-AM06 仍未归零：复现一份 validate 仍不能只靠库函数」。冻结稿没引后者。**判定：部分引数字不引结论。**

**C-02 · E-PR22（09-25 清单第二轮）** · 原文第 3 行：「执行记录，不是裁定」。「没做的那一种」的依据是「补不上任何一项清单能力」，尺子是外部清单覆盖，不是生产或 alpha 吞吐；同一轮还做了一次结构重排（#135 拆 `reports.py`，81 个产物逐字节相同）。冻结稿约十处写成「09-25 已否决」或「已否决」（第 13、27、42、198、316、412、466、565、609、622 行）。**判定：引过头。**

**C-03 · E-PR23（M6 / #55）** · 数字与结论都对。但 WP-C3 反转了 #55 保留 35 个地址的选择，没引那个选择的理由（29 个复现脚本）。**判定：引用对，使用时省略了反面。**

**C-04 · 「规则将带进真实资金，CLAUDE.md」** · `grep 真实资金 CLAUDE.md` 只命中「真实资金账户的权益与持仓」不入库那一行。这句话出自此前几份分析的 Interaction 行。**判定：出处错记，E1 → E5。**

**C-05 · 「监控同宿……是仓库没写过的」（§6.3）** · `docs/analysis/2026-09-05-system-quality-deep-analysis.md` 的 KILL-R2（P0）与 DL-Q8；`docs/analysis/2026-09-06-remediation-execution-plan.md` 的 Q1/Q3 与 D-P4；`docs/RESEARCH_LOG.md:2764`「DL-X2 由 Q1=B 取消」。**判定：不成立。** 这是校准表 09-17 那一行「写『从未跑过』之前没 grep」的同形错。

**C-06 · 「1.16%/次」（§4.5 的重启代价）** · 这个数最早出自 `docs/RESEARCH_LOG.md:10912`：「346 个 armed 周期里 4 次 = 1.16%」，量的是**周期失败率**（四次都是传输层 ERROR）。CLAUDE.md 后来把它写进了「重启」一节；§4.5 再把它乘到「约 74 次重启」上。**判定：引用链上的读法漂移，作者继承了它。**

### 3.3 没被推翻的部分

作者的几条小发现复核成立，而且便宜：E-PR35（14 个循环不读的 `risk_budget` 键）、`live_cmd.py:326` 的 `venue: Any`、`engine.py:49` 把报告层拉进 armed 进程的 import 闭包、exits 两套实现缺同输入对比、BNX 夹具与归档不一致。它们对应 WP-C9、C7、C6、C8、P4。下文的 Kill 不攻击这五项。

---

## 4. 角色与攻击记录（L 级：六角色、十类攻击）

### 4.1 六个反方角色

**Skeptical PM · 时机、优先级、范围**

- **PM-1 · 时机** · 指向 §3.2 Why now、§4.5 · 「没有日期在催」只对工程侧成立。10-03 与 10-13 两个开关在 5–15 天内；k=0.175 的新构造 09-27 才载入，M-010 的 30 天窗口从那天起算。Phase 3 的 mainnet 准入设计在第一个干净窗口读出之前买不到任何可判读数 → KILL-07、KILL-12。
- **PM-2 · 优先级** · 指向 D-PR04、RISK-PR02 · 唯一的用户点名「尤其是 alpha 模块」，方案对 alpha 包零改动。作者自己的头号风险就是「操作者读成又一次等」→ KILL-03、KILL-13。
- **PM-3 · 范围** · 指向 §0、§6.1 · Phase 0–1 有十个工作项、五个裁定。操作者的长期诉求是「少操作」（memory 与 V5 计划都这样写）。这个范围按什么单位算「小」，冻结稿没有说 → KILL-11、KILL-12。

**User Reality Auditor · 现有方案是否已够、采用与信任成本**

- **URA-1 · 绕行够不够** · 指向 C-PR02、WP-P1、WP-P2 · 作者说的两个生产缺口，现有绕行已经覆盖了大半：重启纪律 13 天零丢 bar（R-19），同期丢的 7 根 bar 全部来自失败周期（R-20）；宿主离线的残余风险 09-06 由操作者具名 ACCEPTED（C-05）。没有绕行不是因为绕不过去，而是因为已经绕过去了 → KILL-05、KILL-06。
- **URA-2 · 信任成本** · 指向 §3.7、WP-C3 · 受损方补偿「钉 commit」钉不住 `.beidou/data`（KILL-15）。每小时的 `check.stdout.log` 此刻已经每小时报一次 `FAIL report`（滑点「与噪声不可分辨」），方案再加三节日报与一条告警通道，却没有一句处理告警疲劳。它加重的是操作者注意力这笔没定价的账 → KILL-11。
- **URA-3 · 需求本身** · 指向 Pre-PR1、P1.2 · 操作者原话是「面向生产的重构方案，尤其是 alpha 模块」。Pre-PR1 把它改写成「重排结构能提高产出」，再把改写后的命题驳倒；P1.2 又把操作者的强调记为「近因」E5 → KILL-03。

**Evidence Prosecutor · 样本、来源、时效、因果**

- **EP-1 · 读法** · 数字全部复现，读法错四处：R-03（触碰 = 增长事件）、R-06（2,154 s 是一次按纪律的重启）、R-12（19.71% 是 23 小时不是一周）、R-13（凭据不在 env.sh）→ KILL-02、06、11、01。
- **EP-2 · 时效** · R-10：GAP-PR04 在冻结前 38 分钟已被 `allow` 行回答；`origin/main` 冻结后又进了四个 PR，其中一个改了 D-018 回撤门、一个抬了 governance 的顶 → KILL-14。
- **EP-3 · 出处与省略** · C-02（「否决」引过头）、C-04（CLAUDE.md 出处错记）、C-05（「仓库没写过」不成立）；§4.1 引 memory 的操作者目标，同一段里「production/mainnet explicitly out of scope for now」没有引 → KILL-03、05、07。
- **EP-4 · 状态标签** · C-PR01 的 SUPPORTED/High 是一个反事实命题，三条证据测的是相邻命题；C-PR03 的「最大」只在一种单位下成立 → KILL-03、KILL-02。

**Complexity Accountant · 长期债务是否超过价值**

- **CA-1 · 唯一的机械刹车被放松** · 指向 WP-C2、D-PR03 · ratchet 文件头写着它的职责：「the breach cannot grow while the operator decides」。WP-C2 让 87% 的历史抬幅落在 headroom 内（R-03c），D-PR03 把预算重定到一个 1.7–6.4 天就会用完的数（R-17）→ KILL-02。
- **CA-2 · 新常设物** · 指向 §5.4 R6「净减」· 方案新增三条命令、一到两个 job、四份文档、一张要求登记每个日期常量的表（EC-PR5 自己写「又一处要记住的地方」）、`REPORT_ONLY_KEYS`、`HEADROOM_POLICY`、`looks.jsonl`。R6 写「净减」，没有计数。不单列 Kill，并入 KILL-12 的范围问题。
- **CA-3 · 同一个成本两种用法** · 指向 D-PR04、WP-C2 · 不做下沉的理由是「要抬 alpha 顶约 2,000 行」；同一份方案按政策一次抬 877 行 → KILL-03(c)、KILL-04。

**Delivery Saboteur · 边界、依赖、验收、回滚**

- **DS-1 · 部署步骤会停机** · 指向 WP-P1 · 见 R-13。照文新建只含 URL 的 env.sh，下一次重启 `run_live.sh` 以 78 退出，其它五个 job 的 webhook 变量同时消失 → KILL-01。
- **DS-2 · Falsifier 按构造触发** · 指向 §6.3 · 「M-PR03 重启迟到秒数未归零」在 WP-P2 自己的等待窗口下必然成立（R-06）→ KILL-06。
- **DS-3 · 自己的断言过不了** · 指向 WP-C2 · 表中抬幅让五个包的 headroom 超过它新加的防囤积断言 → KILL-02(d)。
- **DS-4 · 旗标不一致** · 指向 A-PR04 与 WP-P2 · 前者干跑 `--safe --dry-run`，后者规格只有 `--reason`。这是 SKILL Phase 9 点名的「预登记把旗标写错」一类 → KILL-16(h)。
- **DS-5 · 冲突面** · 指向 WP-C1 · 4,316 行搬迁要与每天约 9 次的抬顶合并；RISK-PR04 已登记「高 · 低」。不单列。

**Risk & Abuse Red Team · 越权、安全、资金、合规**

- **RT-1 · mainnet 边界** · 指向 WP-P6 · `guard.py` 的拒绝从常量变成 profile 驱动 → KILL-08。
- **RT-2 · 治理规则由 agent 自改** · 指向 §P1.6 默认、WP-C1/C2、D-PR02/03 · 默认值加上「CI 绿即由 agent 合并」→ KILL-10。
- **RT-3 · 研究完整性** · 指向 WP-A2 · 零 ledger 的看做成常设命令，默认不计入 N → KILL-09。
- **RT-4 · dead-man URL 是一枚静默令牌** · 指向 WP-P1、RISK-PR03 · 谁拿到 URL，谁就能持续 ping 让告警永不触发；它在公开仓库下与凭据同级。`.gitleaks.toml` 加规则有两个已知静默坑（memory：RE2 不支持 lookahead 会 panic；allowlist 的 regex 默认匹配 secret），冻结稿没有引 → 并入 KILL-01 的触发动作与 §7 人类确认点。
- **RT-5 · 生产重启变成一条命令** · 指向 WP-P2 · 任何会话都能调用，而此前至少有一次重启由操作者裁定（09-17 的 #52）→ KILL-06(c)、§7。

### 4.2 十类攻击

| # | 攻击类 | 结论 | Kill |
| --- | --- | --- | --- |
| 1 | 问题真实性 | 单一操作者、损失可见，问题真实。但两处被当成「损失」的现象已被现有绕行覆盖：重启丢 bar 近 13 天为 0；宿主离线风险已具名 ACCEPTED。作者折减了操作者的 alpha 强调，却没有折减自己改写的 Pre-PR1 | KILL-03、05、06 |
| 2 | 问题定义 | F-D 把症状（增长事件计数）写成根因（税）。F-A 由推理排除，不是由证据 | KILL-02、03 |
| 3 | 相对价值 | O-PR4 与作者 WP 用不同尺子定价。已证实的价值集中在 WP-P4、C6–C9 这几个小项上 | KILL-04、12 |
| 4 | 时机与机会成本 | 机会成本两处用法互相矛盾；最稀缺的资源（操作者注意力）没有定价 | KILL-11 |
| 5 | 经济持续性 | 单人系统 N/A 成立。但 D-PR03 与 WP-C2 让「成本随规模恶化」的那条曲线失去刹车 | KILL-02 |
| 6 | 证据与可证伪性 | M-PR03 的 Falsifier 按构造触发；M-PR01 可被 headroom 直接操纵；GAP-PR01、03、04 用已有证据就能关 | KILL-02、05、06、14 |
| 7 | 复杂度与二阶影响 | 三处「降摩擦」撞上「摩擦本身是控制」：抬顶政策、重启命令、look 命令 | KILL-02、06、09 |
| 8 | 风险与滥用 | env.sh 停机且告警静默；guard.py 配置化；默认值加自动合并 | KILL-01、08、10 |
| 9 | 执行与验收 | §9 未写而 K8 与 Memo 已引用；旗标不一致；GAP-PR09 未定义 | KILL-16 |
| 10 | 上线验证 | Success Definition 部分反推：F-D 的方案方向写在 §3.5 之前的 §3.4 表里，M-PR01 定义在方案要改的文件上；18 个工作项里 12 个没有对应指标 | KILL-02、06、17 |

---

## 5. Pre-Mortem 与 Inversion

### 5.1 Pre-Mortem（假设落地 30 天后判定失败）

**PMF-1 · ratchet 的「税」降了，增长没停**
- **触发机制**：WP-C2 一次抬 877 行，此后每次越顶可补到 2% headroom；D-PR03 把预算重定为今天 +5%。
- **预警信号**：M-PR01 在 WP-C2 合入当周骤降，而非 alpha 行数/天不变；D-PR03 的缺口测试一周内再次断言越界。
- **当前证据**：R-03、R-03c、R-17。
- **预防 / 失败动作**：M-PR01 换成不被单个旋钮直接操纵的问题级量（由作者 PIVOT 后定）；失败即回滚 WP-C2 的抬幅。
- **IDs**：KILL-02、M-PR01、WP-C2、D-PR03。

**PMF-2 · dead-man 上线后，实盘循环在下一次重启时停下，而现有告警一条也没送到**
- **触发机制**：操作者按 WP-P1 新建一个只含 `BEIDOU_DEADMAN_URL` 的 env.sh。六个 deploy 脚本从此只读它，不再读 `~/.zshrc`。
- **预警信号**：`live.stderr.log` 出现「demo credentials are not in the environment」；heartbeat 两个整点未更新；`check.stdout.log` 里出现 `FAIL status` 却再没有任何送达记录——两个 webhook 变量都为空时，`notify` 只打印一行，不发送，也不记「did NOT deliver」（`run_check.sh:33-60`）。而 dead-man 照常收到 ping，因为 ping「无论检查结果如何」都发。
- **当前证据**：R-13。
- **预防 / 失败动作**：部署前核凭据来源，部署后必须有一次按纪律的重启与一次告警送达读数；失败即删掉 env.sh 或补齐变量，再按纪律重启。
- **IDs**：KILL-01、WP-P1、RISK-PR03。

**PMF-3 · 方案自己的 Falsifier 把它送回原点**
- **触发机制**：30 天后读 M-PR03，最差重启迟到仍在 300–3,000 s。
- **预警信号**：第一周日报的 Restart cost 仍非 0。
- **当前证据**：R-06、R-19。
- **预防 / 失败动作**：重写 M-PR03 与对应 Falsifier；不据此「回到 §3.4 换杠杆」。
- **IDs**：KILL-06、§6.3。

**PMF-4 · 受损方视角：写复现脚本的会话绕开归档目录**
- **失败原因**：WP-C3 归档 scratchpad 脚本、收缩再导出地址。补偿是「README 钉 commit + 一行 `git worktree add` 配方」。
- **触发机制**：第一次有人要复现一段 RESEARCH_LOG 结论。代码回到了那个 commit，`.beidou/data` 回不去——09-25 的 data repair 已经在 BNX 2023-02 写进 504 根 bar（R-07）；memory 记过 p32f/g/h：钉在 `e80b1335` 上跑还不够，09-25 起还要把 CYS/TUT/LSK 截回 09-23 的 store 状态。worktree 里的 console script 还会加载主 checkout 的代码（memory）。
- **预警信号**：有会话把归档脚本拷回 `scratchpad/`，或把私有名重新加回 `research_cmd` 再导出。
- **B.7 补偿核对**：冻结稿说「钉 commit 的复现配方（memory 已记这条做法）」。memory 记的是「在它的提交开临时 worktree 用今天的数据跑，分清代码变了还是数据变了」——那是诊断方法，不是复现保证。**补偿不存在。**
- **IDs**：KILL-15、WP-C3、§3.7 受损方第一行。

**PMF-5 · 操作者读成「又一次等」，第六次要「重构」**
- **触发机制**：方案交付，alpha 包零改动，五个裁定待答。
- **预警信号**：7 天内 Phase 0 无一项开工（RISK-PR02 自己的预警）；操作者再次点名 alpha。
- **当前证据**：09-17 ×3、09-23、09-25 的同形请求；本次首轮没有暂停去问「尤其 alpha」指什么。
- **预防 / 失败动作**：首轮把 Q1 与 alpha 的含义问清；失败即停在 Phase 1。
- **IDs**：KILL-03、KILL-13、RISK-PR02。

**PMF-6 · 命令化的 look 抬高窥视次数，N 漏算**
- **触发机制**：WP-A2 以默认「不写 ledger、不改 N」落地。
- **预警信号**：`looks.jsonl` 的行数涨得比 `trials.jsonl` 的新增假设快。
- **当前证据**：冻结之后又合入一个 612 行的零 ledger look 脚本（#199，`scratchpad/ls_direction_look.py`）。
- **预防 / 失败动作**：先裁计入口径；失败即关命令（FM-PR7 已写）。
- **IDs**：KILL-09、A-PR07。

**PMF-7 · 治理默认值被自动合并**
- **触发机制**：执行会话照 §8、§9 的默认值开 PR，CI 绿即合。
- **预警信号**：`CLAUDE.md`、`PLAN_BUDGET`、`CEILING` 在操作者未答 Q2/Q5 时变了。
- **当前证据**：CLAUDE.md「PR 流程」第 4 条；auto-merge 自 09-20 起开着；Q2 两个分支都会执行 WP-C2。
- **预防 / 失败动作**：未答不执行；治理类 PR 由操作者合并。
- **IDs**：KILL-10。

### 5.2 Inversion：如果要让这个方案彻底失败，会怎么做？当前是否已经在做？

| 失败做法 | 为什么有效 | 当前是否存在 | 反向控制 | Owner |
| --- | --- | --- | --- | --- |
| 把成功指标定义在方案要改的那个文件上 | 方案一落地指标就好看，问题不必变 | **是**（M-PR01） | 指标写在问题上，且不能被单个旋钮直接操纵 | 作者 |
| 把操作者已具名接受的风险写成「新发现的缺口」 | 每一轮都能再要一次注意力 | **是**（WP-P1 对 09-06 D-P4） | 写「仓库没写过」之前 grep `docs/analysis/` 与 RESEARCH_LOG | 作者 |
| 让部署步骤依赖一个没核过的环境事实 | 故障出在操作者手里，不在 CI 里 | **是**（env.sh） | 部署前核对，部署后要读数 | 作者 / 操作者 |
| 用「降摩擦」去修「摩擦本身是控制」的地方 | 每一步看起来都在省事 | **是**（WP-C2、WP-P2、WP-A2） | 每个降摩擦项写明它放松了哪道控制，交操作者裁 | 作者 / 操作者 |
| 给每个裁定配默认值，再交给自动合并 | 没人答也会「决定」 | **部分**（§P1.6 默认 + PR 流程第 4 条） | 未答不执行；治理类 PR 人工合并 | 操作者 |
| 冻结在旧状态上，并行会话继续改底层 | 审查与执行读到的不是同一个系统 | **是**（R-10、`origin/main` +4 PR） | 定稿前 fetch，逐 PR 读 diff | 作者 |
| 首轮不暂停，直接写 677 行 | 错的前提被 677 行放大 | **是** | 首轮问 Q1 与 alpha 的含义 | 作者 |

七种做法里六种当前存在，一种部分存在。

---

## 6. Kill Register（附录 D.6，记录式）

汇总：**P0 × 2，P1 × 12，P2 × 3，全部 OPEN。** 每条都指向一个会被它改变的决定。

### KILL-01 · P0 · WP-P1 的部署步骤会停掉实盘循环，并让现有告警静默

- **攻击命题**：WP-P1 要求把 dead-man URL 写进 `~/Library/Application Support/beidou/env.sh`。本机此刻没有这个文件。六个 deploy 脚本（live、check、data、forward-board、governance-gate、shadow）全部是「env.sh 存在就只 source 它，否则从 `~/.zshrc` 取 `^export BEIDOU_` 行」。操作者照文新建一个只含 URL 的 env.sh 之后：下一次重启，`run_live.sh` 读不到 Binance 凭据，以 78 退出；每小时的 `run_check.sh` 读不到 webhook 变量，`notify` 什么也送不出去；而 WP-P1 的 ping「无论检查结果如何」照发，dead-man 保持沉默。**新监控对它自己引出的故障是盲的。**
- **关联**：WP-P1、C-PR02、§1「未命中 H8」的理由、§5.3 合规行、§5.5 权限行、RISK-PR03、K7、E-PR20。
- **Evidence**：R-13；`deploy/run_live.sh:11-22`；`deploy/run_check.sh:11-15,33-36`；其余四个脚本同段（`run_data.sh:24-28` 等）；memory 条目「新建 env.sh 会停掉实盘循环」。
- **概率**：中到高。CLAUDE.md 写「凭据只有一个位置：env.sh」，操作者很可能以为文件已在，只需加一行。
- **预警**：`live.stderr.log`「demo credentials are not in the environment」；heartbeat 两个整点不更新；`check.stdout.log` 里 `FAIL status` 之后再无送达记录（`notify` 在变量为空时静默，`run_check.sh:33-60`）。
- **缓解**：WP-P1 的规格写明变量的实际读取来源；部署前核 env.sh 与 `~/.zshrc` 的优先级；部署后的验收必须包含一次按纪律的重启读数与一次告警送达读数。`.gitleaks.toml` 的新规则要配一条会红的金丝雀，并避开 memory 记下的两个静默坑。
- **状态**：OPEN。**是否致命**：是，对 WP-P1 的现行写法。
- **触发动作**：WP-P1 撤出 Phase 0，直到规格改写并补齐上述验收；§1 的 H8 理由改写为可核的事实；K7「全部改动可由 git 回滚」删去——env.sh 在仓库外。
- **Owner**：作者（规格）；操作者（凭据位置的任何变动）。
- **若成立，作者哪一步错了**：把 CLAUDE.md 的规范句「凭据只有一个位置」当成现状（§1、§5.3、§5.5 三处），没有用零成本的 `test -f` 核；E-PR20 读了 `run_live.sh`，漏了 `elif` 分支的语义。

### KILL-02 · P0 · F-D 臂：M-PR01 数的是增长事件；WP-C1 动不了它，WP-C2 靠放松控制压低它，D-PR03 几天就被增长用完

- **攻击命题**：
  - (a) 275 次触碰里 265 次改了 CEILING 数值（R-03）。M-PR01 数的是「包长过了顶」的次数，不是独立的税。
  - (b) WP-C1 搬走注释、把 CEILING 留在原文件，265 次触碰照旧发生。它对 M-PR01 的机械上限约 1 个百分点（纯注释提交 6–8 个 / 861）。§4.3 却把「<15%（Q2=A）」与「<25%（Q2=B）」之间的 10 个百分点记在它头上。
  - (c) 能压低 M-PR01 的只有 WP-C2。代价是：87% 的历史抬幅会落进新 headroom（R-03c），这些增长不再逐次写理由；越顶的那个会话要替此前几个会话的增长写理由。C-PR03 自己称这些理由是「最完整的设计记录」，这笔损失没有定价。
  - (d) WP-C2 的抬幅违反它自己的断言。政策是 headroom ≤ max(40, 2% × 顶)。按表中抬幅，headroom 变成 live 346（上限 307）、alpha 243（219）、cli 205（173）、governance 109（86）、data 108（73），五个包超上限；exchange 15、shared 6 又低于 40 行地板。表里的数是「顶的 2%」，不是「补到政策值」。
  - (e) D-PR03 默认把预算重定为今天 +5%（non_alpha ≤ 34,550，live ≤ 16,080）。非 alpha 余量 1,644 行：按近 7 天 945 行/天撑 1.7 天，按前 7 天 258 行/天撑 6.4 天。live 余量 762 行，按近 7 天 680 行/天撑约 1.1 天（R-17）。「关闭 09-04 的开放决定」不成立。
  - (f) 按改动行数，ratchet 文件只占近三周作者改动的 1.8%–3.2%（R-03b）。
- **关联**：C-PR03、M-PR01、WP-C1、WP-C2、D-PR03、Q2、Q5、§4.2 必答 1 与 4、§4.3、§6.3 Falsifier。
- **Evidence**：R-03、R-03b、R-03c、R-17；`tests/architecture/test_source_budget.py:1-13`（ratchet 的职责：「the breach cannot grow while the operator decides」）。
- **概率**：高。(a)(b)(d)(e) 都是算术。
- **预警**：WP-C2 合入当周 M-PR01 骤降，而非 alpha 行数/天不变；D-PR03 合入一周内缺口测试再次断言越界。
- **缓解**：需要新证据证明「触碰」本身有独立于增长的成本（例如这个文件上的合并冲突次数、被它挡住的 PR 时长）；WP-C2 的抬幅要与它自己的断言一致；预算重定价要带增长率。
- **状态**：OPEN。**是否致命**：是，对 Phase 0–1 的 F-D 臂（WP-C1、WP-C2、D-PR03）。
- **触发动作**：F-D 臂退出 Weak GO 的执行授权；C-PR03 降为 PARTIAL；M-PR01 按问题重写，并写在任何方案之前。
- **Owner**：作者。
- **若成立，作者哪一步错了**：C-PR03 的 A2 把「31.9% 是直接读数」当成「31.9% 是税」，没有拆触碰的构成；§3.5 断言 M-PR01「与用哪种方式降税无关」，没有检查方案集里恰好有一个直接操纵它的旋钮（headroom）；§6.1 的 WP-C2 按「顶的 2%」算抬幅，断言却按「headroom ≤ 顶的 2%」写。

### KILL-03 · P1 · C-PR01 的 SUPPORTED/High 高于证据；F-A 由推理排除；D-PR04 回答的是作者改写过的命题

- **攻击命题**：
  - (a) 三条证据测的都不是「结构是否阻碍产出」。E-PR17 只测 alpha → 其它包这一个方向的 import；E-PR21 测行为级 bug；E-PR11/12 测「已测候选死在哪」，以候选已被测为条件。唯一直接测吞吐的 GAP-PR06（validate 每格耗时剖析）列入 Gap 却没跑。
  - (b)「09-25 已否决」引过头（C-02）。
  - (c) E-PR21 省略了 09-17 的残项：下沉「部分达成」、M-AM06 未归零（C-01）。D-PR04 以「要抬 alpha 顶约 2,000 行」为由把残项判 Won't；同一份方案的 WP-C2 按政策一次抬 877 行。而 C-PR03 的「scratchpad 隐藏 API 面」恰好是这条残项的下游：库函数不够，脚本才去 import CLI 私有名。
  - (d) Pre-PR1 是作者替操作者写的命题（URA-3）。
  - 审查者的反向检验对作者有利：R-18 显示 09-10 起信号改动几乎不牵动非 alpha 源码，支持 C-PR01 的「耦合」一半。作者没有用 V5 自带的这把尺子。
- **关联**：C-PR01、D-PR00、D-PR04、F-A、Pre-PR1、E-PR17/21/22、GAP-PR06。
- **Evidence**：C-01、C-02、R-18；`docs/analysis/2026-09-17-alpha-module-deep-analysis.md:498`；V5 计划第 422、505 行。
- **概率**：高（标签问题是定性的，不靠概率）。**预警**：操作者再次点名 alpha（PMF-5）。
- **缓解**：把 R-18 这类读数与 GAP-PR06 的剖析补进来，再定 C-PR01 的状态。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：C-PR01 改为 PARTIAL（耦合一半有读数支持，吞吐一半 UNKNOWN）；D-PR00 的「由证据排除」改为「未判」；D-PR04 从作者决定改为交操作者的问题，附 09-17 残项；E-PR22 的「否决」改为「09-25 执行记录未做，依据是清单覆盖」。
- **Owner**：作者；D-PR04 的裁定 Owner 为操作者。

### KILL-04 · P1 · G3：O-PR4 与作者的工作项不在同一把尺子上定价

- **攻击命题**：
  - (a) 成本单位不同。O-PR4 写「51k 行测试 import」，那是整个测试套件的行数。实际引用 `beidou_alpha` 的 import 行：tests 479 条（161 个文件）、其它包 149 条、scratchpad 223 条，合计约 851 条；#55 与 #135 两次都用原地址再导出，改动面接近 0。作者自己的 WP-C1 记净行数（−4,100 / +4,300），毛搬迁约 8,400 行。
  - (b) 风险口径不同。O-PR4 记「动被交易的数的风险最高」。作者给自己的 RISK-PR01 记「低」，理由是 M-PR04 + #135 协议兜底。同一协议没有用在 O-PR4 上，而 #135 恰好证明了大搬迁能做到 81 个产物逐字节相同。
  - (c) 价值口径是循环的。O-PR4「对 M-PR01–06 增量为 0」，而 M-PR01–06 是按 F-C/F-D 写的，任何 F-A 方案在这组指标上都只能是 0。
  - (d) §4.2「现有绕行约 60%」「流程/人工约 30%」没有出处，是 E5。
- **关联**：§4.2、§6.1 O-PR4、D-PR01、G3。
- **Evidence**：`grep -rhE '^\s*(from|import) beidou_alpha'` 分目录计数（审查者 2026-09-28）；C-02；E-PR22。
- **概率**：高。**预警**：—。
- **缓解**：§4.2 与 §6.1 用同一单位（毛触碰行数）、同一验收协议、同一组问题级指标重定价。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：G3 由 PASS 改 PARTIAL，直到重定价完成。
- **Owner**：作者。

### KILL-05 · P1 · C-PR02 与 WP-P1 重开一条操作者具名 ACCEPTED 的决定，却不引用它

- **攻击命题**：
  - (a) 2026-09-06 操作者答过同一个问题：Q1=B（没有能 GET/PUT 的远端介质）、Q3=A；D-P4 取 O-X1「同机 + 第二通道 + 只告警」，KILL-Q16 记 ACCEPTED；重开条件「出现远端介质 → 重开 O-X2」。本文的 Q3 再问一遍，答案在仓库里（SKILL 3.2 规则 4）。§6.3 还写「监控同宿……是仓库没写过的」（C-05）。
  - (b) 09-05 的 KILL-R2（P0）列过朴素看门狗的失败形态：心跳是本机文件；崩溃循环每 60 s 刷新心跳，「循环活但下不了单」时心跳仍新鲜；超过 2 小时的人工维护会被当成失联（KILL-R2 (f)）。DL-Q8 因此要求循环主动推 OK、维护 sentinel、「循环活但心跳迟到不得触发」的演练。WP-P1 从 `run_check.sh` 发 ping、「无论检查结果如何」，只证明宿主、launchd 与出站网络还在。KILL-R2 的各条一条也没回应。出站很可能也经过会成簇 503 的 1082 代理（memory 记它同样打 `api.github.com`），EC-PR2 只处理了单次失败。
  - (c) 对作者有利、作者却没用的一条：09-06 的 Q3 原文是「主机死后仓位保持到你处理，告警 ≤ 2 bar」，而取了 O-X1 之后宿主死亡根本不告警（O-X1 行自己写「仍 0 / 3」）。操作者接受的与实际交付的之间有落差，这正是重开的理由。
- **关联**：C-PR02、GAP-PR01、A-PR03、Q3、WP-P1、§6.3 偏差自检、S-PR1。
- **Evidence**：`docs/analysis/2026-09-06-remediation-execution-plan.md:95-102,204,363-371`；`docs/analysis/2026-09-05-system-quality-deep-analysis.md:22,415,896`；`docs/RESEARCH_LOG.md:2764`。
- **概率**：高。**预警**：操作者回一句「这个 09-06 就定过了」。
- **缓解**：以 D-P4 的重开条件立论，引 Q3 文字与 O-X1 实际之间的落差，逐条回应 KILL-R2。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：GAP-PR01 以既有证据关闭（没有宿主外路径，且已 ACCEPTED）；C-PR02 的「宿主外 UNKNOWN」一半改为已知，H3 的这一半触发理由随之消失；WP-P1 从「默认 Must」改为「D-P4 重开，交操作者」；Q3 改写；§6.3「仓库没写过」删去。
- **Owner**：作者；重开裁定 Owner 为操作者。

### KILL-06 · P1 · M-PR03 读反；WP-P2 要解决的问题 09-15 起没有发生，丢 bar 的真实来源不在方案里

- **攻击命题**：
  - (a) 2,154 s 来自一次「this bar was already rebalanced」的安全窗口重启（R-06）。WP-P2 自己也等同一个窗口（整点后 5 分钟到下一整点前 10 分钟），所以这个读数按构造落在 300–3,000 s，不会「命令化后 0」。§6.3 的 Falsifier「M-PR03 重启迟到秒数未归零」按构造必然触发，会把作者送回 §3.4「换杠杆」。
  - (b) 40 条重启行里 27 条已再平衡；13 条未注明的里 12 条在 09-06 至 09-14，第 13 条是周期失败丢的 bar（R-19）。GAP-PR03 被排在「Phase 2 开工前」，其实一次只读就答。
  - (c) 成本降了，频率会升。重启变成一条命令，任何并行会话都能调用；M-PR03 的另一半「重启数/天」没有任何工作项在降，WP-P2 反而可能推高它。此前至少有一次重启由操作者裁定（09-17 的 #52）。
  - (d) §4.5 用「约 74 次重启 × 1.16%/次」推一个月的风险。1.16% 量的是周期失败率，不是重启（C-06）；即便照冻结稿自己的说法把它当成「跨 bar 重启」的条件概率，跨 bar 的重启近 13 天也是 0 次。
  - (e) 冻结稿点名的损失机制——「少一根 bar 的退出检查」——近 13 天确实发生了 7 次，但全部来自失败周期（6 次代理 503、1 次 venue 错误，R-20），0 次来自重启。F-C 的因果链把这项损失挂在「部署=重启」上，而真正丢 bar 的传输层路径不在方案的任何工作项里。
- **关联**：M-PR03、C-PR02「部署=重启且重启有价」、WP-P2、A-PR05、GAP-PR03、§4.3、§4.5、§6.3 Falsifier、RISK-PR07。
- **Evidence**：R-06、R-19、R-20、C-06；`reports/daily/2026-09-27.md:35-49`；`beidou_live/report_execution.py:188-305`；`docs/RESEARCH_LOG.md:10912,10926-10930`。
- **概率**：高。**预警**：WP-P2 合入后重启数/天上升。
- **缓解**：M-PR03 改量「重启丢掉的 bar 数」一类不会被安全窗口按构造抬高的量。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：M-PR03 与 §6.3 Falsifier 重写；GAP-PR03 按 R-19 关闭；WP-P2 的相对价值由 Adequate 改 Unproven；C-PR02 里「重启有价」一项降级，§3.3 因果链里「部署=重启」一层按 R-20 改写；§4.5 的 1.16% 按 C-06 更正；WP-P2 列入人类确认点（§7 HC-4）。
- **Owner**：作者。

### KILL-07 · P1 · D-PR02 与 Q1 替操作者作答：默认把 mainnet 准入设计装进「生产」，而操作者最后一次记录的范围把它排除在外

- **攻击命题**：
  - (a) §4.1 引 memory 里的操作者目标，只引「最终带进真实资金」。同一段 memory 写着「demo is a test environment with real capital deferred to later; production/mainnet explicitly out of scope for now」；V5 计划的诉求里还有「弱化风控」。这两句都没引。
  - (b) D-PR02 的选择理由是「与 CLAUDE.md『规则将带进真实资金』一致」。CLAUDE.md 没有这句话（C-04）。
  - (c) Q1 只给两个选项：A = L-A + L-B 设计；B = 现在就要 mainnet 目标资金。操作者最后记录的范围「只做 L-A」不在选项里，虽然 D-PR02 的备选栏写着它。
  - (d) 09-05 的 KILL-R13 抓过同一种错：把 mainnet 前提项排进路线图，处置是「只在操作者重开 mainnet 时启动」。
  - (e) WP-P6 的「准入清单」09-05 已写过一份解除条件：附录 D 的 P0 全关、冲击模型下重推 `vol_target`、构造冻结后 30 天干净窗口、上线断言四项。S-PR5「准入清单不存在」不成立。
- **关联**：D-PR02、A-PR01、Q1、GAP-PR02、WP-P6、§4.1、S-PR5。
- **Evidence**：memory `beidou-v5-alpha-first-rebuild.md` 第 13 行；V5 计划第 34 行；`docs/analysis/2026-09-05-system-quality-deep-analysis.md:549,907`；C-04。
- **概率**：高。**预警**：—。
- **缓解**：—（这是出处与选项的问题，只能改写）。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：Q1 加入「只 L-A」，附操作者 09-14 的范围原文；§4.1 补引被省略的两句；D-PR02 的理由换成可核的出处；WP-P6 先引 09-05 的解除条件，再说增量。
- **Owner**：作者；裁定 Owner 为操作者。

### KILL-08 · P1 · WP-P6 把 mainnet 的结构性拒绝改成配置驱动

- **攻击命题**：`guard.py` 今天的拒绝是一个 `frozenset` 常量。WP-P6 把 allowlist 改成 profile 驱动，「放行分支不实现」。改完之后，放行 mainnet 只差删掉一处拒绝或给出一个 profile。仓库约 41 次提交/天，CI 绿即由 agent 合并（CLAUDE.md「PR 流程」第 4 条）。结构边界变成配置边界，而这一步对 demo 期的任何读数都没有贡献。RISK-PR05 的预防是「签字文件 + 显式旗标」，但签字文件与旗标本身也在同一个仓库、同一套自动合并里。
- **关联**：WP-P6、RISK-PR05、FM-PR8、EC-PR6、T-PR6a、§5.3 合规行。
- **Evidence**：R-11；CLAUDE.md「PR 流程」与 branch protection 一节。
- **概率**：低（被利用）；影响高。**预警**：`guard.py` 的 diff 出现在任何 PR 里。
- **缓解**：`guard.py` 的任何改动列为人类确认点，且排除在 agent 自动合并之外。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：WP-P6 在 Q1 按 KILL-07 改写并有了回答之前不动 `guard.py`。
- **Owner**：操作者（安全边界）。

### KILL-09 · P1 · WP-A2 的默认与它引用的原文相反，把零 ledger 的「看」做成更便宜的常设通道

- **攻击命题**：E-PR12 引的原文写「这次的 8 次看要算进 N」（R-09）。WP-A2 默认「不写 ledger、不改 N」，A-PR07 把默认定为「只记录、不计入 N，直到操作者裁」。命令化降低了看的成本；冻结后又合入一个 612 行的零 ledger look 脚本（#199）。memory 的判据是「免费的读数不能交回候选行……这类读数都倾向放松门」。FM-PR7 的预防是「何时计入由操作者裁」，也就是在裁定之前，默认就是免费窥视。
- **关联**：WP-A2、A-PR07、FM-PR7、C-PR06、M-PR06。
- **Evidence**：R-09；`git diff 40cbe17c origin/main --stat`（`scratchpad/ls_direction_look.py` +612）。
- **概率**：中。**预警**：PMF-6。
- **缓解**：先裁计入口径，或默认取原文的「计入 N」。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：WP-A2 在操作者裁定计入口径之前不落地。
- **Owner**：操作者（治理口径）；作者。

### KILL-10 · P1 · 默认值加自动合并，等于 agent 替操作者改治理规则

- **攻击命题**：Phase 0–1 里至少四处改的是约束 agent 自己的规则：WP-C1 改 CLAUDE.md 的 ratchet 条款字面；WP-C2「CLAUDE.md 改一句」并按政策一次抬顶；D-PR02/03「CLAUDE.md +10」并改 `PLAN_BUDGET`，默认「alpha 占比只报趋势不设目标」——没有说是 `alpha_share_tree` 0.60 还是 `alpha_share_effort` 0.90，后者是 §4.1 列出的操作者目标。§P1.6 写「未答时的默认……默认都写进 §8 与 §9」。仓库的 PR 流程是 CI 全绿由 agent 直接合并、不请求授权。Q2 两个分支都会执行 WP-C2，操作者从没被直接问到「要不要一次抬 877 行」。
- **关联**：§P1.6 默认、Q2、Q5、D-PR02、D-PR03、WP-C1、WP-C2、§0「受控执行 Phase 0–1」。
- **Evidence**：CLAUDE.md「PR 流程」第 4 条与 auto-merge 一节；`beidou_live/report_governance.py:15`（`ALPHA_EFFORT_TARGET = 0.90  # operator decision 2026-09-04`）。
- **概率**：中。**预警**：PMF-7。
- **缓解**：见触发动作。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：写明「未答不执行默认」；改 CLAUDE.md、`PLAN_BUDGET`、CEILING 政策的 PR 列为人类确认点，由操作者合并；Q2 拆成两问（搬迁 / 抬顶政策）；D-PR03 写明动的是哪一个 alpha 占比。
- **Owner**：操作者。

### KILL-11 · P1 · G4：机会成本的两处用法互相矛盾，用错了资源，E-PR19 读错了窗口

- **攻击命题**：
  - (a) C-PR05 的 A4 用 19.71% 说「工程项一直在挤 alpha」。§4.1 与 §4.3 又说本方案的机会成本可忽略，因为「两边不碰同一批文件」「alpha 侧的下一步本来就不由工程会话日决定」。两句不能同时是决定性的。
  - (b) 本方案最稀缺的资源是操作者注意力。P1.1 自己把它列为第一类损失：五个裁定要他答，十六个工作项的 PR 要他读。§4 没有按这个单位定价；操作者的长期诉求是「少操作」。
  - (c) 19.71% 是约 23 小时的读数，不是一周（R-12）。
- **关联**：C-PR05、E-PR19、§3.1、§4.1、§4.3、G4。
- **Evidence**：R-12；`beidou_live/report_governance.py:18-48`；`beidou_cli/live_cmd.py:1005`。
- **概率**：高。**预警**：—。
- **缓解**：用操作者注意力（裁定数、PR 数、日报新增节数）为单位给全方案定价；E-PR19 注明窗口并换成整周读数。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：G4 由 PASS 改 PARTIAL。
- **Owner**：作者。

### KILL-12 · P1 · 决定词被当成执行许可；Phase 2–3 的 Adequate 没有支撑（H5）

- **攻击命题**：
  - (a) Weak GO 的含义是「仅允许受控小范围验证」。§0 用它授权「受控执行 Phase 0–1」，而 Phase 0–1 是方案的大头：约 2.5 个会话日（§4.3、O-PR2）、十个工作项、两处以上 CLAUDE.md 改动、一次预算重定价、一次 4,300 行搬迁、一次 877 行抬顶。§4.2 自己说 Phase 0 是「80% 结果」。这不是小范围验证。
  - (b) Phase 2–3 记 Adequate，但 §4.2 的四个必答几乎只论证 Phase 0。按本审查：WP-P2 的问题近 13 天没发生（KILL-06），WP-A2 的默认与原文相反（KILL-09），WP-P5 的前一半已被 `allow` 行取消（KILL-14），WP-P6 与既有解除条件重复且与范围冲突（KILL-07）。Phase 2–3 应记 Unproven，命中 H5。
  - (c)「零重启」与 WP-C6 矛盾：§0 说 Phase 0–1 零重启，§6.1 的 WP-C6 写「要重启：是」。它合入后会搭上别人的下一次重启生效。
- **关联**：§0 Final Decision、D-PR01、§4.2、H5、WP-C6、§5.4 R6。
- **Evidence**：SKILL 第 9 节决定词表；冻结稿 §0、§4.2、§6.1。
- **概率**：高。**预警**：—。
- **缓解**：—。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：决定词按第 9 节含义使用；Phase 2–3 标 Unproven；WP-C6 的生效方式写成「搭下一次按纪律的重启」并进 RUNBOOK。
- **Owner**：作者。

### KILL-13 · P1 · 流程：L 级首轮不暂停、取证超预算、Gate 带着未通过项写 PASS——与 09-26 校准行记下的是同一组缺口

- **攻击命题**：
  - (a) 冻结稿引的操作者原话是「先写方案，不要执行」，没有「一次出完」。作者以「自治运行」为由跳过 L 级首轮暂停。09-26 的校准行原样记过「Phase 1 没有暂停（操作者没说『一次出完』）」。这次跳过的代价看得见：Q1（生产的定义）、Q3（09-06 已答）、「尤其 alpha」的含义，都是首轮该取回的。
  - (b) 代码检索约 60 次，L 级预算每段 ≤ 12。作者把仓库算成「用户材料」不计预算；SKILL 3.1 的表把代码库列在第 2 优先级，预算明写「代码检索次数」计入。09-26 行也记过「取证超预算」。
  - (c) G2 PASS 的未通过项写着「C-PR02 一半 UNKNOWN」；G4 PASS 的未通过项写着「机会成本……再被工程项压低」。09-26 行记的正是「G4/G5 列了未通过项仍写 PASS」。
- **关联**：G0、G2、G4、§P1.6、§2 取证说明、E-PR32。
- **Evidence**：`docs/analysis/analysis-calibration.md:23`；SKILL 3.1、5.6。
- **概率**：已发生。**预警**：—。
- **缓解**：—。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：定稿前按 3.3 把 Q1（按 KILL-07 改写）、Q3（按 KILL-05 改写）、alpha 的含义一次问完；G2 与 G4 改 PARTIAL；§10.4 校准行照实记「重复」。
- **Owner**：作者。

### KILL-14 · P2 · 冻结稿在冻结那一刻已经过期

- **攻击命题**：E-PR14 写「无新行……判不出」。冻结前 38 分钟，family gate 已写下 `allow`「OOS 1.8329 vs 1.5739 at N=343」（R-10）。GAP-PR04 按作者自己的阈值关闭：「当前引用报告 PASS → 两次 refuse 是历史」。Q4 的紧迫性与 WP-P5 的前一半随之消失。§5.2 的 governance 行写「job 上次退出 1」，部署行写 governance-gate「上次退出 0」，同一张表自相矛盾（审查时 `launchctl list` 为 0）。另外，冻结稿写 `origin/main c926946d`；审查时是 `37e56ff7`，多了 D-018 回撤门 1pp → 2.5pp（policy 版本号不动）、governance 顶 +87、`reopen.yaml` 删掉一条 10-03 的 `date_after`。WP-C2 的抬幅与 WP-P3 的输入清单都要按新 main 重算。
- **关联**：E-PR14、GAP-PR04、C-PR07、Q4、WP-P5、WP-C2、WP-P3。
- **Evidence**：R-10；`git diff --stat 40cbe17c origin/main`。
- **状态**：OPEN（作者核对后可 CLOSED）。**是否致命**：否。
- **触发动作**：关 GAP-PR04；删去或改写 Q4 与 WP-P5 的前一半；定稿前 fetch 并逐 PR 读 diff。
- **Owner**：作者。

### KILL-15 · P2 · 受损方补偿不存在：钉住 commit 钉不住数据

- **攻击命题**：见 PMF-4。WP-C3 的补偿恢复不了复现；#55 当初保留 35 个再导出地址，理由正是这 29 个复现脚本（C-03）。
- **关联**：WP-C3、§3.7 受损方第一行、RISK-PR06、E-PR23。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：补偿按「代码 + 数据」重写，或 WP-C3 降为 Won't。
- **Owner**：作者。

### KILL-16 · P2 · 自检表与引用完整性

- **攻击命题**：(a) §5.2 引用 GAP-PR09，Gap Plan 只到 GAP-PR07。(b) E-PR34 的位置是「见 §5.2」，不在盘上；数字抽核全部对（R-15），只缺可复核位置。(c) K8「§9 每个 WP 有 Test / Acceptance」与 Memo「§9.4 有各自的 Test / Acceptance」引用的是尚未写出的 §9；K5 引用未写出的 §8.2。(d) 自检表写 E-PR01–35，§2 Produces、K2、G2 写 34 条。(e) E-PR03 的 CEILING 行号是 `:1954` 不是 `:2007`。(f) Claim Register 与 Risk Register 缺 Owner 字段。(g) A-PR01、A-PR03 自标「高×中」，汇总列为高×高。(h) A-PR04 的干跑命令 `--safe --dry-run` 与 WP-P2 规格的 `--reason` 不一致。
- **关联**：自检表第 2、4、5、7、8、15 行；G0、G2、G7。
- **状态**：OPEN。**是否致命**：否。**触发动作**：逐条补齐。**Owner**：作者。

### KILL-17 · P1 · Success Definition 部分反推，而且覆盖不到推荐方案

- **攻击命题**：§3.4 的 F-D 行在「方案方向」一栏已写出「ratchet 记录搬迁与 headroom 政策」，§3.5 的 M-PR01 随后定义在这个文件上——形式上在 Option 之前，实质上按方案写。F-C 的标题损失「宿主离线没人知道」没有任何 M-PR 指标。18 个工作项里 12 个没有对应指标：WP-P1、P3、P5、P6、C3、C4、C6、C7、C8、C9、D-PR02、D-PR03。§6.3 的胜出理由「每一项都对着 §3.5 的某个读数」不成立。
- **关联**：§3.4、§3.5、M-PR01–06、§6.3 D-PR01、G1、G7。
- **Evidence**：冻结稿 §3.4、§3.5、§6.1 逐项对照。
- **概率**：已发生。**预警**：—。
- **缓解**：—。
- **状态**：OPEN。**是否致命**：否。
- **触发动作**：Success Definition 按问题重写（先于方案）；§6.3 的胜出理由改写；G1 由 PASS 改 PARTIAL。
- **Owner**：作者。

---

## 7. 人类确认点（附录 D.8）

以下动作涉及资金链路、生产写入、凭据、权限边界或不可逆操作。审查者不执行其中任何一项，冻结稿里的默认值也不能代替操作者的回答。

| ID | 动作 | 为什么要人确认 | 确认人 | 关联 |
| --- | --- | --- | --- | --- |
| HC-1 | 新建或改写 `~/Library/Application Support/beidou/env.sh`；移动或复制 Binance key | 文件一出现，六个 job 就换凭据来源（R-13）。密钥值离开原位置，要不要轮换由操作者定（CLAUDE.md 公开仓库一节） | 操作者 | KILL-01、WP-P1 |
| HC-2 | 建第三方 dead-man 账号、生成并存放 ping URL | URL 是一枚能压制告警的令牌，视同凭据：不进仓库、不进 agent 会读的文件、不进日志 | 操作者 | KILL-01、KILL-05、RISK-PR03 |
| HC-3 | 任何改动 `beidou_exchange/guard.py`，包括「参数化但不放行」 | mainnet 的结构边界；PR 排除在 agent 自动合并之外 | 操作者 | KILL-08、WP-P6 |
| HC-4 | 生产重启（含 WP-P2 的命令与 WP-C6 这类「重启才生效」的改动） | 跨过 bar 收盘的重启会吃掉那根 bar 的退出检查；命令化不改变「谁可以决定重启」 | 操作者 | KILL-06、WP-P2、WP-C6 |
| HC-5 | 改 CLAUDE.md、重定价 `PLAN_BUDGET`、改 CEILING 政策或一次性抬顶 | 这些是约束 agent 自己的规则；未答不执行默认，由操作者合并 | 操作者 | KILL-02、KILL-10 |
| HC-6 | WP-A2 的 looks 是否、何时计入 N | 多重检验口径；默认不计入即免费窥视 | 操作者 | KILL-09 |
| HC-7 | 是否重开 09-06 的 D-P4（宿主外监控） | 具名 ACCEPTED 的决定只能由同一 Owner 重开 | 操作者 | KILL-05 |
| HC-8 | BNX 2023-02 的夹具与归档哪一边对 | 数据完整性；裁定前不改夹具、不改归档（EC-PR3 已写） | 操作者 | E-PR08、R-07 |
| HC-9 | 「生产」的定义（按 KILL-07 改写后的 Q1） | 决定 Phase 3 与 WP-P6 是否存在 | 操作者 | KILL-07、D-PR02 |

---

## 8. G6 状态

| Gate | 作者状态 | 审查建议 | 依据 |
| --- | --- | --- | --- |
| G1 Problem / Axiom | PASS | PARTIAL | KILL-02（F-D 把症状写成根因）、KILL-03、KILL-06、KILL-17 |
| G2 Evidence / Reality | PASS | PARTIAL | 数字全对；读法四处错、一处过期；引用五处有问题（C-01、02、04、05、06）；KILL-03、05、06、11、13、14、16 |
| G3 Relative Value | PASS | PARTIAL | KILL-04、KILL-12（Phase 2–3 → Unproven，H5） |
| G4 Strategic / Economic | PASS | PARTIAL | KILL-11；Strategic Fit 的 High 建立在省略了「mainnet out of scope」的引文上（KILL-07） |
| G5 System / Solution | PASS | PARTIAL；WP-P1、WP-C2、D-PR03 不得进入交付契约 | KILL-01、KILL-02(d)(e)、KILL-12(c) |
| **G6 Adversarial Survival** | 待审 | **FAIL** | 未关闭 P0 × 2（KILL-01、KILL-02）；Falsifier 检验失败，见下 |
| G7 Delivery / Learning | 待 Phase 9 | 未评；不得为 WP-P1 与 F-D 臂写契约，直到上面两条 P0 关闭 | — |

**Falsifier 检验**：§6.3 列了方案自己的四条 Falsifier。
- 「M-PR01 仍 > 20%」：WP-C2 放宽 headroom 就能直接满足它，WP-C1 按构造满足不了它（KILL-02）。它分不清「税降了」与「刹车松了」。
- 「M-PR03 重启迟到未归零」：按构造必然触发（KILL-06）。
- 「GAP-PR03 重放为 0 次」：候选 bar 已知至多 13 根，12 根在 09-15 之前（R-19）；近 13 天丢的 bar 全部来自失败周期（R-20）。
- 「Q3 答已有」：答案 09-06 就在仓库里（KILL-05）。

四条里没有一条能在「方案对」与「方案错」之间做区分。按附录 D.1 与 SKILL 第 9 节，G6 FAIL 命中 H7，未关闭 P0 命中 H1。

---

## 9. Final Kill Decision（附录 D.7）

| 问题 | 结论 |
| --- | --- |
| 最强反方论点 | 方案可以一边让系统离生产更远，一边达成它自己的全部成功指标：F-D 臂的 M-PR01 数的是增长事件，只能靠放松 ratchet 压低；F-C 臂的 WP-P1 重开一条 09-06 已具名接受的决定却不引用它，部署步骤还会停掉实盘循环；WP-P2 要解的重启丢 bar 09-15 起没有发生过，同期真正丢 bar 的是七次失败周期。 |
| 未关闭 P0 / P1 | **P0**：KILL-01（env.sh 停机与告警静默）、KILL-02（F-D 臂的机制与算术）。**P1**：KILL-03 至 KILL-13、KILL-17，共 12 条。P2：KILL-14、15、16。 |
| 被推翻或 UNKNOWN 的 P0 Claim | **C-PR01**：SUPPORTED → PARTIAL。耦合一半有 R-18 支持（作者没用这把尺子）；吞吐一半 UNKNOWN，GAP-PR06 未跑。**C-PR02**：仍是 PARTIAL，但理由换了。「宿主外 UNKNOWN」一半其实已知，09-06 D-P4 具名 ACCEPTED；「重启有价」一项 09-15 起没有读数支持，同期丢的 7 根 bar 全部来自失败周期（R-20）；其余三项（guard 拒 mainnet、冲击系数 E5、场地无保护单）成立，但都属于操作者尚未重开的 mainnet 范围。 |
| 需要的新 Evidence | ① ratchet 文件上的合并冲突次数，或被它挡住的 PR 时长——证明「触碰」有独立于增长的成本；② GAP-PR06 的剖析；③ 部署前的凭据来源核对；④ 操作者对改写后 Q1 的回答（含「只 L-A」与 09-14 的范围原文）；⑤ 操作者对 D-P4 是否重开、对 looks 计入口径的裁定；⑥ 冻结后四个 PR 的 diff 重读。 |
| 必须改变的 Option / Scope / Contract | 只列必须变的，不替作者设计：WP-P1 撤出 Phase 0，改为 D-P4 的重开问题（KILL-01、05）；F-D 臂（WP-C1、WP-C2、D-PR03）退出执行授权（KILL-02）；M-PR01、M-PR03 与 §6.3 的 Falsifier 按问题重写（KILL-02、06、17）；D-PR04 改为交操作者的问题（KILL-03）；Q1 加「只 L-A」，WP-P6 不动 `guard.py`（KILL-07、08）；WP-A2 等裁定（KILL-09）；治理类 PR 人工合并，未答不执行默认（KILL-10）；§4 按同一把尺子与操作者注意力重定价（KILL-04、11）；Phase 2–3 记 Unproven（KILL-12）。本审查没有攻击、前提已复核成立的是 WP-P4、WP-C6、WP-C7、WP-C8、WP-C9（§3.3）；它们是否先行，由作者在 PIVOT 后定。 |
| 最终建议 | **PIVOT**。决定上限来自 **H2**：存在未处理的强反证——09-06 D-P4 的具名 ACCEPTED 与 09-05 KILL-R2、`cycles.jsonl` 的重启行与 ERROR 行、CEILING 的逐提交构成。同时命中 **H1**（未关闭 P0 = 2）、**H7**（L 级 G6 FAIL）、**H3**（C-PR01 吞吐一半 UNKNOWN；A-PR01 待答）、**H4**（C-PR01 与 C-PR03 的决定性一步是 E5 推理）、**H5**（Phase 2–3 相对价值 Unproven）。不取 KILL：问题成立。系统的生产边界确实没有写成读数，§3.3 的五个小发现复核成立，「生产」的定义也确实没人写过。**H8 未命中**：凭据边界已知（在 `~/.zshrc`），只是冻结稿写错了位置，这一点由 KILL-01 承接。 |

G6（FAIL）、Final Decision（PIVOT）与 G7（不得为 WP-P1 与 F-D 臂写契约）三者一致。
