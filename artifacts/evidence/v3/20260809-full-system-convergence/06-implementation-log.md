# Implementation log

| Time | Task | Change | Pre-change evidence | Post-change evidence | Result |
|---|---|---|---|---|---|
| 2026-08-09T02:15:41Z | BD-V3-01 / BD-V3-03 / BD-V3-05 / BD-V3-06 | 删除 Testnet 内置签名密钥；签名缺失改为 P0；写互锁绑定 authority；P0 不再 transient；`/ready` 绑定 trading readiness；G7 要求真实窗口/样本/P0 invalidation；健康默认 loopback；Shadow 零写 | `tests/unit/test_v3_fail_closed.py` 6 项全部失败；运行态存在 heartbeat/order P0 与 READY 语义矛盾 | 新回归测试 6 passed；配置回归 3 passed；全套 tests 962 passed, 1 skipped；compileall PASS；collect 963 PASS；默认密钥扫描无命中 | PASS_WITH_CONDITIONS |

## Commands

- `.venv/bin/pytest -q tests/unit/test_v3_fail_closed.py` → 6 passed
- `.venv/bin/pytest -q tests/unit/test_risk_engine.py tests/unit/test_guard.py tests/unit/test_launcher.py tests/unit/test_infrastructure.py tests/unit/test_certification.py` → 107 passed, 1 skipped
- `python -m compileall -q beidou_* apps` → PASS
- `.venv/bin/pytest --collect-only -q` → 963 collected
- `ruff check beidou_* apps tests scripts` → FAIL，未解决的既有质量门禁
- `mypy beidou_* apps --no-error-summary` → FAIL，未解决的既有类型门禁

## Limitations

本日志不证明 PostgreSQL、真实交易所 Adapter、G5、G7、Alpha、备份恢复或 24×7 运行已完成。未执行服务重启、部署、交易所写操作、撤单、平仓或凭据变更。
