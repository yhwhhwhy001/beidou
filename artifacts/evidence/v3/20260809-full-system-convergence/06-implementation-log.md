# Implementation log

| Time | Task | Change | Pre-change evidence | Post-change evidence | Result |
|---|---|---|---|---|---|
| 2026-08-09T02:15:41Z | BD-V3-01 / BD-V3-03 / BD-V3-05 / BD-V3-06 | 删除 Testnet 内置签名密钥；签名缺失改为 P0；写互锁绑定 authority；P0 不再 transient；`/ready` 绑定 trading readiness；G7 要求真实窗口/样本/P0 invalidation；健康默认 loopback；Shadow 零写 | `tests/unit/test_v3_fail_closed.py` 6 项全部失败；运行态存在 heartbeat/order P0 与 READY 语义矛盾 | 新回归测试 6 passed；配置回归 3 passed；全套 tests 962 passed, 1 skipped；compileall PASS；collect 963 PASS；默认密钥扫描无命中 | PASS_WITH_CONDITIONS |
| 2026-08-09T02:33:28Z | BD-V3-07 / G5-G7 证书语义 verifier | 新增独立、无网络的 G5/G7 证书语义校验；G5 必须绑定当前 commit、完整 16 场景、无 WARN/提款权限/模拟/未来时间/证据缺口；G7 必须绑定 G5 hash 与 commit、真实 30 天、≥200 样本/≥30 日报、无 P0/重置/活动事故；旧 G5 与 fast-forward G7 不可继承；运行器和 G7 启动/评估流程接入 verifier | 旧 G5 仅 7 项且 `can_withdraw=true` 仍 PASS；旧 G7 `is_simulated=true`、commit/G5 hash 为空；评估器重建空内存窗口 | verifier 4 passed；配置/Fail-Closed 回归 13 passed；全套 tests 966 passed, 1 skipped；compileall PASS；旧 G7 评估明确 `NOT_VERIFIABLE` | PASS_WITH_CONDITIONS |
| 2026-08-09T03:38:24Z | BD-V3-04/08 reconciliation, protection ownership and fill idempotency | `ReconciliationEngine.compare` 增加 fresh/complete/future/key typed failures；账户与挂单独立快照及结果写入 durable store；对账失败立即 `NO_NEW_RISK`，运行时不再自愈写入；health/readiness 绑定心跳、控制面、对账和 owner 语义；保护单新增 owner/session/generation/exchange id，只有真实 ACK 才 ACTIVE，未归属 Algo 禁止启动写入/清理/重试；累计成交转 delta 并以唯一 `fill_events` 防重，signed `position_projection` 支持重启投影；缺失市场特征或保护距离阻断 | 旧主链可在不完整系统事实下保持 READY、用本地保护内存代替权威持仓、按 symbol 数量误认保护覆盖、重复累计 partial fill；本地数据库无 reconciliation/fill/projection schema | `pytest -q` → 989 passed, 1 skipped, 136 warnings；新增 reconciliation/owner/fill 测试 7 项；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `ed9630a162e04fec2882c88eb3c24e733ee15bff`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T03:56:16Z | BD-V3-04/08 execution fact failure and typed Algo/user-stream boundary | 成交事实改为 `fill_events PENDING→COMMITTED`，ledger 采用 durable-first 顺序；ledger/position/index 持久化失败冻结账本、`NO_NEW_RISK`、CRITICAL，并将订单置为治理 UNKNOWN；新增需来源/版本/证据哈希/审批 ID 的 `account_opening_projections`，缺 opening baseline 仍为 `INCOMPLETE`；Algo 库存/创建/撤销统一走 typed Adapter，缺 `algoId/symbol/side/orderType/triggerPrice/algoStatus` 直接 UNKNOWN；用户事件无单调序列或出现 gap 时进入 `SEQUENCE_UNAVAILABLE/GAP` | 旧链在 SQLite 写失败后可能留下“内存已记账、重启缺事实”；Algo raw list 可缺 owner-critical 字段；用户流重连没有可验证的连续性语义 | `pytest -q` → 997 passed, 1 skipped；定向 Adapter/reconciliation/store/architecture → 45 passed；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `b6b6f526f90448a6bf063ad7283d36e52f93e5bf`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T03:59:16Z | BD-V3-02 state backend fail-closed | 配置声明未接入的 PostgreSQL/其他后端时，保留仅用于诊断的 SQLite，但设置 `state_backend_supported=false`；`/ready` 与 `/trading-ready` 明确返回 `STATE_BACKEND_UNSUPPORTED`，不再把错误数据库降级当作生产事实库；新增回归测试 | 旧行为只打印 warning 后继续使用 `.beidou/state.db`，存在错误 backend 仍可被误读为可运行 | `tests/unit/test_v3_fail_closed.py` → 7 passed；变更文件 Ruff PASS；代码 commit `a93c1cb3d84507b0a3f1f7fbf717fa3ed407a026`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T04:01:26Z | BD-V3-02 durable ledger conflict boundary | 为 `ledger_transactions.source_event_id` 增加唯一约束；同 transaction 重放保持幂等，不同 transaction 争用同一成交事实直接抛出冲突；可修复“头已提交、分录未齐”的同事务崩溃窗口，禁止 `INSERT OR IGNORE` 静默吞掉跨写者事实冲突 | 跨进程不同 transaction id 可指向同一 source event，持久层可能只保留一份而调用方误以为成功 | `tests/unit/test_store.py` → 5 passed；变更文件 Ruff PASS；代码 commit `a6fe71ec632b392691a93fa3f1e60c1dd79cd397`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T04:15:40Z | BD-V3-04/08 three-way reconciliation and user-stream journal | 新增 `UserOrderUpdate` typed 解析；`user_stream_events` 与 `user_stream_projections` durable journal；事件按 `PENDING → APPLIED` 提交，重复幂等、同 ID 原文冲突拒绝；重启高水位默认进入 `GAP`，必须显式 replay；`ReconciliationEngine.compare_three_way` 与引擎注入边界比较 system/REST/event-stream 三方；opening projection 改为基线 + durable fill replay，成交后余额未独立重建则保持 INCOMPLETE | 旧实现仅有事件序列观察，没有 durable 应用/恢复，system side 可用当前 projection 覆盖 opening 基线，双边 REST 匹配不能证明事件连续 | `pytest -q` → 1003 passed, 1 skipped；新增/变更 user-stream、reconciliation、store 定向测试 33 passed；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `4271a32b991e1a24e79a3de50e618f0270802cc1`；项目 Ruff → 161 errors（既有质量债）；mypy → SQLite 二进制 UTF-8 阻断；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T04:18:26Z | BD-V3-04 numeric fact hardening | 对账余额/仓位拒绝 `NaN`、`Infinity` 和非数字值并返回 typed `ERROR`；opening fill replay 对非法/负数量保持 `INCOMPLETE`，避免损坏 durable 行让比较器异常退出或错误匹配 | 浮点比较可能让非有限值绕过差异判断；损坏成交行可能中断对账循环而没有明确事实状态 | `pytest -q` → 1004 passed, 1 skipped；定向 reconciliation/user-stream/adapter → 34 passed；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `ef58bd005c8f0996c09cc2565af75a2ea5ed0a97`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T04:26:32Z | BD-V3-04 complete account user-stream and replay authorization | 新增 `UserBalanceUpdate`/`UserPositionUpdate`/`UserAccountUpdate` typed 解析；ACCOUNT_UPDATE 仅能在带完整事实、来源/版本、证据哈希、审批 ID 的显式 replay baseline 后应用；无序列流使用 baseline 后单调事件时间门；baseline 后至少一条用户事件才允许 `complete=true`，重启后重新授权 | 旧 projector 只能证明订单事件，余额始终为占位零值；无法以 account update 形成完整第三方事实，重启可能误信旧 baseline | `pytest -q` → 1006 passed, 1 skipped；user-stream/adapter/reconciliation 定向 → 36 passed；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `d1696b6254e90ed0739ef7afa4723b6e37f0a13b`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |
| 2026-08-09T04:34:13Z | BD-V3-02 backup and forward-only PostgreSQL migration slice | 新增 SQLite online backup 与 integrity/foreign-key/必需事实表行数校验；加密仅接受显式 32-byte AES-GCM key；新增 `002_v3_fact_chain.up.sql` 前向 schema 与 checksum-checked migration runner；Compose 移除硬编码 PostgreSQL/MinIO 凭据；`BackupVerification` 缺 reconciliation 事实时不可恢复交易 | 旧灾备类型只校验 restore/ledger/intent，无法证明 reconciliation；迁移目录无 V3 事实链；Compose 存在默认密码风险 | `pytest -q` → 1011 passed, 1 skipped；infra/migration 定向 → 21 passed；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`docker compose config` 使用一次性非生产环境变量 PASS；项目 Ruff 仍有 161 个既有错误；mypy 仍被 `beidou_state.db` 二进制 UTF-8 阻断；代码 commit `159cd1eb049f4a2b71850398eddb074578596fb4`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |

## Commands

- `.venv/bin/pytest -q tests/unit/test_v3_fail_closed.py` → 6 passed
- `.venv/bin/pytest -q tests/unit/test_risk_engine.py tests/unit/test_guard.py tests/unit/test_launcher.py tests/unit/test_infrastructure.py tests/unit/test_certification.py` → 107 passed, 1 skipped
- `python -m compileall -q beidou_* apps` → PASS
- `.venv/bin/pytest --collect-only -q` → 967 collected
- `.venv/bin/pytest tests -q` → 966 passed, 1 skipped
- `pytest -q` → 981 passed, 1 skipped, 127 warnings
- `ruff check <changed files>` → PASS
- `ruff check beidou_* apps tests scripts` → FAIL，161 errors（既有质量债、脚本 print 规则及 SQLite 二进制被扫描）
- `python -m compileall -q beidou_* apps scripts` → PASS
- `mypy beidou_* apps --no-error-summary` → FAIL，`beidou_state.db` 无法按 UTF-8 解码
- `pytest -q` (commit `ed9630a`) → 989 passed, 1 skipped, 136 warnings
- `pytest -q` (commit `b6b6f52`) → 997 passed, 1 skipped
- `pytest -q` (after backend gate) → 998 passed, 1 skipped
- `pytest -q` (after durable ledger conflict gate) → 999 passed, 1 skipped
- `pytest -q` (commit `4271a32`) → 1003 passed, 1 skipped
- `pytest -q` (commit `ef58bd0`) → 1004 passed, 1 skipped
- `pytest -q` (commit `d1696b6`) → 1006 passed, 1 skipped
- `pytest -q tests/unit/test_binance_adapter.py tests/unit/test_reconciliation_contract.py tests/unit/test_store.py tests/architecture/test_architecture.py` → 45 passed
- `.venv/bin/pytest -q tests/unit/test_user_events.py tests/unit/test_binance_adapter.py tests/unit/test_reconciliation_contract.py` → 33 passed
- `ruff check` changed runtime/store/exchange/tests → PASS
- `python -m compileall -q beidou_* apps scripts` → PASS
- `git diff --check` → PASS
- `ruff check` changed runtime/store/reconciliation/protection/tests → PASS
- `python -m compileall -q beidou_* apps scripts` (commit `ed9630a`) → PASS
- `git diff --check` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → exit 1，`NOT_VERIFIABLE`
- `python -m compileall -q beidou_certification scripts/testnet scripts/certification` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → `NOT_VERIFIABLE`；旧模拟证书被 commit/G5 binding/simulation 等检查拒绝
- `ruff check beidou_* apps tests scripts` → FAIL，未解决的既有质量门禁
- `mypy beidou_* apps --no-error-summary` → FAIL，未解决的既有类型门禁

## Limitations

本日志不证明 PostgreSQL、完整账户余额 user-stream/replay/gap-fill、真实 G5、真实 G7、Alpha、备份恢复或 24×7 运行已完成。未执行服务重启、部署、交易所写操作、撤单、平仓或凭据变更。变更文件测试通过不等于持续盈利证明。
