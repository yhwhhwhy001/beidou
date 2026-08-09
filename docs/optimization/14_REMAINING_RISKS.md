# 剩余风险

> 证据刷新（2026-08-09 15:50）：当前 HEAD（最近观测）`62ed6ef85d4b41b553e5dd380afbda56f047f225`，工作树 1 项未提交安全覆盖；外部 Testnet 进程仍在运行，监督未形成 READY 证书。以下风险仍未解除。

- 当前工作树 DIRTY，代码未提交/部署；外部 Testnet 启动进程已失败，不能视为已通过门禁。任何未来 Testnet 启动仍应被 preflight/构造验证阻断直到制品与秘密边界齐全。
- SQLite 不是外部灾备；没有已验证的 PG/PITR、异机恢复和密钥托管。
- 交易所 user-stream、REST gap-fill、Algo owner/generation 真实恢复尚未验证。
- 交易池和因子证据不足时会保持关闭，这是正确结果，不应手工激活。
- 本地测试、历史证书、健康端点、模拟成交和正 IC 都不能证明未来盈利。
- Python/运行时、远端 CI、时钟、磁盘、网络、watchdog 和权限混沌仍需实机证据。
- CI 覆盖率门当前实测 56.66%（阈值 85%）；`-W error::ResourceWarning` 全量回归已清零资源警告，但不能用全量测试通过替代覆盖率与生产恢复验收。
- 硬编码扫描本轮无阻断项；生产制品仍需逐项审计 localhost/tmp 路径与批准边界。
- 本地 `.venv` 未提供 Bandit；`pip-audit` 需要外部 PyPI 查询，本轮因无结果/超时中止，安全供应链门仍 `NOT_VERIFIABLE`。
- 先前外部自动提交 `ab5897c` 清理了若干已跟踪的自动生成 evidence artifacts；本轮未用 destructive Git 操作恢复，历史证据保留/恢复需单独授权和核对，不能把当前目录缺失视为“从未发生”。
