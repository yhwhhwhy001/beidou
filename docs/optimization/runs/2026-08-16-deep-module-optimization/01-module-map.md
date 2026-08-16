# Beidou Module Map（2026-08-16 扫描版）

状态标注：FULLY_IMPLEMENTED / PARTIALLY_IMPLEMENTED / MOCKED / PLACEHOLDER / BROKEN / UNREACHABLE / UNVERIFIED / NOT_IMPLEMENTED

## 唯一生产主链（已实测确认）

```
launchd com.beidou.autopilot（实装 ~/Library/LaunchAgents，KeepAlive=true，30 symbols）
  → beidou CLI（pyproject.toml:51 → beidou_launcher/cli.py:80 main）
    → BeidouSupervisor.run（supervisor.py:1200）
      → preflight（33 项：git_worktree/config/signing_key/signed_policy/state_backend(PG+迁移头)/fencing_token/environment_guard…）
      → AutonomousEngine（beidou_core/engine.py:1157，约 40 组件装配）
      → 三重互锁：exchange write interlock / resume interlock / health callbacks（supervisor.py:152-265）
      → engine.run() 三个时钟域：
          realtime 2s：行情轮询→订单监控→outbox claim→_place_order→UNKNOWN resolve(60s)→对账(30s)
          nearline 30s(testnet)/300s：幽灵清理→excess-order 清理→保护重试→10 标的×4tf：K线→
            RealMarketStateEstimator→typed kernel→SignalFuser→adaptive sizing→TargetDeltaPlan→
            R0-R10 注册表→快照风控→HMAC 签名审批→控制面 Gate→outbox.commit
          offline 300s(testnet)/3600s：因子 IC→drift→universe→日报→MAPE-K→checkpoint→凭证健康
```

**唯一订单写路径**：`_nearline_tick` OrderIntent(engine.py:8897) → 签名审批(8914) → `_control.should_accept`(9004) → `_outbox.commit`(9011) → realtime `claim`(3258) → `_place_order`(4246)：STATE_BACKEND 门(4253)→control gate(4266)→资格门(4292)→`_verify_intent_at_send`(4300)→`_plan_execution`(4493 算法+切片)→`persist_execution_plan`(4519)→逐切片 `_submit_order_slice`(4535)：规则快照(5135)→量化(5203)→nonce(5239)→`adapter.create_order`(5242)。

**权威事实源**：`PostgresPersistentStore` + `PostgresIntentOutbox`（DATABASE_URL=postgresql://…beidou_testnet；实测进程持有 PG 连接）。SQLite `PersistentStore` 仅诊断回退且 `_state_backend_supported=False` 双重 fail-closed 阻断新增风险（engine.py:1320-1393, 4194, 4253）。

## 模块矩阵

| # | 模块 | 位置 | 核心类/函数 | 生产链状态 | 测试 | 关键风险 |
|---|---|---|---|---|---|---|
| M00 | 系统基线与架构治理 | beidou_launcher(13f)、beidou_core/engine.py(10640行)、apps(6f)、beidou_bootstrap | supervisor.run / preflight / AutonomousEngine / EnvironmentGuard | 主链真实；但遗留入口集群与死代码并存 | test_launcher、test_supervisor_*、test_engine_readiness_contract、test_truth_eligibility | P0×4（self-heal 失效、launchd 重启循环、testnet 特赦面、15 blocker 告警风暴） |
| M01 | 行情与 Market Data | beidou_data(12f) + beidou_core/feed.py + beidou_exchange/binance_usdm | MarketDataFeed / KLineGenerator / ClosedBarNormalizer / DataQualityGate / BinanceUsdmWebSocketClient / BinanceRESTClient | PARTIALLY：生产链=feed.py+KLineGenerator；market/orderbook/canonical_bars 等 8 模块为"合约孤岛"（仅测试） | test_market_data(26)、test_market_data_dq(10)、test_orderbook、test_binance_{ws,rest}_client | P0×4：伪造 is_closed、迟到 tick 无防护、本地时钟当交易所时间、DQ 门禁装饰化 |
| M02 | Universe Selection | beidou_data/trading_pool_lifecycle.py + engine.py:9190-9290 | TradingPool / InstrumentScore / _evaluate_trading_universe / _seed_pool_from_history | PARTIALLY：动态五维评分+滞回真实；候选集硬编码（CLI 29 symbols / start_beidou.sh:17 / engine.py:7737,7771,7958 恢复路径硬编码 3 symbol） | test_s3_strategy_portfolio、test_v3_fail_closed、test_remaining_contracts | P1：候选不动态发现；seed 观察期捷径(now-365d) |
| M03 | Feature / Indicator | beidou_core/feed.py:905-1017 + beidou_research/features | _compute_kline_features（RSI14/MACD/ATR/EMA/SMA/Bollinger/波动率/量比/spread） | PARTIALLY：warm-up<20 返回{}→抛 UNKNOWN 正确；RSI/ATR 用简单平均近似 Wilder、macd_signal 非标准公式 | test_market_data、test_strategy_data | P1：指标非标准；特征异常时降级固定 5%/10% 保护(engine.py:10167) |
| M04 | 因子系统 | beidou_research/factors(1187行) | FactorDefinition / FactorLifecycle(12态) / FactorPromotionGate / FactorRegistry / compute_ic(_rank_ic) | PARTIALLY：IC/RankIC/ICIR 真实；neutralization/decay NOT_IMPLEMENTED；turnover 半实现未接线 | test_factor_research、test_promotion_chain、test_rsi_factor | P0：晋级门禁 NaN/Inf 穿透(factor.py:429-436)；ICIR 零方差返回 ±inf(factor.py:575) |
| M05 | 因子挖掘 | beidou_research/mining(~11k行) | MiningRunner / expression_ast(2027行) / template_grid / metrics / purged_walk_forward / cpcv / multiple_testing / EvidenceBundle | PARTIALLY：端到端真实；防作弊大部分真实；但 sign-flip p-hacking、PBO 退化、statistics/validation.py 占位 0.0 分、promotion_chain 全自证 approved=True | test_fw03_e2e_mining、test_bf05_evaluation、test_bf03_ast、test_wfo_leakage | P0×2：全样本符号翻转+翻转后 p 值入多重检验分母；占位统计内核可误接 |
| M06 | 策略模块 | beidou_strategy/alpha(kernel/typed_graph 682行) + engine.py 8 组件(480-1150) | TypedAlphaGraph / MeanReversionEntry / TrendFollowingEntry / BreakoutEntry / filters / RealMarketStateEstimator | PARTIALLY：fail-closed 真实；但纯指标条件拼接、8 个 validate() 恒 True 桩、Exit 输出在实盘被丢弃(engine.py:8294 死键) | test_bf08_typed_graph(360行)、test_alpha_graph、test_engine_signal_contracts | P0：Exit 死键；P2：两套内核语义分裂(TypedStrategyKernel 仅测试) |
| M07 | 策略组合与 Portfolio | beidou_strategy/portfolio + signal_fusion | PortfolioOptimizerImpl(等权) / optimize_portfolio(仅对角方差) / SignalFuser(方向加权平均) / hedge_netting | PARTIALLY：实盘=单信号+等权+取最大 strength(engine.py:8307-8316,8355,8530)；协方差/风险预算未接线 | test_strategy_portfolio、test_s3_strategy_portfolio、test_safety_domain_contracts | P1：无真实协方差/风险预算；conflict 仲裁 0.6 阈值单策略下永不触发 |
| M08 | 回测与研究系统 | beidou_research/mining/evaluation + backtest/replay | StrategyPnLKernel / simulate_paper_window / ReplayValidator / KlineStore | PARTIALLY：PnL 内核唯一权威但 total_return=log(1+Σr) 数学错误(pnl_kernel.py:53)、Sharpe per-bar vs Sortino/Calmar 年化量纲分裂、Profit Factor 未实现、partial fill 无订单级撮合 | test_pnl_kernel、test_wfo_numpy_parity、test_simulate_paper_window | P1：数学错误+量纲；成本硬编码 5/8bps |
| M09 | 动态仓位与杠杆 | engine.py:443-472,8424-8440,8516 + beidou_strategy/risk/adaptive_sizing_engine.py | adaptive_leverage(硬编码 3/2/1/0.5 档) / adaptive_position_pct / compute_adaptive_sizing(未接线) | PARTIALLY：真实动态引擎仅测试引用；实盘=硬编码分级+×0.5帽+1.5×ATR 止损+10% 资本预算 | test_legacy_safety_algorithms、test_adaptive_protection | P1：生产 sizing 硬编码；correlation/liquidity/drawdown 不进公式 |
| M10 | Risk Engine | beidou_safety/risk(engine 300-962) + rules.py + beidou_control | PreRiskCheckerImpl(生产死代码) / RiskRuleRegistry R0-R10 / RiskEngineImpl(仅 R0/R4/R5) / RiskSnapshot(不可变+hash,无签名) / RiskApprovalSignerImpl(HMAC) / RiskLevelManager / CONTROL_ALLOW_MATRIX | PARTIALLY：**双轨风险引擎规则集不一致**；R0-R10 实现齐全但输入 context 待审计；Kill Switch 无命名组件（等效=控制矩阵） | test_risk_engine(_fail_closed)、test_risk_rules、test_approval_lifecycle、test_truth_eligibility | P0：双轨；P1：快照无签名、PreRisk 死代码 |
| M11 | Order Execution（P0 模块） | beidou_safety/execution(intent.py 1033行) + beidou_infra/outbox.py + engine.py 4246-5370 | OrderIntent / IntentOutbox / PostgresIntentOutbox(claim+fencing token) / _place_order / ExecutionPlanEngine / OrderAggregate / command_aggregate | 主链完整：UNKNOWN 先查交易所不盲发(engine.py:4029-4105)、三层幂等键、单调性守卫（**未提交**，fe62da152 已 reset）；三套订单状态机并存 | test_execution(_algorithms)(_command_aggregate)、test_order_machine_fail_closed、test_postgres_outbox、test_executor_fencing | P0：单调守卫未提交（脏树阻断重启）；P2：三套状态机、rest_client 裸 print、idempotency sha256 截 64bit |
| M12 | Position / TP / SL | beidou_safety/position + protection + engine.py:2544-2665 | PositionProjection(三重幂等) / StopLossCalculator(5 型真实) / TakeProfitCalculator(3 型真实) / _assess_protection_coverage / _maybe_emergency_close_unprotectable | PARTIALLY：实现真实；**当前运行态 15 持仓 MISSING_SL/MISSING_TP 且保护重试侧 positions=0**（两侧事实源矛盾，"Protection ownership unknown" CRITICAL） | test_protection(_engine_fail_closed)、test_position_lifecycle、test_position_truth | P0(live)：保护事实源分裂；P2：exchange_protection.py:96,111 硬编码 round(...,8) |
| M13 | 账户事实与对账 | beidou_safety/execution/reconciliation.py + ledger.py + engine.py:6712-6970 | ReconciliationEngine(三方/同源伪造检测) / ImmutableLedger(复式+借贷平衡) / MANUAL_REPAIR_REQUIRED | 主链真实且当前 MATCHED；open_orders 仅比 ID 集合(reconciliation.py:305)；balance 容差按环境(testnet 1%) | test_reconciliation_*（4 文件）、test_s4_ledger_reconciliation | P2：ID 集合比较简化；Ledger 双实现 |
| M14 | 生命周期系统 | beidou_research/factors/factor.py + beidou_strategy/alpha/model_registry.py + beidou_lifecycle | FactorLifecycle(12态+证据表) / FactorPromotionGate / ModelRegistry / ModuleState(15态) | PARTIALLY：因子生命周期真实；**Champion 在线自动替换仅凭 ICIR≥0.3**(engine.py:9148-9163)无门禁无回滚；TradingPair/Strategy 生命周期 NOT_IMPLEMENTED | test_factor_research、test_promotion_chain、test_model_control | P0×2：Champion 在线改写、NaN/Inf 晋级穿透 |
| M15 | Evolution / 自进化 | beidou_autonomy(mapek.py 343行) + engine.py:9295-9471 | MAPEKController / RecoveryPlanner / 离线循环(IC评估→drift→universe→MAPE-K→checkpoint) | PARTIALLY：Observe/Detect 真实；Generate Challenger/Research/OOS/Paper/Testnet 闭环 NOT_IMPLEMENTED；恢复走日志-only execute_recovery(engine.py:9433)；指纹库空→LOCK 默认 | test_mapek、test_legacy_zero_coverage_contracts | P0：日志-only 恢复无控制面副作用；P1：无 rollback、重启计数双套 |
| M16 | 数据与存储 | beidou_infra(9f) + migrations(001-006) + beidou_core/store.py | PostgresPersistentStore / PersistentStore(SQLite) / PostgresIntentOutbox / migrations forward-only+checksum / EventStore(内存) / AtomicPersistence(纯内存) | PARTIALLY：PG 主链+迁移头校验真实；PITR/restore/backup 演练 UNVERIFIED；LeaseManager(lease.py)未接线引擎；ha.py 纯声明 | test_postgres_store、test_postgres_outbox、test_core_store_contracts | P1：内存事件仓、PITR 无证据；P2：双库残留(beidou_state.db) |
| M17 | API / Frontend | beidou_control/api.py(FastAPI) + beidou_core/health.py + .beidou/monitor_evidence.jsonl | control API(紧急动作真实执行) / /health | PARTIALLY：控制面 API 真实；无独立前端（数据经 health+supervisor-state 呈现） | test_control_api、test_control_gate | P2：无 UI 字段溯源审计（待 M17 专项） |
| M18 | Monitoring / Observability | beidou_observability(32f) + supervisor 监督循环(5s) | collect_monitoring_checks(8类) / frequency_policy(600/1800/3600s) / IncidentManager(FSM,未接线) / AlertSuppressor(P0 不抑制) / P0ImmediateTrigger(未接线) / watchdogs | PARTIALLY：数据真实观测；**频率策略不门控实际检查节奏**(supervisor.py:734-740 仅诊断)、未持久化；IncidentManager 未接入；当前告警风暴 | test_observability、test_monitoring_services、test_operational_fact_bus | P1：频率门控失效、Incident 未接线；因子 stale 检查 last_evaluation=0 永久失效 |
| M19 | Self-Healing | beidou_safety/execution/recovery.py + supervisor.py:879-1060 + beidou_autonomy | RecoveryEngine(7态) / _recover_if_validated / _maybe_testnet_auto_reauthorize / RecoveryOrchestrator | PARTIALLY：恢复链真实；**testnet 自动 RESUME 绕过 TruthSnapshot/authorize_resume（且不检查 --no-self-heal）** | test_recovery_engine_fail_closed、test_supervisor_testnet_auto_resume | P0：自动 RESUME 旁路；--no-self-heal 语义失效 |
| M20 | Security / Configuration | beidou_security(3f) + config/ + .env + EnvironmentGuard | CredentialRegistry(JSONL 轮换) / SecretSanitizer / Permission(withdraw 拒绝) / EnvironmentGuard(production/mainnet blocked) | PARTIALLY：密钥全 env 化、.env 被 gitignore；vault.enabled=false 无 KMS；start_beidou.sh 默认签名 key fallback + 弱密码自动生成 | test_credential_validator、test_architecture(secret 检查)、test_evidence_tamper | P0：start_beidou.sh 默认 key+伪造 G5 证书；P1：无 KMS |
| M21 | CI / Testing / Supply Chain | pyproject.toml + .github/workflows/ci.yml + requirements.lock(37行) + requirements_lock.txt(57行) + artifacts/sbom | pytest(2496 passed 实测) / ruff(38 errors) / mypy(strict+15 模块 ignore_errors) / coverage(fail_under=85,实测 66.5%) / bandit / pip-audit | PARTIALLY：CI 存在但 critical_packages 95/90 表未执行、pre-commit ruff v0.3.0 vs 本地 0.16.1、lock 三版本矛盾、SBOM 过期 | 全量 2496 passed 0 skipped（2026-08-16 实测 65.5s） | P0：供应链 httpcore2/httpx2 可疑包；P1：15 模块 mypy 豁免含 beidou_core.engine |
| M22 | Deployment / 24h 无人值守 | deploy/com.beidou.autopilot.plist + 实装 ~/Library/LaunchAgents + start_beidou.sh + docker-compose.yml | launchd KeepAlive 托管 / SIGTERM 挂起需 SIGKILL(已知) / 自动拉起 | PARTIALLY：launchd 托管真实运行；**实装 plist 与模板漂移（30 vs 2 symbols、KeepAlive 相反）；KeepAlive=true 与终态退出码 5/6 冲突→崩溃重启循环风险** | test_startup_guard、test_environment_parity | P0：重启循环风险；P1：minio:latest 未锁、docker-compose migration 无 checksum |

## 旁路与重复实现清单（M00 收敛对象）

1. **双 store**：SQLite PersistentStore ↔ PG PostgresPersistentStore（PG 为主链，SQLite 诊断回退 fail-closed——保留但需防漂移）
2. **三套订单状态机**：order_state.py / order_state_machine.py / order_machine.py+command_aggregate.py（迁移中途）
3. **双轨风险引擎**：RiskRuleRegistry R0-R10 ↔ RiskEngineImpl.full_evaluate（仅 R0/R4/R5）
4. **双策略内核**：TypedStrategyKernel（仅测试）↔ TypedAlphaGraph（生产）
5. **双 PositionAggregate / 双 RiskSnapshot / 双 Ledger**（新旧并存）
6. **apps/ 遗留入口集群**：autopilot/strategy_engine(硬编码 paper)/safety_executor/research_lab(占位)/factor_miner(部分占位) + beidou_bootstrap DEV_BYPASS
7. **engine.py 死代码**：run_parity_check/build_strategy_signal/build_execution_plan/build_position_aggregate/build_idempotency_key/_sync_exchange_state UNREACHABLE；_cert_manager/_production_ladder/_kernel_parity 死接线
8. **testnet 特赦 8 类**：event 漂移降级(6957)、覆盖豁免外部持仓(2584)、user stream 活性(2705)、清算价推导(8682)、对账 300s(8783)、liveness 放宽(8821)、自动 RESUME(3318)、零写跳过对账(8769)
9. **市场数据"合约孤岛"8 模块**：market.py/orderbook.py/contracts.py/datasets.py/universe_hysteresis.py/trading_pool.py/canonical_bars.py/FeatureStore(PIT 查询侧)
10. **统计验证双轨**：statistics/validation.py（占位 0.0 分，无消费者）↔ mining/evaluation/*（真实实现）
