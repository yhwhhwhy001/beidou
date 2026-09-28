# 面向生产的重构方案 · Checkpoint

> deep-analysis V3.8 · L 级 · 文件名按 SKILL 11。恢复时先处理 HOLD / BLOCKER / P0，不重复 COMPLETE 的 Phase。

- **更新时间 / 段次**：2026-09-28 13:xx 本地（定稿）。上一版（同日凌晨）记的是「因用量上限中断于 Phase 7」；那次 Opus 审查子代理因 429 未产出，12:03 重跑成功。
- **等级 / Interaction**：L / Yellow（🟢用户 🟢场景 🟡损失 🟡成功标准 🟢约束 🟢可得证据）。
- **当前 Phase 与状态**：Phase 1–10 **COMPLETE**。Phase 7 由 Opus 5.5 子代理执行（审查稿 546 行，原样存档 `2026-09-28-production-refactor-adversarial-review.md`）：PIVOT，P0 ×2、P1 ×12、P2 ×3；作者处置 CLOSED 11、MITIGATED 4、ACCEPTED 1（KILL-13 流程缺口）；**审查者未复审修订版**。
- **已冻结结论**：D-PR00（F-C + F-D 修订版；F-A 未判→Q6）、D-PR01（六项卫生项 + 六个问题 + 一项操作者动作 + 三项测量）、D-PR02（Q1 三选项，默认 A「只 L-A」，未答不写）、D-PR03（→Q5，无默认）、D-PR04（→Q6）、D-PR05（验证协议）、D-PR06（治理类改动不走自动合并、未答不执行默认）、D-PR07（丢 bar 的可干预路径是传输层）。
- **Gate 状态**：G0 PASS · G1 PARTIAL · G2 PARTIAL · G3 PARTIAL · G4 PARTIAL · G5 PARTIAL · G6 FAIL→已处置（未复审）· G7 PASS（限卫生批与问题）。Final：**PIVOT**（H2 已处理；H3、H5；H7 保守计入；H1 处置后 0）。
- **开放 Claims / Kills / Assumptions**：C-PR01 PARTIAL（吞吐一半 UNKNOWN）；C-PR02 PARTIAL；C-PR03 PARTIAL；A-PR01 UNKNOWN（Q1）；MITIGATED：KILL-03/04/11/17；ACCEPTED：KILL-13。
- **证据缺口**：GAP-PR06（validate 每格耗时，M×M，下一会话）、GAP-PR08（ratchet 文件冲突成本，M×M，下一会话）、GAP-PR09（BNX 夹具对归档，操作者 HC-8）、GAP-PR10（失败周期窗口内重试收益，M×M，下一会话）。已关：GAP-PR01/03/04/05/07。
- **待决问题**：Q1（生产层级；默认 A 只 L-A）、Q2a（ratchet 记录搬迁）、Q2b（headroom 政策，无默认数字）、Q3（是否重开 09-06 D-P4；附两条新事实）、Q5（非 alpha 增长率裁定）、Q6（「尤其是 alpha」要哪一件）。**未答不执行任何默认。** 人类确认点 HC-1–9 见分析 §7.6。
- **取证预算已用 / 剩余**：外部查证 0；作者代码检索约 60 次 + 审查者 125 次工具调用（含一次完整 pytest）。超 L 级预算精神，记进校准行。
- **下一动作**：
  1. 卫生批六个 PR（WP-P4、C6、C7、C8、C9、P3），各按 D-PR05 验收；WP-C6 搭下一次按纪律的重启生效。
  2. 操作者：答 Q1–Q6；换代理节点（09-22 判读）；HC-8 定 BNX 夹具与归档哪边对。
  3. 下一会话：GAP-PR06 / GAP-PR08 / GAP-PR10 三项零 ledger 测量，读数进 RESEARCH_LOG。
  4. 治理类 PR（若操作者答 A）由操作者合并，不走自动合并。
- **关联文件**：`docs/analysis/2026-09-28-production-refactor-deep-analysis.md`（定稿，§0–§6 保留冻结稿原文并标 [R 修订]）；`docs/analysis/2026-09-28-production-refactor-adversarial-review.md`（Opus 审查稿原样）；`docs/analysis/analysis-calibration.md`（新增 2026-09-28 行）；冻结稿原件在会话 scratchpad `beidou-production-refactor-frozen.md`（sha256 `1a3e51c1…e863c`，不入库）；Constitution：无，建议用 Q1–Q6 的答案生成。
