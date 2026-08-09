# Implementation log

| Time | Task | Change | Pre-change evidence | Post-change evidence | Result |
|---|---|---|---|---|---|
| 2026-08-09T02:15:41Z | BD-V3-01 / BD-V3-03 / BD-V3-05 / BD-V3-06 | 删除 Testnet 内置签名密钥；签名缺失改为 P0；写互锁绑定 authority；P0 不再 transient；`/ready` 绑定 trading readiness；G7 要求真实窗口/样本/P0 invalidation；健康默认 loopback；Shadow 零写 | `tests/unit/test_v3_fail_closed.py` 6 项全部失败；运行态存在 heartbeat/order P0 与 READY 语义矛盾 | 新回归测试 6 passed；配置回归 3 passed；全套 tests 962 passed, 1 skipped；compileall PASS；collect 963 PASS；默认密钥扫描无命中 | PASS_WITH_CONDITIONS |
| 2026-08-09T02:33:28Z | BD-V3-07 / G5-G7 证书语义 verifier | 新增独立、无网络的 G5/G7 证书语义校验；G5 必须绑定当前 commit、完整 16 场景、无 WARN/提款权限/模拟/未来时间/证据缺口；G7 必须绑定 G5 hash 与 commit、真实 30 天、≥200 样本/≥30 日报、无 P0/重置/活动事故；旧 G5 与 fast-forward G7 不可继承；运行器和 G7 启动/评估流程接入 verifier | 旧 G5 仅 7 项且 `can_withdraw=true` 仍 PASS；旧 G7 `is_simulated=true`、commit/G5 hash 为空；评估器重建空内存窗口 | verifier 4 passed；配置/Fail-Closed 回归 13 passed；全套 tests 966 passed, 1 skipped；compileall PASS；旧 G7 评估明确 `NOT_VERIFIABLE` | PASS_WITH_CONDITIONS |
| 2026-08-09T03:38:24Z | BD-V3-04/08 reconciliation, protection ownership and fill idempotency | `ReconciliationEngine.compare` 增加 fresh/complete/future/key typed failures；账户与挂单独立快照及结果写入 durable store；对账失败立即 `NO_NEW_RISK`，运行时不再自愈写入；health/readiness 绑定心跳、控制面、对账和 owner 语义；保护单新增 owner/session/generation/exchange id，只有真实 ACK 才 ACTIVE，未归属 Algo 禁止启动写入/清理/重试；累计成交转 delta 并以唯一 `fill_events` 防重，signed `position_projection` 支持重启投影；缺失市场特征或保护距离阻断 | 旧主链可在不完整系统事实下保持 READY、用本地保护内存代替权威持仓、按 symbol 数量误认保护覆盖、重复累计 partial fill；本地数据库无 reconciliation/fill/projection schema | `pytest -q` → 989 passed, 1 skipped, 136 warnings；新增 reconciliation/owner/fill 测试 7 项；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；代码 commit `ed9630a162e04fec2882c88eb3c24e733ee15bff`；未重启、未连接交易所、未执行写操作 | PASS_WITH_CONDITIONS |

## Commands

- `.venv/bin/pytest -q tests/unit/test_v3_fail_closed.py` → 6 passed
- `.venv/bin/pytest -q tests/unit/test_risk_engine.py tests/unit/test_guard.py tests/unit/test_launcher.py tests/unit/test_infrastructure.py tests/unit/test_certification.py` → 107 passed, 1 skipped
- `python -m compileall -q beidou_* apps` → PASS
- `.venv/bin/pytest --collect-only -q` → 967 collected
- `.venv/bin/pytest tests -q` → 966 passed, 1 skipped
- `pytest -q` → 981 passed, 1 skipped, 127 warnings
- `ruff check <changed files>` → PASS
- `ruff check beidou_* apps tests scripts` → FAIL，163 errors（既有质量债、脚本 print 规则及 SQLite 二进制被扫描）
- `python -m compileall -q beidou_* apps scripts` → PASS
- `mypy beidou_* apps --no-error-summary` → FAIL，`beidou_state.db` 无法按 UTF-8 解码
- `pytest -q` (commit `ed9630a`) → 989 passed, 1 skipped, 136 warnings
- `ruff check` changed runtime/store/reconciliation/protection/tests → PASS
- `python -m compileall -q beidou_* apps scripts` (commit `ed9630a`) → PASS
- `git diff --check` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → exit 1，`NOT_VERIFIABLE`
- `python -m compileall -q beidou_certification scripts/testnet scripts/certification` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → `NOT_VERIFIABLE`；旧模拟证书被 commit/G5 binding/simulation 等检查拒绝
- `ruff check beidou_* apps tests scripts` → FAIL，未解决的既有质量门禁
- `mypy beidou_* apps --no-error-summary` → FAIL，未解决的既有类型门禁

## Limitations

本日志不证明 PostgreSQL、独立三方对账、完整交易所事件 Adapter、真实 G5、真实 G7、Alpha、备份恢复或 24×7 运行已完成。未执行服务重启、部署、交易所写操作、撤单、平仓或凭据变更。变更文件测试通过不等于持续盈利证明。
