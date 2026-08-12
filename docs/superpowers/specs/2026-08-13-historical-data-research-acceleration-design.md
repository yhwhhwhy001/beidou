# 历史数据加速研究：全链路设计

- 日期：2026-08-13
- 状态：已批准（待实现）
- 关联：BD-CV20/22/23/24、BF-04/BF-07/BF-11、BD-T06 因子晋级门禁

## 1. 背景与问题

北斗 testnet 处于合法 HOLD 状态：因子全部为 IDEA，无任何研究证据驱动晋级。根源有三：

1. **数据瓶颈**：`feed.fetch_klines()` 只支持"最近 N 根"（limit ≤ 1500），无 `startTime` 支持。默认 1h × 500 根 ≈ 20 天数据，sample_count=476，IC 估计纯噪声。2026-08-07 的挖掘运行全部 `gate_decision=FAIL (ic_below_threshold)`。
2. **无历史数据层**：`beidou_research/data/__init__.py` 为空；每次研究运行都重新调交易所 API；无本地持久化、无数据集溯源清单。
3. **研究→运行时桥接缺失**：`evidence/factors/` 中已有 100+ 证据文件，但引擎启动时从不加载；因子注册即 IDEA、`promotion_history` 恒空；引擎 offline 监控只处理 ACTIVE/CHALLENGER/DEGRADED 状态的晋级，从 IDEA 出发永远到不了 CHALLENGER。供给侧（研究）与消费侧（运行时）完全断开。

## 2. 目标

- 用 2 年 × 1h 主粒度 + 1d 辅助粒度的历史数据驱动因子挖掘与评估，产出统计上有功效的 sealed PASS EvidenceBundle。
- 研究产出通过启动扫描桥接自动加载进引擎，经 `FactorPromotionGate` 逐级推进，使因子在 testnet 真正晋级 ACTIVE 并接入 Alpha DAG。
- 保持全部既有安全不变量：fail-closed、证据门禁（BD-T06）、环境隔离（canary/live 不受 replay 证据影响）、testnet 干净工作区要求。

## 3. 非目标（YAGNI）

- 不改动 FactorPromotionGate 的严格性（`strict=True` 恒成立，无环境旁路）。
- 不重写 expression_ast（2000 行，已够用；只消费其 evaluate 接口）。
- 不为 canary/live 打通 replay 证据路径（按环境区分，canary/live 只接受运行时观察证据）。
- 不引入新持久化后端（parquet 用已声明的 research extras；不用新数据库）。
- 不做实时行情流的本地录制/回放（本次只做 REST 历史 K 线）。

## 4. 已确认决策（用户拍板）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 工作范围 | 全链路：历史数据层 + 研究流水线 + 证据桥接 |
| D2 | 评估对象 | 挖新因子并接入（挖掘器挖新因子 → 表达式组件接入 DAG） |
| D3 | 末两级证据 | 按环境区分：testnet/paper/research 接受 `historical_replay` 来源；canary/live 仍要求运行时观察 |
| D4 | 接入方式 | 通用 `ExpressionComponent`，运行时解释表达式，不逐因子手写类 |
| D5 | 数据跨度 | 多粒度组合：1h 主评估 + 1d 稳健性交叉验证 |
| D6 | 桥接方式 | 引擎启动自动扫描 `evidence/factors/`，幂等可重放 |

## 5. 阶段 1：历史数据层

### 5.1 `feed.fetch_klines` 扩展（beidou_core/feed.py）

- 新增可选参数 `start_time` / `end_time`（毫秒时间戳或 datetime，datetime 内部转毫秒）。
- 提供时间范围时按 1000 根/页循环分页（Binance REST 上限），直到覆盖区间或到达 `max_pages`（默认 400 页 ≈ 40 万根）。
- 复用现有 `_api(path, method, signed, params)`：params 直接透传 `startTime`/`endTime`/`limit`，`_api` 本体不改。
- 保护：每页之间 `time.sleep` 遵守权重预算（klines weight=10/请求，限速 1200/min → 每秒 ≤ 2 请求）；tenacity 重试沿用现有 `_api` 机制。
- 不传时间范围时行为与现状完全一致（向后兼容，旧调用方不受影响）。

### 5.2 `beidou_research/data/` 数据层（新）

- `KlineStore`：
  - 存储路径 `.beidou/data/klines/{symbol}/{interval}.parquet`（parquet 依赖 pyproject 已声明的 `research` extras）。
  - `append(symbol, interval, klines)`:按 `open_time` 去重合并（读入现有文件 → concat → drop_duplicates(subset=["open_time"], keep="last") → 写回）。
  - `load(symbol, interval) -> pandas.DataFrame`：列 `open_time, open, high, low, close, volume, is_closed`。
  - `.beidou/data/` 加入 `.gitignore`（数据不入库，manifest 哈希负责溯源）。
- `DatasetManifest`：
  - 对 (symbol, interval, 首末 open_time, 行数) 生成确定性清单：`{"symbol", "interval", "rows", "first_open_time", "last_open_time", "content_sha256"}`。
  - `dataset_manifest_hash = sha256(json.dumps(manifest, sort_keys=True))` 的前 24 位 hex，绑定 EvidenceBundle.dataset_manifest_hash。
  - 落盘 `.beidou/data/klines/{symbol}/{interval}.manifest.json`。

### 5.3 `factor_miner backfill` CLI

```
python -m apps.factor_miner backfill \
    --symbols BNBUSDT,BTCUSDT,ETHUSDT,SOLUSDT \
    --intervals 1h,1d --start 2024-08-01 [--end 2026-08-13] [--max-pages 400]
```

- 逐品种 × 粒度串行；限速 ≤ 2 请求/秒；进度日志每品种每 50 页一行。
- 增量续传：若 parquet 已存在，从文件末 `open_time` 继续拉（`--start` 仅为首次起点）。
- 单页失败：重试 3 次后记录错误并跳过该页，运行结束输出失败清单，退出码非 0。
- `--dry-run` 打印将要执行的 (symbol, interval, 预计请求数) 不实际拉取。

## 6. 阶段 2：研究流水线

### 6.1 数据源切换与 WFO 向量化（beidou_research/mining/runner.py）

- `factor_miner run` 新增 `--from-store` 标志；默认行为：本地 parquet 存在（`KlineStore.load` 成功且行数 ≥ 100）则读本地，否则回退到 `fetch_klines`（旧行为）。`--from-store` 显式指定时本地缺失直接报错退出（不静默回退 API，保证数据集可复现）。

- 候选因子值矩阵化：N_candidates × T 的 numpy float64 矩阵（生成阶段逐候选求值后拼装；T = 时间点数）。
- 收益标签向量化：每 fold 的 label 向量；purge/embargo 边界语义保持不变（fold 划分逻辑不动，只替换 fold 内的 IC/ICIR/decile 计算为矩阵运算）。
- 验收：与现有纯 Python 实现对拍（小样本上 IC/ICIR/shape 结果一致，容差 1e-9），对拍测试固化在 tests/。
- `min_train_samples: 100`、`min_folds_for_verdict: 5` 等 policy 阈值不变（config/factor_mining_policy.yaml）。

### 6.2 多粒度交叉验证

- `MiningRunner.run()` 新增可选参数 `aux_price_data: list[dict] | None`（1d 粒度，`_compute_dataset_hash` 同步计入 aux 数据的 manifest）。
- 主评估在 1h 上执行；每个进入 WFO 的候选在 1d 数据上重算 IC/ICIR 稳健性：
  - 写入 `stability_results`（dimension=`timeframe_robustness`，字段 `ic_1h/ic_1d/degradation_pct/is_stable`）。
  - `is_stable=False` → gate FAIL（fail-closed），`failure_reasons` 追加 `timeframe_unstable`。
- 无 aux 数据时跳过该维度（向后兼容现有调用）。

### 6.3 replay 模拟模块（beidou_research/backtest/replay.py 扩展）

- 历史区间切分：前 75% 研究/OOS 区间；后 25% 作为 paper/challenger 观察窗口（窗口最短 500 个 1h bar，不足则拒绝产出末两级证据）。
- 简化回测内核（新函数 `simulate_paper_window`）：
  - 输入：因子信号序列（bar 粒度）、CostBreakdown、观察窗口 bar。
  - 输出：`paper_sharpe`、`paper_drawdown_pct`、`signal_consistency`（信号与后续收益方向一致率）、`challenger_metrics`（`live_signal_quality` 用窗口内 ICIR 代理、`latency_within_slo` 用 bar 收盘信号下根 bar 可执行性验证）。
  - 费用：使用 `CostBreakdown`（commission/slippage/funding），不模拟撮合深度。
- 所有 replay 证据强制带 `evidence_source="historical_replay"`（字段写入 bundle 与各级 PromotionDecision 元数据）。

### 6.4 逐级证据链生成（研究侧）

一次成功运行产出**从 GENERATED 到 ACTIVE 的完整 PromotionDecision 序列**（8 次转换），写入 `evidence/factors/{factor_id}:{candidate_hash}/{version}.json`（扩展既有格式）：

```json
{
  "factor_id": "BNBUSDT:<candidate_hash>",
  "version": "2.0.0",
  "data": { "...EvidenceBundle 全字段... },
  "promotion_chain": [
    {"from": "IDEA", "to": "GENERATED", "approved": true, "reason": "...",
     "commit": "<git-sha>", "dataset_hash": "...", "policy_version": "2.0.0",
     "falsifier": "factor-miner", "evidence_ids": ["code_compiles","basic_test_pass","economic_rationale"],
     "icir": 0.0, "sample_count": 0},
    "... 共 8 级，逐级到 ACTIVE ...",
    {"from": "CHALLENGER", "to": "ACTIVE", "...", "falsifier": "factor-miner",
     "evidence_ids": ["sealed_oos_verified","cost_capacity_verified","paper_shadow_verified","active_approval"]}
  ],
  "evidence_source": "historical_replay",
  "role": "entry"
}
```

- 每级绑定：`commit`（研究运行时的 git HEAD sha）、`dataset_hash`（DatasetManifest 哈希）、`policy_version`（factor_mining_policy.yaml 的 2.0.0）、Performance（icir/sample_count 来自对应评估阶段）、`evidence_ids`（PROMOTION_EVIDENCE_REQUIREMENTS 规定的清单）。
- PAPER_TRADING/CHALLENGER 两级的证据来自 6.3 的 replay 窗口，`evidence_source` 标 `historical_replay`。
- 向后兼容：旧文件无 `promotion_chain`/`evidence_source` 字段，桥接忽略（不晋级）。
- `role` 字段：挖掘器按表达式语义默认 `entry`；在 `config/factor_mining_policy.yaml` 的 `generation` 段新增 `role` 配置（可选值 entry/filter/exit，默认 entry），运行时读取并写入 bundle。

## 7. 阶段 3：证据桥接

### 7.1 EvidenceBridge（新文件 beidou_core/evidence_bridge.py）

- 引擎初始化（`_factor_registry` 注册 8 个基础因子之后、Alpha DAG 构建之前）调用：
  `EvidenceBridge.load_and_apply(registry, gate, env_mode, evidence_dir="evidence/factors")`。
- 处理流程（每个 `*.json` 证据文件）：
  1. 重算 `artifact_hash`（从 data 字段重建 EvidenceBundle → `compute_bundle_hash()`）与文件内 `artifact_hash` 对拍，不一致 → 拒绝 + incident（category=evidence）。
  2. `is_complete()` / `can_promote()` 失败 → 拒绝 + incident。
  3. 环境来源检查：`evidence_source=historical_replay` 且 `env_mode ∈ {canary, live}` → 拒绝 + incident（`replay_evidence_rejected_in_production`）。
  4. 因子不存在于 registry → 按 bundle 元数据动态注册 `FactorDefinition`（factor_id、version、author=factor-miner、category 按 role 映射）并在 `_factor_component_registry` 注册 `(ExpressionComponent, ())`；已 ACTIVE（`has_authorized_active_evidence()`）→ 跳过。
  5. 逐级应用 `promotion_chain`：每级重建 PromotionDecision 并调用 `FactorPromotionGate.validate_evidence` 复验（不能直接采信文件内 approved 字段），全部通过才逐级 `record.transition` + `promotion_history.append`。任一级不通过 → 该因子整体不晋级（已推进的级别回滚：重新加载前状态，因子保持 IDEA），记录 incident。
- 幂等：重启后重复扫描，已 ACTIVE 因子跳过；未 ACTIVE 因子重新验证（失败同样 fail-closed）。
- 任何失败**不阻断引擎启动**：桥接失败仅记录 incident 与 WARNING 日志。

### 7.2 ExpressionComponent（新文件 beidou_core/expression_component.py）

- 通用 Alpha 组件：构造时绑定 `factor_id` + `Expression`（expression_ast 解析缓存）。
- `compute(context) -> ComponentSignal`：从 context 取所需输入（close/high/low/volume 序列），`Expression.evaluate_series` 求值；求值异常 → 信号 `NO_ACTION` + 告警（不抛出，不炸引擎）。
- 接入 Alpha DAG：桥接注册 `(ExpressionComponent, ())` 后，`_entry_ids`/`_filter_ids`/`_exit_ids` 按 `role` 归类；edges 沿用 DAG 现有 ENTRY→FILTER→EXIT 拓扑。

### 7.3 registry.py 启动检查更新

- `EXPECTED_ALPHA_COMPONENTS` 语义改为"核心 8 个必须注册"；`extra_components` 不再导致 FAIL（动态因子是合法扩展），从 FAIL 条件中移除，改入 evidence 供审计。
- `EXPECTED_FACTORS` 同步：`extra_factors` 不再 FAIL（挖掘因子合法注册）。
- 同步更新 tests/ 中对应断言。

## 8. 数据流全景

```
[阶段1] factor_miner backfill
          → feed.fetch_klines(start,end) 分页 → KlineStore.parquet → DatasetManifest
[阶段2] factor_miner run（读本地 parquet，不调 API）
          → 候选生成 → 预筛 → WFO(1h 向量化) → 1d 稳健性 → 多重检验
          → replay 观察窗口(后25%) → 逐级证据链 → EvidenceBundle.seal()
          → evidence/factors/{factor_id}:{hash}/{version}.json（git 提交）
[阶段3] beidou testnet 启动
          → EvidenceBridge 扫描 → hash 对拍 → 环境来源检查
          → 逐级 validate_evidence → 因子 ACTIVE
          → ExpressionComponent 注册 → Alpha DAG 接线 → 信号 → 下单链
```

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| backfill 单页失败 | 重试 3 次 → 跳过该页记错误 → 结束输出失败清单，退出码非 0 |
| backfill 中断 | 按 parquet 末时间戳增量续传，无重复数据 |
| bundle hash 不匹配（篡改/重生成） | 拒绝 + incident，不阻断启动 |
| canary/live 收到 replay 证据 | 拒绝 + incident（D3 环境区分） |
| 逐级链任一级复验失败 | 该因子不晋级、保持 IDEA、incident |
| 表达式求值运行时异常 | 组件信号 NO_ACTION + WARNING，引擎继续 |
| 无证据文件（现状） | 桥接空转，行为与今天一致 |

## 10. 测试计划

- 阶段 1（tests/unit/test_kline_backfill.py 等）：
  - fetch_klines 分页拼装（mock `_api` 返回多页）、区间裁剪、max_pages 上限、无时间范围时兼容旧行为。
  - KlineStore 去重（重复 open_time 只保留一条）、manifest 确定性（同数据两次哈希一致）。
- 阶段 2：
  - 向量化与纯 Python 对拍（IC/ICIR/decile 容差 1e-9）。
  - 多粒度稳健性：1d 数据存在时 stability_results 含 timeframe_robustness；is_stable=False → FAIL。
  - replay 窗口：窗口 < 500 bar 拒绝末两级证据；费用扣除方向正确。
  - 逐级链：8 级齐全、每级 evidence_ids 覆盖 requirements、binding 字段非空。
- 阶段 3：
  - 有效 bundle → 因子逐级晋级到 ACTIVE 且 `has_authorized_active_evidence()` 为真。
  - 篡改 bundle（改一个 metric）→ 拒绝、因子保持 IDEA。
  - env_mode=canary + replay 来源 → 拒绝。
  - ExpressionComponent 求值正确性 + 异常→NO_ACTION。
  - registry 动态检查：新因子注册后启动检查 PASS。
- 回归：现有 2317 个测试全绿；testnet 启动深度自检通过；paper 环境全链路验证不受影响。

## 11. 验收标准

| 阶段 | 验收 |
|---|---|
| 1 | backfill 完成 BNBUSDT/BTCUSDT/ETHUSDT/SOLUSDT × 1h/1d（2024-08-01 起 ≈ 2 年）；manifest 哈希可复算；旧测试全绿 |
| 2 | 产出 ≥1 个 sealed PASS bundle（含完整逐级链与 historical_replay 证据）；对拍测试通过 |
| 3 | testnet 启动后 ≥1 因子 ACTIVE、DAG 接线、深度自检通过、0 新 incident；全测试绿 |
| 安全 | canary/live 拒绝 replay 证据（测试固化）；FactorPromotionGate strict 语义不变 |

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| Binance API 历史数据不足 2 年或限速 | 增量续传 + 失败清单；数据起点按实际可拉到的最早时间对齐（manifest 记录实际范围） |
| 向量化引入数值差异 | 对拍测试（1e-9 容差）固化；fold 划分逻辑不动 |
| 挖掘因子无真实经济假设，晋级后衰减 | 门禁本就有 OOS/CPCV/成本容量/paper 各级；DEGRADED 自动降级路径已存在 |
| 桥接把未验证因子带入真实下单 | testnet 本就是可写 demo 环境；canary/live 拒绝 replay 证据且要求运行时观察 |
| research extras 安装影响现有环境 | 新增依赖与现有依赖无冲突（numpy/pandas/pyarrow 独立）；用 `pip install -e ".[research]"` 单独安装 |

## 13. 涉及文件

- 改：beidou_core/feed.py、beidou_research/mining/runner.py、beidou_research/backtest/replay.py、apps/factor_miner/__main__.py、beidou_launcher/registry.py、beidou_core/engine.py（桥接调用点）、.gitignore、pyproject.toml（无改动，extras 已存在）
- 新：beidou_research/data/kline_store.py、beidou_research/data/dataset_manifest.py、beidou_core/evidence_bridge.py、beidou_core/expression_component.py
- 测试：tests/unit/test_kline_backfill.py、tests/unit/test_vectorized_wfo.py、tests/unit/test_replay_evidence.py、tests/unit/test_evidence_bridge.py、tests/unit/test_expression_component.py
