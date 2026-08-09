# 最终验收报告（当前轮）

## 结论

**HOLD / NOT READY FOR UNATTENDED TRADING**。

本轮代码切片完成了若干 fail-closed 修复并有本地测试证据，但没有完成真实交易所快照、真实 G5/G7、PostgreSQL/PITR、gap-fill、parity、观察窗口或盈利证据。不能交付“24 小时持续盈利运行”声明。

06:39 UTC+8 的只读运行态仍反证证书语义：旧 SHA 进程心跳 stale、BNBUSDT 保护 P0、监督 `trading_ready=false`，却同时暴露 `/health=HEALTHY`、`/ready=true`/`RESUME`；`/trading-ready` 才返回 503。该实例必须保持 HOLD。

本轮本地门禁：`.venv/bin/pytest -q` 为 **1043 passed, 1 skipped**；Ruff lint/format、CI 包范围 mypy、compileall、`git diff --check`、测试质量与禁止模式扫描、生产 TODO/no-op pass 扫描均通过。但 CI 覆盖率命令实测 **57.04% < 85%**，因此 G4 仍 FAIL；硬编码扫描无阻断但有 10 条非阻断告警。上述只证明部分当前工作树代码质量，不证明新代码已部署或运行实例已切换。

## 必须先完成

- 取得授权后处置旧运行实例并保存全部证据；
- fresh authority/readiness 与实际写入互锁一致；
- PG/WAL/PITR、user-stream replay/gap-fill、保护 exact match、crash/chaos；
- 真实 Paper/Shadow/Testnet 窗口和独立红队复核。
