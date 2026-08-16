# 北斗逐模块深度优化 — 最终验收报告

日期: 2026-08-16
Commit 链: 3f85456 → 02efa69(main,工作树干净)

## 模块闭合总览

| 模块 | 主题 | 状态 |
|---|---|---|
| M00-M14 | 基线/行情/宇宙/指标/因子/挖掘/策略/组合/回测/仓位/风控/执行/保护/对账/生命周期 | 已闭合(含 R2) |
| M15 | 自进化 MAPE-K | 已闭合(a50ebc9) |
| M16 | 存储(镜像漂移/扩列/PITR) | 已闭合(d5bf0ca + R2 32e392b) |
| M17 | API(exit-ready/factors 接线) | 已闭合(754a8af) |
| M18 | 监控(频率门控/因子 stale) | 已闭合(a5b2eac) |
| M19 | 自愈(授权链 P0-12 + 人工 RESUME) | 已闭合(771b586 + 536cb15) |
| M20 | 安全(密码/G5 显式化/PG scram) | 已闭合(5e7313c) |
| M21 | CI(供应链/mypy/coverage) | 已闭合(48933b7) |
| M22 | 部署(PITR 实装/launchd 受控重启) | 已闭合(02efa69) |

## 质量门禁实测

- **全量测试**: 2693 passed / 0 failed(63s)
- **ruff**: 干净(本会话清理后零新增;基线 32 保持受控)
- **mypy**: 全量 0 errors(豁免列表外 10 个错误清零)
- **coverage**: 78%(66.5% → 78%,本会话测试增量;CI fail-under 已对齐实测)
- **对抗审查**: 5 个模块补审(M10/M11/M13/M14/M16),全部 FAIL → 15+ CONFIRMED_BUG 全部处置闭合

## 真实闭环验证(testnet,实测日志证据)

1. **启动授权链首次真实运行**(M19-F01):
   `RESUME DEFERRED: TruthSnapshot gate rejected — TradingEligibility=NOT_VERIFIABLE`
   —— 冷启动事实未齐备时 authorize_resume 正确拒绝,不再盲置授权。
2. **testnet 自动重新授权兜底**(EXEMPT-07 语义保留):
   `testnet auto re-authorized RESUME after clean recovery`
   —— 事实齐备后闭环恢复,无死锁。
3. **最终状态**: `trading_ready: True, reason: SUPERVISOR_VALIDATED`,
   liveness HEALTHY,引擎在新代码上稳定运行。
4. **PITR**: 宿主机 PG archive_mode=on / wal_level=replica /
   archive_timeout=300 生效(受控重启窗口执行,连接实测正常)。
5. **PG 认证加固**: host 行 scram-sha-256,错误密码实测拒绝,
   正确密码实测通过,回滚备份 pg_hba.conf.bak-m20。

## R-M03-1 阈值重标定(证据已生成,决策:登记风险接受)

2026-08-16 已跑 M08 修正内核后回测(scripts/recalibrate_r_m03_1.py,
3 品种 × 60 配置,证据:evidence/M03/r-m03-1-recalibration.{md,json}):

1. **阈值扫描**:全部配置 paper_ir 为负,相邻阈值差 < 0.01(噪声级)
   —— 重标定无可测收益,不改阈值。
2. **RSI 阈值等效映射**(旧 SMA-RSI → 新 Wilder RSI,三品种一致):
   旧 70 → 新 ≈62(p25≈58, p75≈66);旧 30 → 新 ≈37。当前新数学下
   入场门相对旧数学放宽 6-8 点 —— **显式登记风险接受**;若未来要
   恢复旧入场频次语义,等效阈值 (62, 37),但需新 alpha 证据支撑。
3. R-M03-3(vol 档位)同族,待其证据轮次。

## 已知保留项(显式登记,非遗漏)

- EXEMPT-07 引擎侧自动 RESUME / testnet 自动重新授权(旁路登记在案)
- EXEMPT-08 零写模式对账豁免
- EventStore/AtomicPersistence/ControlPlaneAPI/IncidentManager 为
  未接线组件(诚实化标注,契约测试锁定;接线属架构演进)
- challenger 生成闭环(研究演进方向,生产自进化仅故障恢复)
- order_states stopPrice/reduce_only 已扩列(防线生效);
  事件侧投影器 detail 携带属后续演进(detail_degraded 审计标志承接)
- mypy 豁免列表(带 owner/expiry 治理,expiry 到期逐模块清偿)
