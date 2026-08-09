# 剩余遗漏项对照与本轮收敛（2026-08-10）

本文件是 `pasted-text.txt`、`02_GAP_MATRIX.md`、`06/08_ACCEPTANCE_MATRIX.md`、
`14_REMAINING_RISKS.md` 与当前代码/测试的差异记录。它只记录可在本地闭环的修复；
真实交易所、PostgreSQL/PITR、user-stream、崩溃重放和经过时间仍必须由独立环境重新取证。

## 已补齐的遗漏

| 主题 | 原遗漏 | 本轮落地 | 证据/门禁 |
|---|---|---|---|
| 交易所边界 | `tools/`、`scripts/` 存在自有 REST/订单旁路 | 旧 live/e2e 工具改为不可执行退役入口；历史诊断与 G5 只经 `BinanceUsdmAdapter`/`Endpoint` | `delivery/scripts/check_forbidden_patterns.py` 全仓扫描通过 |
| G5 安全参数 | URL、最大名义金额在 runner 内有默认值；失败回包被压成空结构 | plan 必须显式提供 environment、`mainnet_prohibited=true`、有限正名义金额和 Testnet URL；Adapter 响应缺失/失败保持 UNKNOWN | `scripts/testnet/run_g5.py`；未执行真实写场景 |
| 证据不可伪造 | certification `EvidenceBundle` 哈希只覆盖 checksum/状态；PASS 不要求引用证据；证书上下文可为空 | 哈希覆盖 manifest、完整证据元数据、场景引用、创建时间；PASS 要求绑定证据和 provenance；证书缺上下文/签名不可验证 | `tests/unit/test_certification_evidence_bundle.py` |
| 场景认证 | `ScenarioResult.PASS` 可在 required evidence 缺失时通过 | evaluate 时缺证据改为 `NOT_VERIFIABLE` | `tests/unit/test_certification.py::test_pass_without_required_evidence_is_not_verifiable` |
| 混沌恢复 | `run_chaos_cycle` 自行写入 `recovered=True/invariants_ok=True` | 无独立 observer 时显式 UNKNOWN/未通过；仅接受严格布尔和有限耗时的外部观测 | `tests/unit/test_model_control.py` |
| 资本阶梯 | 配置读取失败回退非零硬编码阶梯，可能误晋级 | 配置未知仅保留零资本 shadow，晋级阻断；单元测试显式注入完整测试配置 | `beidou_certification/engine.py`、`beidou_production/ladder.py` |
| 研究持久化 | PostgreSQL 因子存储默认连接 `localhost`，运行时自动建表，读取错误压成空集合 | 必须显式提供 DSN；连接/表 schema 未验证时不可用且记录原因；运行时不再建表 | `beidou_research/mining/persistence.py` |
| 行情未知语义 | Feed 请求/结构失败返回 `{}`/`[]`，WS 启动失败后健康仍可为真，聚合异常被静默吞掉 | 失败抛出 `MarketDataUnknownError`；WS/聚合失败记录 UNKNOWN 并使健康门失败；同步兼容包装器可安全嵌套事件循环 | `tests/unit/test_market_data.py` |
| 时钟域 | Feed/Health/G7 uptime、WS freshness、错误衰减使用 wall clock | Feed、Health、G7 经过时间与淘汰改用 monotonic；墙钟仅用于审计显示 | `beidou_core/feed.py`、`beidou_core/health.py`、`beidou_launcher/g7_tracker.py` |
| CI 扫描范围 | CI 未编译/lint `tools`、`delivery/scripts`；禁止模式扫描允许脚本旁路 | CI 纳入脚本/工具；禁止模式只允许交易所 Adapter 和明确的本地健康轮询 | `.github/workflows/ci.yml`、forbidden scan |

## 仍然阻断（本轮没有伪造关闭）

1. 全仓覆盖率仍为 **66.77%**（1273 passed、1 skipped；1274 collected），低于 85% 总门；关键链路的行/分支覆盖率尚未达到发布要求。
2. `bandit`、`pip-audit` 当前本地虚拟环境未安装，安全门只能记为 `NOT_VERIFIABLE`。
3. PostgreSQL authority、migration head/checksum、真实事务 Outbox、PITR/restore、crash/chaos/replay 仍无独立运行证据。
4. Binance user-stream 的 listen-key、断线 replay/gap-fill、订单协议矩阵、原生保护 ACK 和三方对账尚未执行；历史证书不继承。
5. Paper/Shadow 与 Testnet 的成本、容量、净 PnL、OOS、真实经过时间窗口仍不足以支持 G6/G7，更不能支持 Mainnet 或持续盈利结论。
6. 生产配置/签名密钥/withdraw 权限/fencing token 等 preflight P0 仍按原报告保持 HOLD；本轮没有重启、部署、下单、撤单、平仓或密钥变更。

本轮增量还将 Health uptime 与 G7 诊断窗口的经过时间/样本淘汰切换为 monotonic 时钟，墙钟仅保留为审计显示；对应合同测试已纳入全量回归。

## 重验顺序

`全量测试与覆盖率 → 安全依赖安装/扫描 → PostgreSQL authority/PITR → user-stream/replay/gap-fill → venue protection/三方对账 → Paper/Shadow parity → 独立 G5 → 从零开始 G7`。

任何步骤出现 UNKNOWN、证据引用断裂、保护覆盖不完整、对账不匹配、重复/孤儿单或告警送达失败，立即保持 `NO_NEW_RISK/EXIT_ONLY/LOCK`，并重置对应经过时间窗口。
