# 验收矩阵

| Gate | 必须满足 | 失败处置 |
|---|---|---|
| G0 | 范围、权限、当前 SHA/进程/DB 证据齐全 | 停止变更，补取证 |
| G1 | 需求可追溯到任务/测试/证据 | NOT_VERIFIABLE |
| G2 | 目标架构无双执行链、无 fail-open | HOLD |
| G3 | P0 task package 有依赖、rollback、owner | 不进入实现 |
| G4 | pytest、lint、format、mypy、compile、diff、覆盖率（当前配置要求 ≥85%）全部通过 | 不交付；当前覆盖率 57.04%，FAIL |
| G5 | 真实 Testnet 16 场景、订单/保护/对账/恢复证据 | NO_NEW_RISK |
| G6 | 独立红队无未关闭 P0/P1 | HOLD |
| G7 | ≥30 日/≥200 周期 fresh SLI、无 P0、成本与 parity 通过 | 窗口重置 |
| G8 | 用户验收、回滚演练、证据完整 | 不发布 |
| G9 | 签名制品、备份/PITR、watchdog、权限与告警 | 不部署 |
| G10 | 仅在明确授权后允许生产候选；Mainnet 仍需另行批准 | PROHIBITED |
