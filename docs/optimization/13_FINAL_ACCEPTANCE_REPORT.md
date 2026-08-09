# 最终验收报告（当前轮）

> 证据刷新（2026-08-09 15:11）：当前 HEAD `b2a6bce587ea13253c2b2bd42141146727333f43`，工作树仍有 9 个未提交修改；最新监督状态为 `PREFLIGHT/BLOCKED`（未提交工作树、缺少 `BEIDOU_SIGNING_KEY`），无服务在线。以下历史运行态只作为历史证据，不得与当前状态混合。

## 结论

**HOLD / NOT READY FOR UNATTENDED TRADING**。

本轮代码切片完成了若干 fail-closed 修复并有本地测试证据，但没有完成真实交易所快照、真实 G5/G7、PostgreSQL/PITR、gap-fill、parity、观察窗口或盈利证据。不能交付“24 小时持续盈利运行”声明。

历史 06:39 UTC+8 只读运行态曾反证证书语义：旧 SHA 进程心跳 stale、BNBUSDT 保护 P0、监督 `trading_ready=false`，却同时暴露 `/health=HEALTHY`、`/ready=true`/`RESUME`；`/trading-ready` 才返回 503。该实例证据不能继承为当前健康证明。

本轮本地门禁：`.venv/bin/pytest -q` 为 **1028 passed, 1 skipped**；Ruff lint/format、CI 包范围 mypy、compileall、`git diff --check`、测试质量/禁止模式/硬编码扫描均通过。但 CI 覆盖率命令实测 **56.50% < 85%**，因此 G4 仍 FAIL；同时保留 49 条 ResourceWarning。上述只证明部分当前工作树代码质量，不证明新代码已部署或运行实例已切换。

## 必须先完成

- 取得授权后处置旧运行实例并保存全部证据；
- fresh authority/readiness 与实际写入互锁一致；
- PG/WAL/PITR、user-stream replay/gap-fill、保护 exact match、crash/chaos；
- 真实 Paper/Shadow/Testnet 窗口和独立红队复核。
