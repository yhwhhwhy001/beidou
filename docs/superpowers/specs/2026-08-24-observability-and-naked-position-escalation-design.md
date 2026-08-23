# 可观测性修复与裸仓升级处置设计(2026-08-24)

## 背景与根因

2026-08-23 19:27 引擎因 `LOCAL_POSITION_WITHOUT_VENUE_FACT` 进入 LOCKED 终态,
静默停机 6 小时 11 分。事后加装的 watchdog(commit 275bc11)解决了"停机后无人拉起",
但排查过程暴露出四个各自独立的缺陷,共同特征是:**系统的诊断输出不携带处置所需的信息,
或安全机制的触发条件与它要防的故障负相关**。

本轮修复这四项。三项纯增量(不改 fail-closed 行为),一项改变 LOCKED 判定
(第 3 项 A/B 分流)、一项改变不可逆动作的触发判据(第 4 项)。

### 四项缺陷与证据

| # | 缺陷 | 关键证据 |
| --- | --- | --- |
| 1 | G5 认证场景污染共享基线后,还原不抗硬杀 | 41 次注入 0 次搁浅,但 08-22 12:44 那次注入**已实测触发 LOCKED** |
| 2 | 对账 `Balance mismatch` 消息标签在第三条轴上错误 | 08-22 18:24 实测 `exchange/event_stream: ... system=10715.30 exchange=5039.31`,两个标签都不是真实来源 |
| 3 | LOCKED 根因不可恢复;保护 gap reason 到不了判定点 | 19 次 LOCKED 中 **14 次(74%)真实阻断项被 `message[:80]` 截断吞掉**;501 条 protection 事故标题全相同、不含 reason |
| 4 | 裸仓紧急平仓安全网几乎从不触发 | 18 次升级尝试仅 1 次走完(`x1/3`×15、`x2/3`×2、成功×1);同期 **38 次引擎重启** |

### 第 3 项的根因判定(与初始假设不同)

初始假设"A 类(新开仓保护待建)良性快愈、B 类(保护真丢失)致命不愈,故给 A 放宽阈值"。
**该假设被数据否定**——按 reason 统计 gap 会话时长(数据源 `evidence/beidou_engine.log`
的 `protection facts not clean` 行,60s 限频打印,故分辨率 60s 量级):

| reason | 会话数 | 中位 | p90 | p99 | 最长 | >300s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `STOP_LOSS_QUANTITY_UNCOVERED`(A) | 525 | 60s | 60s | 1329s | **5078s** | 2.5% |
| `LOCAL_POSITION_WITHOUT_VENUE_FACT`(B) | 158 | 60s | 150s | 933s | 933s | 7.6% |
| `ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION` | 28 | 60s | 60s | 355s | 355s | 3.6% |

两类中位相同,且 **A 的长尾(85 分钟)比 B(15 分钟)更长**。按"良性"给 A 放宽,
等于让一个**确实没有止损的持仓**裸奔更久,方向相反。

**成立的理由是另一条**:

> LOCKED 修不好 A,反而让 A 变成永久状态。

- `CONTROL_ALLOW_MATRIX[NO_NEW_RISK] = {REDUCE, FLATTEN, CANCEL, QUERY}`
  (`beidou_control/plane.py:50`),止损单是 reduce-only → **NO_NEW_RISK 下放行**。
  引擎活着就能继续把保护建起来——A 类那些长达 5078s 最终痊愈的会话即活证据。
- LOCKED 则是进程退出(wrapper 映射 exit 0,launchd 不拉起),保护**永远建不起来**,
  持仓无限期裸奔且无人值守。
- B 类相反:内存事实已错,继续运行会基于错误事实开新仓,停机才是对的。

fail-closed 语义未被削弱:"不承担新风险"由 NO_NEW_RISK 承担且始终有效;
LOCKED 额外的"整体停机"对 A 是帮倒忙。

### 遗留的证据缺口(必须记录)

74% 的 LOCKED 真实根因不可恢复,意味着 **A 类在 LOCKED 中的真实占比未知**。
本设计的 A/B 分流依据是上述安全论证(可修复性),不是占比统计。
第 3a 项的可观测性修复正是为了让该占比在未来可测。

---

## 决策记录(与用户澄清确认)

| ID | 决策 | 理由 |
| --- | --- | --- |
| D-1 | 四项同轮修复,按 architectural 路径 | 第 3 项改变 fail-closed 行为边界 |
| D-2 | 第 3 项两步都做(可观测 + A/B 分流) | 用户在知悉"74% 根因不可恢复"后仍明确选择;分流依据改为可修复性论证 |
| D-3 | A 类**永不** LOCKED,只 DEGRADED | LOCKED 使 A 永久化(见上) |
| D-4 | A 类卡死必须配套 stuck 告警 | 否则把"静默停机"换成"静默卡死",现有告警通道只在进程死亡时响 |
| D-5 | 第 4 项不新建能力,只改判据载体 | `_maybe_emergency_close_unprotectable` 的安全属性已足够,失效点单一 |
| D-6 | E3 在 live/canary 默认关闭 | 自动平真钱持仓必须是单独的明确决定,不可由本轮顺带生效 |
| D-7 | 逐品种平仓,绝不用账户级 `EMERGENCY_FLATTEN` | 爆炸半径最小化 |
| D-8 | preflight 只检测报错,不自动改 PG | 检查不应有副作用 |

---

## 1. G5 基线抗硬杀

### 问题

`beidou_certification/g5_scenarios/engine/reconciliation_mismatch.py` 故意把共享基线
(`account_opening_projection` / `default:BINANCE`)的 `balance_amount` 写成 `"1"`
(`_CORRUPTED_BALANCE`),验证引擎检出 MISMATCHED 并在还原后回到 MATCHED。
`finally`(第 409-427 行)兜底还原,但**只挡 Python 异常,挡不住 SIGKILL / OOM / 断电**。

污染窗口约 35~70s。窗口内进程被硬杀 → 基线永久停在 `1` → 引擎恒 MISMATCHED →
NO_NEW_RISK → 可累积到 LOCKED 静默停机。**watchdog 救不了**:重启后基线仍坏,
连试 3 次后熔断。

### 方案:日志式还原(journal)

保持测试诚实性(照样污染真实基线、照样测真实路径),只增加崩溃可恢复性。

1. 污染**前**写独立记录 `record_type='g5_journal'`, `record_id='reconciliation_mismatch:baseline'`,
   payload 含 `original_payload`、`corrupted_at`、`scenario_run_id`
2. 还原成功后**删除**该记录
3. 场景启动时若发现残留 journal → 先用它还原基线,记录 `action: "recovered_stranded_journal"`,再开工
4. `beidou_launcher/preflight.py` 新增检查 `startup.safety.g5_baseline_journal`:
   journal 存在 → **P0 FAIL 阻断启动**,消息给出记录 key 与修复方式

### 被否决的备选

- **污染自带过期时间**:把测试语义泄漏进引擎生产逻辑,引擎需理解"这是测试写的坏值"——否决
- **改用独立账户 key**:场景的全部价值在于测真实基线上的真实 recon 路径,换 key 即失去意义——否决

---

## 2. 对账消息标签

### 问题

`beidou_safety/execution/reconciliation.py:323` 硬编码标签:

```python
f"Balance mismatch: system={system_facts.balance.amount} exchange={exchange_facts.balance.amount} "
```

而 `compare()` 是通用两两比较器,被 `compare_three_way` 用三对来源复用
(第 567-600 行)。`exchange/event_stream` 轴打出的 `system=` 实际是 exchange 值,
`exchange=` 实际是 event_stream 值——**两个标签都错**。

### 方案

`compare()` 增加 `left_label: str = "system"` / `right_label: str = "exchange"`,
由 `compare_three_way` 传真实来源名。

**兼容性已核对**:`reconciliation_mismatch.py:167` 断言
`f"system={corrupted_payload['balance_amount']}" in line`,它读的是 `system/exchange` 轴,
该轴真实名字**就是** system/exchange,断言继续通过。
`tests/unit/test_reconciliation_tolerance.py` 只断言 `diff=` / `tolerance=`,不受影响。

---

## 3. 可观测性 + A/B 分流

### 3a 可观测性(纯增量,不改任何判定)

补三处信息丢失:

1. **`summarize_blockers`(`beidou_launcher/supervisor.py:71`)**
   对 `runtime.health.incidents` 改为优先展示 **severity 为 CRITICAL/HIGH 的那条事故**
   (即真正驱动阻断的那条),而非列表首项;保留 severity 与 category。
   现状 `message[:80]` 截断导致 14/19 次 LOCKED 根因不可恢复。

2. **gap reason 贯通**
   `_assess_protection_coverage`(`beidou_core/engine.py:3060-3164`)产出的
   `gaps[].reason` 目前只进 `logger.warning`。改为一并进入:
   - protection 事故的 description(现状 501 条标题全相同、不含 reason)
   - `runtime.safety.protection_coverage` / `runtime.health.incidents` 的
     `CheckResult.evidence`,供 supervisor 分类使用(3b 的输入)

3. **LOCKED 完整快照**
   `_apply_debounce_action` 判定 LOCKED 时,把**未截断**的完整 blocker 列表
   (含每条 reason/severity/entity)写入 `evidence/locked-<timestamp>.json`。

### 3b A/B 分流

分类依据是**引擎活着能否修复**,不是"良性与否":

| 类 | 判据 | 阻断行为 |
| --- | --- | --- |
| **A 可修复** | 所有 gap reason ∈ {`STOP_LOSS_QUANTITY_UNCOVERED`, `MISSING_SL`, `MISSING_TP`, `ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION`} | 计入 DEGRADED,**不计入 LOCKED** |
| **B 不可修复** | 其余全部(含 `LOCAL_POSITION_WITHOUT_VENUE_FACT`、execution_fact、reconciliation) | **维持现状,不做任何改动** |
| A、B 并存 | — | 按 B 处理(fail-closed) |
| reason 缺失/无法分类 | — | 按 B 处理(fail-closed) |

**机制**:`HealthDebounce.feed()` 增加 `repairable: bool = False` 参数,内部维持两个计数器。
计数单位是**监控轮次**(与现有 `degrade_after` / `lock_after` 同一单位,
`monitor_interval` 默认 5s),不是墙钟秒:

- B 类连续轮次 → `lock_after`(testnet 60 轮 / live 12 轮),语义不变
- A 类连续轮次 → 只驱动 `degrade_after`(6 轮),永不触发 LOCKED

`supervisor.py:1412-1415` 的调用点据 `CheckResult.evidence` 中的 reason 判定 `repairable`。

> 注:3c/4 的 1800s / 7200s 是**墙钟秒**,取自 `protection_exposure.unprotectable_since`,
> 与此处的轮次计数是两套独立量纲,不可混用。

### 3c 配套:A 类卡死告警(D-4)

A 永不 LOCKED 意味着"静默停机"可能变成"静默卡死"。现有可感知告警通道
(watchdog 模态弹窗)只在进程死亡时触发,活着卡死无人知晓。

**状态来源统一**:本项**不新建状态**,复用第 4 项的 `protection_exposure` 记录
(逐 symbol、持久化、含 `unprotectable_since`)作为唯一真相源。
两项是同一条件上的两级阶梯,不是两套独立机制:

| 级 | 条件(取自同一 `protection_exposure` 记录) | 动作 | 可逆 |
| --- | --- | --- | --- |
| 告警级(3c) | 任一记录 age ≥ **1800s** | 写 stuck 标记 → watchdog 弹窗 | 是 |
| 处置级(4-E3) | 该记录 age ≥ **7200s** 且无显式拒绝 | 逐品种平仓(需 env 开关) | 否 |

即:卡死 30 分钟先叫人,人未处置且到 2 小时才自动平。告警级永远先于处置级触发,
给人留出 90 分钟介入窗口。

**实现**:引擎每轮扫描 `protection_exposure` 记录,存在 age ≥1800s 者即写标记文件
`~/Library/Application Support/beidou-watchdog/stuck`(含 symbol/reason/`unprotectable_since`);
所有记录清除或 age 回落时删除标记。`deploy/beidou_watchdog.sh` 每轮巡检读取,
存在即 `notify`(30 分钟节流,复用现有 `(now - down_since) % 1800 < 60` 同款节流)。

选择该路径的原因:watchdog 已每 60s 轮询、已有验证可用的弹窗通道,增量约 5 行。

---

## 4. 裸仓升级处置

### 问题

`_maybe_emergency_close_unprotectable`(`beidou_core/engine.py:9842`)已实现:
连续 3 轮 SL 无法建立 → 走受治理的 reduce-only 市价平仓。
其安全属性已足够:逐品种、`enqueue_reduce_only_market` 仅持久化意图、
执行走唯一 fenced executor 写路径、要求签名策略、数量取实时投影(非滞后的 `pp.quantity`)。

**但它几乎从不触发**:`x1/3` 15 次、`x2/3` 2 次、走完 1 次。

根因唯一且明确:`self._sl_unprotectable_streak: dict[str, int]`(`engine.py:1606`)
**是内存态、按 `pos_id` 键控**。同期日志有 **38 次引擎重启**,每次重启清空字典;
持仓平掉重开则更换 `pos_id`,计数从 1 重来。
三道治理门(`signed policy unavailable` / `position quantity UNKNOWN` /
`intent rejected`)实测触发次数**全为 0**,均未参与拦截。

即:**安全网的触发条件与它要防的故障负相关**——保护建不起来这类故障高度伴随重启与持仓翻转。

### 方案:把连击计数换成持久化裸露时钟

不新建能力,只换判据载体。

新增 `record_type='protection_exposure'`, `record_id=<symbol>`:

```json
{
  "symbol": "BTCUSDT",
  "position_generation": 16,
  "unprotectable_since": "2026-08-24T03:00:00+08:00",
  "last_reason": "VENUE_REJECT_-2021",
  "attempts": 7
}
```

清除条件(任一满足即删除记录):

- 保护确认 ACTIVE
- 持仓数量归零
- `position_generation` 变化(新持仓故意从零起算,**不得**拿旧仓的账去杀新仓)

**重启不清除**——这正是修复的要害。

### 升级阶梯(逐品种)

| 阶段 | 条件 | 动作 | 可逆 |
| --- | --- | --- | --- |
| E1 | 首次确认无法建立保护 | 写 `protection_exposure` + 打印(沿用现有位置) | 是 |
| E2 | **≥3 次确认拒绝且跨度 ≥60s** | 紧急平仓(现有路径) | 否 |
| E3 | 裸露 ≥ **7200s** 且无显式拒绝(纯卡死) | 同 E2,需 `BEIDOU_NAKED_POSITION_AUTOCLOSE` 开启 | 否 |

### 两个阈值的依据

- **E2 保持快**:触发源是 venue `-2021`(止损价已被现价穿越),止损事实上已该成交,
  拖延只增加亏损。仅加 60s 跨度下限,防止紧凑重试循环在一秒内连打 3 次。
- **E3 必须慢**:实测 A 类合法自愈 p99=1329s、最长 5078s(85 分钟)。
  阈值低于此即会平掉**本来即将建成保护**的持仓。7200s ≈ 1.4× 实测最长。
  **这是假设,不是实测结论**;写入代码注释,证伪条件见下。

### 新增安全约束(现有代码没有的)

- E3 在 **live/canary 默认关闭**,testnet 默认开启(D-6)
- 不可逆动作**前后各发一次告警**(现状只有一行 `print`,用户不可感知)
- 全程逐品种,绝不使用账户级 `EMERGENCY_FLATTEN`(D-7)

---

## 实施顺序(存在依赖)

3c 复用第 4 项的 `protection_exposure` 记录作为状态来源,故顺序不可任意:

```
2  标签修复(独立,最小)
↓
3a 可观测性(纯增量;为 3b 提供 reason 输入)
↓
4  protection_exposure 记录 + E1/E2(建立状态来源)
↓
3b A/B 分流(消费 3a 的 reason)   ┐ 可并行
3c stuck 告警(消费 4 的记录)      ┘
↓
4-E3(消费 4 的记录 + 需 3c 已能告警,保证"先叫人后处置")
↓
1  G5 journal(完全独立,可随时插入)
```

**硬约束**:4-E3 不得先于 3c 落地——否则会出现"没告警就直接平仓",
违反 D-4 与"告警级永远先于处置级"的设计意图。

## 测试策略

按 TDD:每项先写失败测试。

| # | 关键测试 |
| --- | --- |
| 1 | journal 写入后模拟进程消失(不执行 finally)→ 下次场景启动能还原;preflight 在 journal 存在时 P0 FAIL |
| 2 | `compare_three_way` 三条轴的标签各自正确;`system/exchange` 轴保持向后兼容 |
| 3a | `summarize_blockers` 在多事故列表中选出 CRITICAL 那条而非首项;reason 出现在 evidence |
| 3b | A-only 连续 100 轮 → 只 DEGRADED 不 LOCKED;B-only 达阈值 → LOCKED(行为不变);A+B 并存 → LOCKED;reason 缺失 → LOCKED |
| 3c | A 类超 1800s 写 stuck 文件、消除时删除 |
| 4 | 裸露时钟跨"重启"(重建引擎实例)保留;`position_generation` 变化时清零;E3 在 live 模式默认不触发 |

回归基线:全量单元 **3581 passed**(2026-08-24 实测,commit b2193fb)。

---

## 范围外

- **B 类(真故障)的任何行为改动**——本轮完全不碰
- **`lock_after` 数值调整**——74% 根因不可恢复,无依据;3a 落地并积累数据后另行决定
- **`system=1` 相关**:已查明为 G5 故意注入(`_CORRUPTED_BALANCE`),非缺陷,不修
- **多资产余额口径错配**(`Balance mismatch diff≈5682`):testnet 下已被
  `event-stream drift (reference only)` 降级为不阻断,属既有设计,本轮不动
- **08-20/08-22 出现的 17 次 system 侧字面量 `1`**:同属 G5 注入,已解释
- **E3 在 live 启用**——需单独决定

---

## 验证计划

| 假设 | 指标 | 阈值 | 未通过动作 |
| --- | --- | --- | --- |
| A 类不 LOCKED 不会掩盖真故障 | A-only 导致的长期 DEGRADED 次数与时长 | 单次 >2h 需人工复核 | 收紧 A 白名单 |
| E3 阈值 7200s 不误杀 | 合法自愈时长最大值 | 出现 >7200s 的合法自愈即证伪 | 上调阈值 |
| 可观测性修复有效 | 新增 LOCKED 事件中根因可判定的比例 | 应达 100%(现状 26%) | 补充未覆盖的 blocker 类型 |
