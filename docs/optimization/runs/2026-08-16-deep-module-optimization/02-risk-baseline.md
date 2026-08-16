# 北斗 P0/P1/P2 风险基线（2026-08-16，扫描时 HEAD d91035a）

标注规则：file:line 来自侦察代理（模块修复前须复核）；`[live]` = 活体运行证据；`[工作树]` = 未提交修改。

## P0（20 项 — 按威胁面排序）

| # | 模块 | 风险 | 证据 |
|---|---|---|---|
| P0-01 | M00/M19 | `--no-self-heal` 语义失效：`_maybe_testnet_auto_reauthorize` 不检查 `self_heal`，testnet 故障后仍自动 RESUME | supervisor.py:1006-1060, 1144-1145 vs :960 |
| P0-02 | M22 | launchd KeepAlive=true 与监督器终态退出码 5/6 冲突 → 崩溃-重启循环 | supervisor.py:1191/1195 vs 实装 ~/Library/LaunchAgents/com.beidou.autopilot.plist KeepAlive=true,ThrottleInterval=10 |
| P0-03 | M12/M13 | [live] 保护事实源分裂：覆盖检查侧 15 持仓 MISSING_SL/MISSING_TP，保护重试侧 positions=0 → 补挂永远无法执行；CRITICAL "Protection ownership unknown" | supervisor-state.json blockers + launchd 日志 `[nearline-diag] retry: symbols=15 positions=0` |
| P0-04 | M01 | 闭合证据伪造：REST kline 无 x 标志强制 `is_closed=True`（feed.py:412-414）；同步路径丢 is_closed（feed.py:874-884）→ `bar_is_closed` 门禁可被未闭合 bar 放行 | 与 market.py:151-154 不变量冲突 |
| P0-05 | M01 | 乱序/迟到 tick 无防护：KLineGenerator.process_tick 把迟到 tick 并入当前 bar（klines.py:111-133），无序列检查 | 生产链用此聚合器 |
| P0-06 | M01 | 时钟漂移：tick 时间戳用本地 `datetime.now()`（feed.py:194,543,827）当交易所时间切 bucket；CLOCK_SKEW 检查全仓零调用 | quality.py:101 无调用方 |
| P0-07 | M01 | DQ 门禁装饰化：DataQualityGate 构造后从不求值（feed.py:594-609），FAIL 不阻断任何下游 | engine.py 无 DataQualityTier.FAIL 门禁引用 |
| P0-08 | M05 | p-hacking：全样本 IC 符号决定翻转方向，翻转后 p 值进 BH/Holm 分母 → p 值系统性低估 | runner.py:525-534, 697, 798 |
| P0-09 | M05 | 占位统计内核：statistics/validation.py PurgedWFCV/CPCV/PBO/DSR 全零分占位（scores_fn 从不调用），若被接线即静默放行 | validation.py:79, 139 |
| P0-10 | M04/M14 | NaN/Inf 穿透晋级门禁：`icir < min_icir` 无 isfinite 守卫（NaN 通过）；sample_count 同理 | factor.py:429-436；engine.py:9152 Inf 通过 |
| P0-11 | M14/M15 | Champion 在线自动替换：ICIR≥0.3 硬编码即 promote_to_champion，无证据门禁/批准/回滚 | engine.py:9148-9163；model_registry.py:52-65 |
| P0-12 | M14/M19 | RESUME 授权链空转：`authorize_resume`（TruthSnapshot 门禁）生产零调用；启动直接置标志；testnet 自动 RESUME 旁路 | plane.py:465-490 仅测试；supervisor.py:1293-1296, 1006-1060 |
| P0-13 | M15 | MAPE-K 恢复日志-only：离线循环调 `execute_recovery` 而非 `execute_with_authority`，无控制面副作用 | engine.py:9433 vs mapek.py:244-343 |
| P0-14 | M10 | 双轨风险引擎：R0-R10 注册表与 full_evaluate（仅 R0/R4/R5 配置）并行评估，规则集不一致 | engine.py:8753-8754 vs 8858-8863 |
| P0-15 | M06 | 类型化图 Exit 输出在实盘被丢弃：engine.py:8294 读 `exit_signals` 键，kernel 返回 dict 从不含此键 → 退出信号死路径 | kernel_parity.py:171-218 |
| P0-16 | M20 | start_beidou.sh 伪造 G5 证书（无场景执行生成 PASS+DEV_BYPASS）+ 默认签名 key fallback + 时间戳弱密码 | start_beidou.sh:271-281, 199, 157-165 |
| P0-17 | M21 | 供应链可疑包：requirements.lock 含 `httpcore2==2.5.0`/`httpx2==2.5.0`（非 PyPI 真实发行、pyproject 未声明）；三份 lock/SBOM 互相矛盾 | requirements.lock:10,12；SBOM 含 librt/ast_serialize 等 |
| P0-18 | M11 | ~~[已解决] 订单终态单调守卫未提交~~：复核后确认已提交为 `fe62da152`（扫描起始时误读为未提交）；工作树干净，重启阻断解除 | git rev-parse HEAD=fe62da152；git status 仅未跟踪文档 |
| P0-19 | M00 | [live] 15 条重复 P0 blocker 同一 check_id 不合并 → 每 5s 周期告警风暴 + HIGH 告警洪水 | supervisor-state.json blockers；launchd 日志连续 HIGH 告警 |
| P0-20 | M10/M11 | testnet 安全门系统性放宽 8 类（对账 300s/liveness 放宽/覆盖豁免/自动 RESUME 等），无任何特赦路径集成测试 | engine.py:8764-8807, 2573-2592；tests/testnet 仅 5 个静态 parity 测试 |

## P1（关键项，18 项）

| # | 模块 | 风险 |
|---|---|---|
| P1-01 | M09 | 生产 sizing 硬编码：杠杆 3/2/1/0.5 档、×0.5 仓位帽、1.5×ATR 止损钳制、10% 资本预算（engine.py:443-472, 8424, 8440, 8516）；compute_adaptive_sizing/RealCostModel/ConstraintOptimizer 均仅测试引用 |
| P1-02 | M07 | 组合层无真实协方差/风险预算：optimize_portfolio 只用对角方差；实盘单信号+等权；conflict 仲裁永不触发 |
| P1-03 | M08 | PnL 内核数学错误：total_return=log(1+Σr)；Sharpe per-bar vs Sortino/Calmar 年化量纲分裂；Profit Factor 未实现 |
| P1-04 | M05 | PBO 退化为单次比较（multiple_testing.py:261-277 无随机性）；DSR 非标准公式+双侧 p 值；promotion_chain 全自证 approved=True（runner.py:1486-1502） |
| P1-05 | M05 | Residualize/残差因子全样本 OLS 系数 = look-ahead（虽标 NOT_VERIFIABLE 但 IC 基于泄漏数据）；SafeDiv(x,x)→1 等 NaN 不健全自简化 |
| P1-06 | M03 | 指标非标准：RSI/ATR 简单平均近似 Wilder、macd_signal 非标准公式（feed.py:944-969）；spread_bps 默认 2.0 硬编码 |
| P1-07 | M02 | Universe 候选硬编码（CLI/start_beidou.sh:17/engine.py:7737,7771,7958）；seed 观察期 now-365d 捷径 |
| P1-08 | M01 | replay 脚本 BROKEN（4 处独立崩溃：check_closed_bar/status=="CLOSED"/kline_gen.add/FeatureVector kwargs）；FeatureVector data_quality_tier 恒 UNKNOWN |
| P1-09 | M10 | RiskSnapshot 完整性哈希无签名；PreRiskCheckerImpl.check() 生产死代码+内联副本；contracts.py:131-133 reduce-only replay pass 空操作 |
| P1-10 | M13 | open_orders 对账仅比 ID 集合（reconciliation.py:305）；Ledger/PositionAggregate 双实现并存 |
| P1-11 | M11 | rest_client 裸 print+locals() 取错误变量；idempotency_key sha256 截 64bit；三套订单状态机 |
| P1-12 | M18 | 自适应频率不门控实际检查节奏（每 5s 全量跑）、未持久化；IncidentManager/StormDetector/P0ImmediateTrigger 未接入运行时 |
| P1-13 | M18 | 因子 stale 检查 last_evaluation=0 硬编码恒失效；监控读引擎私字段 |
| P1-14 | M16 | LeaseManager（lease.py 双主防护）未接线引擎；EventStore/AtomicPersistence 纯内存；PITR/restore 无独立运行证据 |
| P1-15 | M21 | mypy ignore_errors 15 模块（含 beidou_core.engine，expiry 至 2026-11）；critical_packages 95/90 门未在 CI 执行；coverage 实测 66.5%<85% |
| P1-16 | M00 | 遗留入口集群（apps/×6）+ engine 6 个 UNREACHABLE 方法 + 3 个死接线属性；ReadinessGate 仅 CONFIG+DEPENDENCY 两阶段 |
| P1-17 | M00 | `_chaos_engine` 一旦 BEIDOU_CHAOS_ENABLED=true，monitoring 每轮无 observer 恒 FAIL 注入（monitoring/__init__.py:626-633） |
| P1-18 | M20 | vault.enabled=false 无 KMS；fencing 依赖裸环境变量；scripts/tools 下运维脚本直接持有读写密钥 |

## P2（质量项，抽样）

- **测试 CWD 污染（M00-F02 实测发现 → M21）**：`test_launcher_cli_contracts` 改变进程 CWD 未恢复；颠倒文件顺序运行即 3 个测试失败（FileNotFoundError: pyproject.toml 等）。全量套件按字母序恰好掩盖。M21 修复（fixture 恢复 CWD / 测试不得 chdir 到进程级）。

- 模板 plist 与实装漂移（30 vs 2 symbols、KeepAlive 相反）；minio:latest 未锁；docker-compose migration 无 checksum 幂等
- components/__init__ 文档声称不存在的子目录；README 宣称 81 py/26 测试（实际 432/150）
- symbolic_gp 骨架（伪造 hash）；models/ 空壳；中性化未实现；turnover/decay 未接线；survivorship 未接入
- exchange_protection.py:96,111 硬编码 round(...,8)；algorithms.py:522 兜底价 1.0；conditional.py:63 硬编码 0.8
- factor_mining_policy.yaml 与代码漂移（pbo_threshold 0.30 vs 0.20 等）
- 仓库根 beidou_state.db(1.6MB) 与 .beidou/state.db 遗留双库无清理策略
- pre-commit ruff v0.3.0 vs 本地 0.16.1 漂移；SBOM 过期且三源矛盾

## 质量基线（实测 2026-08-16）

| 检查 | 结果 |
|---|---|
| 全量 pytest | **2496 passed, 0 failed, 0 skipped（65.5s）** |
| ruff check | **38 errors**（engine.py 15 / rest_client.py 13 / cli.py 7 / algorithms.py 1 / 测试 2） |
| ruff format --check | **23 files 需重排** |
| Python | 3.14.6（CI 矩阵 3.12） |
| coverage | 历史实测 66.53%（fail_under=85 不达标）——本轮未重跑，M21 复测 |
| 危险模式 | 0 TODO/FIXME、0 skip/xfail、0 未播种随机、无硬编码密钥、无直接可利用 SQL 注入（store.py:326-328 标识符拼接需加固） |

## 跨模块不变量违反记录（扫描阶段）

- INV-004（市场数据异常不得产生有效交易信号）：被 feed.py:412-414 闭合证据伪造 + DQ 门禁装饰化削弱（P0-04/07）
- INV-005（NaN/Inf 不得通过晋级）：被 factor.py:429-436 破坏（P0-10）
- INV-006（Paper/Testnet/Production 语义一致）：testnet 特赦 8 类（P0-20）；53 个旧测试曾把 testnet 自动 RESUME/陈旧事实 ready 编码为期望（上轮审查，待 M21 复查这些测试当前是否仍存在）
- INV-008（自动恢复不得绕过风险状态机）：被 testnet 自动 RESUME 绕过（P0-12）
- INV-009（健康状态必须来自真实观测）：因子 stale 检查 last_evaluation=0 例外（P1-13）
