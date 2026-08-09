# 剩余风险

- 当前工作树 DIRTY，代码未提交/部署；旧运行进程仍可能使用旧语义。
- SQLite 不是外部灾备；没有已验证的 PG/PITR、异机恢复和密钥托管。
- 交易所 user-stream、REST gap-fill、Algo owner/generation 真实恢复尚未验证。
- 交易池和因子证据不足时会保持关闭，这是正确结果，不应手工激活。
- 本地测试、历史证书、健康端点、模拟成交和正 IC 都不能证明未来盈利。
- Python/运行时、远端 CI、时钟、磁盘、网络、watchdog 和权限混沌仍需实机证据。
- CI 覆盖率门当前实测 57.04%（阈值 85%），并出现未关闭 SQLite `ResourceWarning`；不能用全量测试通过替代覆盖率与资源生命周期验收。
- 硬编码扫描虽无阻断项，仍有 10 条 localhost/tmp/审批告警，需在生产制品前收敛或逐项证伪。
- 本地 `.venv` 未提供 Bandit；`pip-audit` 需要外部 PyPI 查询，本轮因无结果/超时中止，安全供应链门仍 `NOT_VERIFIABLE`。
