# M00 系统基线与架构治理 — 范围定义（REQ / AC）

原则：只处理本模块职责（启动链/装配/生命周期/依赖方向/重复链/死代码/参数治理）；跨模块缺陷只做「登记+阻断护栏」，深度修复留给对应模块（M10/M11/M12/M18/M19）。不重启引擎（未授权）；实装 launchd plist 变更需用户明确授权。

## 模块现状摘要

- 生产主链唯一且已实测确认（launchd→cli→supervisor→AutonomousEngine；PG 为主事实源）
- 真实缺口：--no-self-heal 失效、launchd 重启循环风险、告警风暴、死代码/遗留入口、testnet 特赦面过大、关键参数游离于签名策略之外、工作树脏（修复未提交）阻断重启

## TASK-M00-F01 核实未提交修复状态（P0-18）— ✅ 已完成

- REQ-M00-001：核实单调守卫修复提交状态 —— 复核结论：已提交为 `fe62da152`（引擎 18:59 启动时即加载此版本）；工作树干净；preflight `git_worktree` 无阻断
- AC-M00-001：`git rev-parse HEAD` = fe62da152 ✓；`git status` 仅本 run 未跟踪文档 ✓；全量 pytest 2496 passed ✓（fe62da152 树上实测）

## TASK-M00-F02 --no-self-heal 语义修复（P0-01）

- REQ-M00-003：`_maybe_testnet_auto_reauthorize` 在 `self_heal=False`（--no-self-heal）时不得自动 RESUME；仅在 `self_heal=True` 且满足现有防抖/无 blocker 条件时放行
- REQ-M00-004：对 live/canary/paper 保持现状（永不自动 RESUME）
- AC-M00-002：新增测试：`--no-self-heal` + testnet + DEGRADED 无 blocker → 不 RESUME、`_resume_authorized` 保持 False；self_heal=True 对照场景仍自动 RESUME
- 说明：该修复使 testnet 自动 RESUME 收敛到显式开关；TruthSnapshot 授权链挂接（P0-12）属 M14/M19 范围，本模块只登记

## TASK-M00-F03 重复 blocker 去重与告警抑制（P0-19）

- REQ-M00-005：同 check_id + 同 message 类别的 P0 blocker 在 supervisor-state.json 与告警文本中按 (check_id, entity_id) 聚合（保留全部 entity_id 明细，不丢信息）
- REQ-M00-006：HIGH 告警对同一指纹聚合周期内的重复发送做去重（AlertSuppressor 已存在，接线核验）
- AC-M00-003：测试：15 个同 check_id blocker 聚合为 1 条聚合记录 + 明细列表；连续周期不产生重复 HIGH 告警

## TASK-M00-F04 引擎死代码与死接线清理（P1-16）

- REQ-M00-007：移除 6 个 UNREACHABLE 方法（run_parity_check/build_strategy_signal/build_execution_plan/build_position_aggregate/build_idempotency_key/_sync_exchange_state）与 3 个死接线（_cert_manager/_production_ladder/_kernel_parity 的构造-即弃）；每项移除前 grep 全仓引用确认
- REQ-M00-008：contracts.py:131-133 reduce-only replay `pass` 空操作（潜伏跨零开仓）——修正为与 position_aggregate.py:119-132 一致的正确实现（该路径当前无调用方，属死代码，修正后加测试防止未来启用中招）
- AC-M00-004：架构测试（无残留引用断言）+ 全量回归

## TASK-M00-F05 testnet 特赦集中治理（P0-20 的 M00 部分）

- REQ-M00-009：8 类 testnet 特赦集中到单一显式模块/配置（如 `beidou_launcher/testnet_exemptions.py` 或 env 配置表），每项带：理由、风险接受记录、失效条件；引擎侧引用改为查表+日志
- REQ-M00-010：新增架构测试断言特赦清单完整且每项有负责人/评审标注；新增日志审计（特赦生效时 INFO 带字段）
- 边界：特赦是否收紧/移除由 M10/M11/M12 逐项裁决；本模块只做登记与可见性

## TASK-M00-F06 apps 遗留入口退役（P1-16）

- REQ-M00-011：research_lab 占位子命令、strategy_engine/safety_executor 硬编码存根改为显式退役（打印指向唯一入口并 exit 2，同 tools/e2e_real_demo.py 退役模式）；apps/autopilot 保留但标注 dev-only；factor_miner resume/compare 占位改为显式 NOT_IMPLEMENTED 报错
- AC-M00-005：架构测试禁止任何入口直接实例化 AutonomousEngine（除唯一主链）

## TASK-M00-F07 deploy plist 模板修复（P0-02 仓库侧）

- REQ-M00-012：deploy/com.beidou.autopilot.plist 与实装对齐（30 symbols 从配置读取、KeepAlive 语义文档化）；新增 `ExitCode`/`ThrottleInterval` 说明与「终态退出码不重启」方案（launchd 原生不支持按退出码条件重启 → 文档推荐 KeepAlive=false + 外部守护，或 plist 内嵌 wrapper 检查退出码）
- REQ-M00-013：`beidou_launcher doctor` 增加检查项：比对实装 plist 与模板（symbols 集合、KeepAlive、ThrottleInterval），漂移 → WARN
- 边界：实际修改 ~/Library/LaunchAgents/com.beidou.autopilot.plist 需用户明确授权后另行执行

## TASK-M00-F08 硬编码参数治理（P1-01 的 M00 部分）

- REQ-M00-014：fee tier（engine.py:8620, 4804 vip1 2.0/4.0 bps）、max_margin_ratio（8732）、stop_loss 钳制 1%/5%（8424）、capital budget 10%（8516）迁移至签名策略（config/policies/risk_parameters.json 增加字段）或显式配置源；缺失时 fail-closed（不再使用代码内默认值）
- REQ-M00-015：策略 JSON 结构变更后重新签名（签名密钥在 .env；生成新 policy 文件需用户确认后写入）
- AC-M00-006：测试：策略缺字段 → 引擎以保守默认 + WARN 启动而非静默采用代码常量；策略提供字段 → 生效且进 RiskSnapshot 哈希

## TASK-M00-F09 测试与回归

- REQ-M00-016：以上每项配套 fail-closed 单元/契约/架构测试；全量 pytest、ruff、mypy（受限范围）、format 全绿

## TASK-M00-F10 对抗审查与证据归档

- REQ-M00-017：独立对抗审查（第二轮）：构造「M00 修复后仍不能长期无人值守」的反例逐一验证
- REQ-M00-018：证据归档至 docs/optimization/runs/2026-08-16-deep-module-optimization/evidence/M00/（测试输出、diff、审查记录）
- 退出 Gate：M00 无 P0 未决（P0-02 实装 plist 变更、P0-03 保护事实源分裂、P0-12 授权链等跨模块项已登记并指派模块）→ 进入 M01

## 明确不做（本模块）

- 不重启引擎（需授权）；不改实装 plist（需授权）；不修 M12 保护事实源分裂（M12/M13）；不动双轨风险引擎（M10）；不动 testnet 特赦的具体收紧（M10/M11）；不清理遗留 SQLite 库文件（M16）
