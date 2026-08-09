# 剩余风险

> 证据刷新（2026-08-09 15:11）：当前 HEAD `b2a6bce587ea13253c2b2bd42141146727333f43`，工作树 9 项未提交修改；监督状态 `PREFLIGHT/BLOCKED`，P0 为 dirty worktree 与缺失 signing key，9090 无服务。以下风险仍未解除。

- 当前工作树 DIRTY，代码未提交/部署；当前没有运行进程，但任何未来 Testnet 启动仍应被 preflight 阻断直到提交制品与秘密边界齐全。
- SQLite 不是外部灾备；没有已验证的 PG/PITR、异机恢复和密钥托管。
- 交易所 user-stream、REST gap-fill、Algo owner/generation 真实恢复尚未验证。
- 交易池和因子证据不足时会保持关闭，这是正确结果，不应手工激活。
- 本地测试、历史证书、健康端点、模拟成交和正 IC 都不能证明未来盈利。
- Python/运行时、远端 CI、时钟、磁盘、网络、watchdog 和权限混沌仍需实机证据。
- CI 覆盖率门当前实测 56.50%（阈值 85%），并出现 49 条未关闭 SQLite `ResourceWarning`；不能用全量测试通过替代覆盖率与资源生命周期验收。
- 硬编码扫描本轮无阻断项；生产制品仍需逐项审计 localhost/tmp 路径与批准边界。
- 本地 `.venv` 未提供 Bandit；`pip-audit` 需要外部 PyPI 查询，本轮因无结果/超时中止，安全供应链门仍 `NOT_VERIFIABLE`。
