# 执行交付方案（2026-08-09）

## 交付结论

当前交付是“安全收敛切片 + 证据驱动的后续方案”，不是 24 小时无人值守或持续盈利的上线批准。

决策：

- Paper/Shadow：HOLD
- Testnet：HOLD
- Mainnet：PROHIBITED
- 24×7 持续盈利：不作保证；只能在真实净成本、容量、OOS、事实链和经过时间窗口证据齐全后重新评估

## 本轮已执行切片

1. 订单簿 diff 只接受严格连续的 `prev_sequence → sequence`，缺口/错序立即要求 resync 且不修改快照。
2. Paper/Shadow 成本证据独立计数；成本证据缺失或偏差超过门限阻断 Testnet Gate；Paper 撮合成本含独立实现的手续费与价格冲击。
3. Supervisor 启动阶段在任何 P0/P1 blocker 存在时不得授权 `RESUME`；运行时阻断仍进入 `NO_NEW_RISK`/`DEGRADED`。
4. 市场特征不足时不再伪造止损/止盈默认值；保护配置 UNKNOWN 会记录 CRITICAL、关闭新增风险并拒绝创建保护。
5. 执行算法、切片或硬约束不可证明时拒绝 Intent，不回退裸 MARKET/LIMIT 单切片。
6. 因子和交易池禁止 Testnet 启动即 ACTIVE；ACTIVE 因子必须绑定 sealed OOS、成本容量、Paper/Shadow 与具名批准证据。
7. 风险签名新增 `issue_for_approved_risk` 边界，签名不能独立充当风险批准。
8. 监控静态组件默认不宣称健康；测试质量扫描、硬编码扫描和禁止模式扫描恢复为 PASS。
9. `StartupReport.passed` 只有在 `trading_ready=true` 且 supervisor=`RUNNING` 时才为真；启动中/无检查不再生成“通过”假证书。

## 当前证据

- HEAD（最近观测）：`62ed6ef85d4b41b553e5dd380afbda56f047f225`；工作树有 1 个未提交安全覆盖，未部署。
- 全量回归：`.venv/bin/pytest -q -W error::ResourceWarning` → **1034 passed, 1 skipped**（1035 collected）。
- 覆盖率门：`.venv/bin/pytest tests/ -q --cov --cov-report=term --cov-fail-under=85` → **FAIL，56.66% < 85%**；资源警告门已清零。
- Ruff lint/format、CI 包范围 mypy、compileall、`git diff --check`：PASS。
- `scan_test_quality.py`、`check_forbidden_patterns.py`、`scan_hardcoded.py`：PASS。
- 最新监督状态（2026-08-09T07:49Z）：`ENGINE_STARTING`、`trading_ready=false`、`passed=false`；未形成可采信的运行就绪证书。外部 Claude 进程以 Testnet mock signing-key 环境运行，非本轮授权、非本轮证据；我未读取密钥、未启动/停止该进程、未部署或执行交易所写操作。

## G0–G7 判定

| Gate | 当前判定 | 不能宣称的内容 |
|---|---|---|
| G0 数据因果 | CONDITIONAL | 未完成全量 PIT/闭合 bar/数据内容哈希的独立重放 |
| G1 统计 Alpha | FAIL/NEED EVIDENCE | 不能把回测、IC 或历史收益当成未来盈利 |
| G2 组合/成本/容量 | BLOCKED | 未完成真实盘口容量、funding、impact 和跨 regime OOS |
| G3 执行事实链 | CONDITIONAL | 未完成运行时 PostgreSQL、gap-fill、崩溃/混沌重放 |
| G4 代码/覆盖率 | FAIL | 总覆盖率 56.66%，低于 85% 发布门；资源警告门已通过 |
| G5 Testnet | HOLD | 未执行新的真实 16 场景矩阵；历史证书不继承 |
| G6 对抗性生产审查 | FAIL | SRE/执行/量化红队 P0 尚未全部关闭 |
| G7 无人值守 | NOT_VERIFIABLE | 没有真实 30 日/200 闭环、无 P0、无证据缺口窗口 |

## 后续依赖顺序

`P0 取证与隔离 → 唯一 PostgreSQL 事实链 → user-stream/replay/gap-fill → venue-backed protection/三方对账 → Paper/Shadow parity → 真实 G5 → 从零开始 G7 → 独立发布审批`。

任何阶段出现 UNKNOWN、未保护仓位、重复/孤儿单、账本差异、PIT/Parity 缺口或告警送达失败，立即停止新增风险并重置相应经过时间窗口。

## 回滚与权限边界

- 本轮未执行停机、重启、部署、撤单、平仓、余额/密钥变更或 Git 推送；外部运行进程不属于本轮授权动作。
- 代码回滚只能回到 schema 兼容的已验证制品；数据库采用前向补偿，不做未经验证的逆向迁移。
- 先前外部自动生成物清理提交删除了部分 tracked evidence artifacts；在恢复历史证据前必须先核对提交、哈希和来源，本轮不把缺失文件补造为新证据。
- 要继续真实 Testnet，必须先取得具名授权：保存当前 DB/WAL/监督证据、取得交易所只读快照、确认活跃订单归属与保护覆盖，然后再按治理停机流程处置。
