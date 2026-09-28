# 面向生产的重构方案 · Checkpoint

> deep-analysis V3.8 · L 级 · 文件名按 SKILL 11。恢复时先处理 HOLD / BLOCKER / P0，不重复 COMPLETE 的 Phase。

- **更新时间 / 段次**：2026-09-28 13:xx 本地（定稿）。上一版（同日凌晨）记的是「因用量上限中断于 Phase 7」；那次 Opus 审查子代理因 429 未产出，12:03 重跑成功。
- **等级 / Interaction**：L / Yellow（🟢用户 🟢场景 🟡损失 🟡成功标准 🟢约束 🟢可得证据）。
- **当前 Phase 与状态**：Phase 1–10 **COMPLETE**。Phase 7 由 Opus 5.5 子代理执行（审查稿 546 行，原样存档 `2026-09-28-production-refactor-adversarial-review.md`）：PIVOT，P0 ×2、P1 ×12、P2 ×3；作者处置 CLOSED 11、MITIGATED 4、ACCEPTED 1（KILL-13 流程缺口）；**审查者未复审修订版**。
- **已冻结结论**：D-PR00（F-C + F-D 修订版；F-A 未判→Q6）、D-PR01（六项卫生项 + 六个问题 + 一项操作者动作 + 三项测量）、D-PR02（Q1 三选项，默认 A「只 L-A」，未答不写）、D-PR03（→Q5，无默认）、D-PR04（→Q6）、D-PR05（验证协议）、D-PR06（治理类改动不走自动合并、未答不执行默认）、D-PR07（丢 bar 的可干预路径是传输层）。
- **Gate 状态**：G0 PASS · G1 PARTIAL · G2 PARTIAL · G3 PARTIAL · G4 PARTIAL · G5 PARTIAL · G6 FAIL→已处置（未复审）· G7 PASS（限卫生批与问题）。Final：**PIVOT**（H2 已处理；H3、H5；H7 保守计入；H1 处置后 0）。
- **开放 Claims / Kills / Assumptions**：C-PR01 PARTIAL（吞吐一半 UNKNOWN）；C-PR02 PARTIAL；C-PR03 PARTIAL；A-PR01 UNKNOWN（Q1）；MITIGATED：KILL-03/04/11/17；ACCEPTED：KILL-13。
- **证据缺口**：GAP-PR06（validate 每格耗时，M×M，下一会话）、GAP-PR08（ratchet 文件冲突成本，M×M，下一会话）、GAP-PR09（BNX 夹具对归档，操作者 HC-8）、GAP-PR10（失败周期窗口内重试收益，M×M，下一会话）。已关：GAP-PR01/03/04/05/07。
- **待决问题**：~~Q1–Q6 未答~~ **2026-09-28 全部答完**（分析 §14.1）：Q1=B（mainnet 准入设计文档，以后 C）、Q2a=是、Q2b=好（数字提议 max(40, 1%×顶)，PR 上确认）、Q3=重开 D-P4、Q5=是（预算只记录）、Q6=先跑剖析（已跑，§14.2：一格 6.7 s，计算不是瓶颈，下沉不开工）；补充「代理节点先不换」。仍待操作者：Q2b 的数字确认（PR 上）、HC-2 建 dead-man 账号、补第二告警通道变量、HC-8 BNX 夹具与归档哪边对。
- **取证预算已用 / 剩余**：外部查证 0；作者代码检索约 60 次 + 审查者 125 次工具调用（含一次完整 pytest）。超 L 级预算精神，记进校准行。
- **下一动作**（裁定后，分析 §14.5）：
  1. 卫生批六个 PR（WP-P4、C6、C7、C8、C9、P3），各按 D-PR05 验收，CI 绿即合；WP-C6 搭下一次按纪律的重启生效。
  2. 裁定解锁、由操作者合并的 PR：WP-C1（ratchet 记录搬迁）、WP-C2（headroom 政策，数字待确认）、D-PR03（预算只记录）、WP-R1（宿主外告警，按 09-05 DL-Q8 ①②⑥⑦；URL 放 `~/.zshrc`，不新建 `env.sh`；循环侧搭下一次重启）、WP-P6（mainnet 准入设计文档，`guard.py` 不动）。
  3. 操作者动作：补 `BEIDOU_ALERTS_WEBHOOK_URL_2`；建 dead-man 账号并放 URL（HC-2）；HC-8 定 BNX 夹具与归档哪边对；代理节点按裁定暂不换。
  4. 下一会话：GAP-PR08（ratchet 冲突成本）、GAP-PR10（失败周期窗口内重试收益）两项零 ledger 测量；WP-R2 在 WP-R1 满 14 天后写预登记。
  5. 卫生项之外不执行任何未经裁定的默认（D-PR06）。
- **执行版**：`docs/analysis/2026-09-28-production-refactor-execution-plan.md`（2026-09-28，操作者要求「优化到可执行状态」后写；PR 波次、每包规格、操作者清单 O-1–O-9、完成定义）。
- **关联文件**：`docs/analysis/2026-09-28-production-refactor-deep-analysis.md`（定稿，§0–§6 保留冻结稿原文并标 [R 修订]）；`docs/analysis/2026-09-28-production-refactor-adversarial-review.md`（Opus 审查稿原样）；`docs/analysis/analysis-calibration.md`（新增 2026-09-28 行）；冻结稿原件在会话 scratchpad `beidou-production-refactor-frozen.md`（sha256 `1a3e51c1…e863c`，不入库）；Constitution：无，建议用 Q1–Q6 的答案生成。
