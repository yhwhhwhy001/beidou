# 面向生产的重构方案 · Checkpoint

> deep-analysis V3.8 · L 级 · 文件名按 SKILL 11。恢复时先处理 HOLD / BLOCKER / P0，不重复 COMPLETE 的 Phase。

- **更新时间 / 段次**：2026-09-28（本地凌晨）；会话因用量上限中断于 Phase 7 进行中。
- **等级 / Interaction**：L / Yellow（🟢用户 🟢场景 🟡损失 🟡成功标准 🟢约束 🟢可得证据）。
- **当前 Phase 与状态**：
  - Phase 1–6：**COMPLETE**，冻结稿 §0–§6 已落盘（见「关联文件」）。
  - Phase 7：**IN PROGRESS**——Opus 5.5 独立审查子代理已启动，输入只有冻结文件与原始证据路径；它会把审查稿写到
    `/private/tmp/claude-501/-Users-maguannan-beidou/ce8d8edb-efc7-45e0-8aed-796cac99e63b/scratchpad/beidou-production-refactor-review-opus.md`。
    若该文件不存在或为空，说明审查未完成，按附录 D.9 的 prompt 用新会话（`model: opus`）重跑一次，输入仍只给冻结文件。
  - Phase 8–10：**草稿已写**（未按 Phase 7 的 Kill 调整），在 `docs/analysis/2026-09-28-production-refactor-deep-analysis.DRAFT.md` 的 §8–§10。
  - §11 自检表、§12 Checkpoint、§13 Final Decision、校准行、正式文件名（去掉 `.DRAFT`）：**未做**。
- **已冻结结论**（Decision IDs）：
  - D-PR00：框定选 F-C「生产边界」+ F-D「变更成本」；F-B「alpha 是功效与 parity 问题」承认为真但不由重构解决；F-A「alpha 代码结构问题」被三条 E1 排除。
  - D-PR01：推荐 O-PR2 + O-PR3 + O-PR5，14 个工作包分 Phase 0–3，零构造、零 ledger、零 Policy 变更。
  - D-PR02：「生产」三层定义，默认 L-A（demo 无人值守做完）+ L-B（mainnet 小额准入**设计**，不启用）。
  - D-PR03：`PLAN_BUDGET` 重定价，默认今天 +5%，理由与改动同 commit。
  - D-PR04：`beidou_alpha` 不重排、不重写；只补 WP-A1 / WP-A2 两件仪器（都不在 alpha 包内）。
  - D-PR05：每个 WP 的验证协议 = 两条构造测试 + 三个 digest 前后逐字 + #135 快照逐字节 + `live verify --check` 0 + 四道门全量。
- **Gate 状态**：G0 PASS · G1 PASS · G2 PASS · G3 PASS · G4 PASS · G5 PASS · **G6 待审** · G7 草稿。命中 H3 → 决策上限 **Weak GO**。
- **开放 Claims / Kills / Assumptions**：C-PR02 PARTIAL（宿主外监控 UNKNOWN）；C-PR04 PARTIAL→按子代理 C 计数 7 条 `.beidou/` 读者可升 SUPPORTED（定稿时改）；A-PR01 / A-PR03 / A-PR05 三条高×高各挂 GAP-PR02 / 01 / 03；Kill 待 Opus 审查稿。
- **证据缺口**：GAP-PR01（宿主外告警是否已有，H×H，操作者）；GAP-PR02（生产定义，H×H，操作者）；GAP-PR03（60 次重启漏掉的 bar 上本该触发几次退出，零 ledger 离线重放，下一会话）；GAP-PR04（family gate 任务 09-26/27 读了什么）；GAP-PR05 已由子代理 C 回答（7 条）；GAP-PR06（validate 每格耗时）；GAP-PR07（engine.py 自然边界，子代理 A 未见）；GAP-PR09（BNX 夹具 48 行对归档 552 行，哪个对——操作者）。
- **待决问题**：Q1（Q-CRITICAL，生产指哪一层）、Q2（ratchet 理由可否外移）、Q3（宿主外 dead-man / 第三方是否可接受）、Q4（family gate refuse 后的降级是否排 job）、Q5（预算重定价 A/B）。默认：Q1=A、Q2=A、Q3=按第三方做、Q4=B、Q5=A。
- **取证预算已用 / 剩余**：外部查证 0 次；代码检索约 60 次（用户材料，不计外部查证，但远超 L 级每段 12 次的精神，校准行要记）；四道门在主 checkout `40cbe17c` 跑过一遍：ruff / mypy 绿，pytest 1 failed / 2812 passed（`test_the_fixtures_are_the_archive_verbatim[BNXUSDT_2023-02-22.json]`，本地专属）。
- **下一动作**（按顺序）：
  1. 读 Opus 审查稿；把 Kill Register 并成 §7，按状态（CLOSED 需新证据或改范围）关闭，改写受影响的 §0 / §1 / §3.5 / §6 / §8；保留被推翻段落并标 **[R 修订]**。
  2. 按 §7 结果修 §8–§10 草稿；填 §11 自检表、§12、§13；去掉文件名里的 `.DRAFT`。
  3. 把 Opus 审查稿原样存为 `docs/analysis/2026-09-28-production-refactor-adversarial-review.md`（先例：`2026-09-07-exits-tail-adversarial-review.md`）。
  4. 在 `docs/analysis/analysis-calibration.md` 追加一行（§10.4 已给模板）。
  5. 在本 worktree 跑四道门（用主 checkout 的 `.venv` 绝对路径；不加 `-x`、不再加 `-q`），推分支、开 PR，立即 `mcp__ccd_pr__set_monitor`（auto_fix = address_comments = true，auto_archive_on_close 保持关）。
  6. 交付时把 Q1–Q5 与默认值单独摆给操作者。
- **关联文件**：
  - 冻结稿（§0–§6 + 冻结稿自检表 + 原始证据路径）：`/private/tmp/claude-501/-Users-maguannan-beidou/ce8d8edb-efc7-45e0-8aed-796cac99e63b/scratchpad/beidou-production-refactor-frozen.md`（内容与本仓库 DRAFT 文件的 §0–§6 相同）。
  - 本仓库草稿：`docs/analysis/2026-09-28-production-refactor-deep-analysis.DRAFT.md`（分支 `docs/alpha-refactor-plan-2026-09-28`，基于 `origin/main c926946d`）。
  - 三个只读盘点子代理的汇报（live / cli+governance+data / tests）与既有分析梳理，只在会话记录里；关键数字已复核并写进冻结稿 §2.1 与 §5.2。
  - Constitution：无；§10.4 建议用 Q1–Q5 的答案生成一份。
