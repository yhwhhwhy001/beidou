# Implementation log

| Time | Task | Change | Pre-change evidence | Post-change evidence | Result |
|---|---|---|---|---|---|
| 2026-08-09T02:15:41Z | BD-V3-01 / BD-V3-03 / BD-V3-05 / BD-V3-06 | 删除 Testnet 内置签名密钥；签名缺失改为 P0；写互锁绑定 authority；P0 不再 transient；`/ready` 绑定 trading readiness；G7 要求真实窗口/样本/P0 invalidation；健康默认 loopback；Shadow 零写 | `tests/unit/test_v3_fail_closed.py` 6 项全部失败；运行态存在 heartbeat/order P0 与 READY 语义矛盾 | 新回归测试 6 passed；配置回归 3 passed；全套 tests 962 passed, 1 skipped；compileall PASS；collect 963 PASS；默认密钥扫描无命中 | PASS_WITH_CONDITIONS |
| 2026-08-09T02:33:28Z | BD-V3-07 / G5-G7 证书语义 verifier | 新增独立、无网络的 G5/G7 证书语义校验；G5 必须绑定当前 commit、完整 16 场景、无 WARN/提款权限/模拟/未来时间/证据缺口；G7 必须绑定 G5 hash 与 commit、真实 30 天、≥200 样本/≥30 日报、无 P0/重置/活动事故；旧 G5 与 fast-forward G7 不可继承；运行器和 G7 启动/评估流程接入 verifier | 旧 G5 仅 7 项且 `can_withdraw=true` 仍 PASS；旧 G7 `is_simulated=true`、commit/G5 hash 为空；评估器重建空内存窗口 | verifier 4 passed；配置/Fail-Closed 回归 13 passed；全套 tests 966 passed, 1 skipped；compileall PASS；旧 G7 评估明确 `NOT_VERIFIABLE` | PASS_WITH_CONDITIONS |
| 2026-08-09T03:12:49Z | BD-V3-02/04/08 execution + research fact-chain convergence | Engine/Feed/Order 写路径收敛到 BinanceUsdmAdapter；Intent/Outbox 与 PersistentStore 共用 SQLite WAL，SENDING/SENT 重启转 UNKNOWN；最终审批/经济字段持久化；成交 ledger journal 可恢复；保护单 PENDING→交易所 ACK 后 ACTIVE；Paper 缺盘口/撮合异常、未闭合 bar、交易所精度未知均 fail-closed；启动恢复不再取消全部未归属 Algo；Mining 接入闭合 bar、manifest 绑定、Purged WFO/CPCV 和多重检验；新增执行/账本/闭合 bar/架构反证测试与 V3 交付方案 | 旧主链包含内存 Outbox/ledger、默认盘口/精度、Paper 即时成交兜底、启动取消全部 Algo；研究 runner 未实际执行样本外/多重检验；旧证书不可验证 | `pytest -q` → 981 passed, 1 skipped, 127 warnings；变更文件 Ruff PASS；`python -m compileall -q beidou_* apps scripts` PASS；`git diff --check` PASS；项目级 Ruff FAIL（既有 163 errors，含 SQLite 二进制扫描）；mypy FAIL（SQLite 二进制不可解码）；旧 G7 evaluation → NOT_VERIFIABLE；commit `bef91c76b4f286c03143ce6dbedf18d53e3d5ee9` | PASS_WITH_CONDITIONS |

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
- `git diff --check` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → exit 1，`NOT_VERIFIABLE`
- `python -m compileall -q beidou_certification scripts/testnet scripts/certification` → PASS
- `python scripts/certification/evaluate_g7.py --window-id g7-20260808-165129` → `NOT_VERIFIABLE`；旧模拟证书被 commit/G5 binding/simulation 等检查拒绝
- `ruff check beidou_* apps tests scripts` → FAIL，未解决的既有质量门禁
- `mypy beidou_* apps --no-error-summary` → FAIL，未解决的既有类型门禁

## Limitations

本日志不证明 PostgreSQL、独立三方对账、完整交易所事件 Adapter、真实 G5、真实 G7、Alpha、备份恢复或 24×7 运行已完成。未执行服务重启、部署、交易所写操作、撤单、平仓或凭据变更。变更文件测试通过不等于持续盈利证明。
