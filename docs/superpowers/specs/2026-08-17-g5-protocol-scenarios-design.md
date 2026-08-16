# G5 协议测试场景实现设计(2026-08-17)

## 背景与根因

`preflight.g5_certificate` 检查 FAIL,根因:`config/g5-testnet-plan.yaml` 定义的 16 个协议
场景(create_query_cancel、ack_loss 等)在仓库中**从未实现** —— `scripts/testnet/run_g5.py`
明确将所有 plan 场景标记为 NOT_VERIFIABLE("scenario requires a complete protocol test and
durable evidence"),生成的证书(status=FAIL, certification_mode=DEV_BYPASS)绑定旧 commit,
每次提交后失效。

`verify_g5_certificate` 要求:PASS 证书需 16 个场景全部 status=PASS、commit 绑定当前 HEAD、
summary warn=0/fail=0、can_withdraw=False、无 P0、max_notional ≤ plan 上限。

当前 `~/.beidou/.env` 的 `BEIDOU_DEV_FAST_START=1` 豁免已使 G5 检查不阻断启动(降 P2,
M22-F05 登记设计,"检查永不缺席、判定恒真实")。本设计实现真正的协议场景以获得 PASS 证书。

## 决策记录(与用户澄清确认)

1. **执行对象:混合模式** —— 纯交易所协议场景自建 `BinanceRESTClient`/Adapter 直连;
   引擎行为场景用引擎组件级集成(构造组件,不依赖运行中的引擎实例);
   重启类场景真实重启引擎/PG。
2. **破坏性场景:真实重启** —— process_restart 对引擎 SIGKILL 等 autopilot 拉起;
   database_restart 执行 `brew services restart postgresql@16`;执行认证时服务中断
   各约 1 分钟,属预期。
3. **证据持久化:每场景独立证据文件** —— `artifacts/evidence/testnet/g5/scenarios/<name>.json`,
   支持逐场景重跑与审计。
4. **实现方案:场景框架 + 每场景独立模块**(方案 B),run_g5.py 保留为薄入口。

## 1. 包结构与基类契约

```
beidou_certification/g5_scenarios/
├── __init__.py
├── base.py              # ScenarioResult / ScenarioBase / 证据与 notional 记账
├── protocol/            # 协议直连组(自建 BinanceRESTClient + Adapter)
│   ├── create_query_cancel.py
│   ├── stable_client_order_id.py
│   ├── duplicate_request.py
│   ├── clock_skew.py
│   ├── rate_limit.py
│   └── credential_failure.py
├── engine/              # 引擎集成组(构造引擎组件,不依赖运行实例)
│   ├── ack_loss.py
│   ├── timeout_unknown_recovery.py
│   ├── partial_fill.py
│   ├── cancel_fill_race.py
│   └── reconciliation_mismatch.py
├── restart/             # 重启恢复组(真实重启引擎/PG)
│   ├── process_restart.py
│   ├── database_restart.py
│   └── user_stream_reconnect.py
├── protection/          # 原生保护组
│   ├── native_protection.py
│   └── double_worker_fencing.py
└── runner.py            # 注册表、按序执行、汇总证书
```

### 基类契约(base.py)

- `ScenarioResult`:scenario_id、status(PASS/FAIL/NOT_VERIFIABLE/WARN)、evidence dict
  (确定性 JSON 可序列化)、duration、artifact_hash(sha256 规范化证据 JSON)。
- `ScenarioBase.run(ctx) -> ScenarioResult`:`ctx` 共享上下文 —— 交易所 client(协议组)、
  引擎构造工厂(引擎组)、notional 记账器、证据目录、代理环境。
- **notional 记账器**:全局累计下单金额,超 plan `max_test_notional_usdt`(20)立即
  fail-fast,防止认证耗尽账户资金。
- 证据文件内容:输入参数、步骤时间线、原始交易所响应(密钥脱敏)、最终判定与 hash。

## 2. 16 个场景逐个要点

### 协议直连组(6 个,自建 client;真实写操作限于小单)

| 场景 | 验证内容 | 写操作 |
|---|---|---|
| create_query_cancel | 下单→按 orderId 查询→撤单→再查询确认 CANCELED | 1 单(最小量,可撤) |
| stable_client_order_id | 同 clientOrderId 重发被交易所幂等拒绝/返回同一单,不产生重复单 | 1 单 |
| duplicate_request | 引擎侧幂等键语义:同 idempotency_key 的 intent 只提交一次(引擎组件级) | 无 |
| clock_skew | 引擎 `_resync_clock_offset` 校正后签名请求成功(时钟偏差检测路径) | 1 单 |
| rate_limit | 限频响应(429/-1003)触发退避、不丢失订单状态;熔断正确分类(venue 级 vs 业务拒绝) | 无(或 1 单) |
| credential_failure | 无效凭据 → 认证错误明确分类(非网络错误)、fail-closed 不伪造成交 | 无 |

### 引擎集成组(5 个,构造引擎组件/独立子进程引擎实例连同一 PG,testnet 真实 API)

| 场景 | 验证内容 |
|---|---|
| ack_loss | 下单 REST 响应丢弃(注入)→ 订单进 UNKNOWN 锚点 → 对账恢复 → 无重复下单 |
| timeout_unknown_recovery | 请求超时 → UNKNOWN 状态机 → 恢复查询闭合风险;durable UNKNOWN 行阻断 readiness 直到对账 |
| partial_fill | PARTIALLY_FILLED 事实单调守卫(不被 NEW 回写)+ 部分成交记账。testnet 流动性差:下大单(≥盘口深度)真实部分成交;30s 内全成/未成则记 NOT_VERIFIABLE 不伪造 |
| cancel_fill_race | 撤单响应与成交事件竞态:终态单调(成交不被 CANCELED 覆盖) |
| reconciliation_mismatch | 人为写错 opening 基线 → 对账 MISMATCHED → 控制面关闭;OPERATOR_REBASELINE 后恢复 MATCHED。注意:基线在共享 PG,场景执行期间运行中的引擎会短暂 MISMATCHED(预期,场景结束立即恢复基线) |

### 重启恢复组(3 个,真实重启;执行时服务中断各约 1 分钟)

| 场景 | 验证内容 |
|---|---|
| process_restart | SIGKILL 引擎 → autopilot 自动拉起 → durable facts(订单/保护/投影)恢复一致、对账 MATCHED、trading_ready 恢复 |
| database_restart | `brew services restart postgresql@16` → 引擎经重试自愈(不崩溃)、状态后端重连成功 |
| user_stream_reconnect | 强制断开 ws(listen key 过期)→ 引擎重连 + 事件流投影与交易所一致 |

### 原生保护组(2 个)

| 场景 | 验证内容 |
|---|---|
| native_protection | 真实挂 SL/TP Algo 单 → 成交/手动平仓后自动取消孤儿 TP → 保护行终态持久化 |
| double_worker_fencing | 第二实例启动被 InstanceLock fencing 拒绝,不产生双写 |

## 3. runner 与证书生成流程

```
run_g5.py(薄入口,兼容现有参数)
  └─ g5_scenarios/runner.py
     1. 加载 plan(config/g5-testnet-plan.yaml)→ expected_scenarios + max_notional
     2. preflight:API KEY/SECRET、testnet URL(拒 mainnet)、引擎进程存在性(重启组需要)
     3. 按注册表顺序执行场景(--scenario <name> 仅执行指定场景并复用已有证据)
     4. 每场景:run(ctx) → ScenarioResult → 证据落盘 + 打印进度
     5. 汇总:证书结构沿用 verify_g5_certificate 兼容格式
        - scenarios: {name: {status, evidence_path, artifact_hash}}
        - evidence_hash = sha256(全部场景 artifact_hash 排序拼接)
        - status: 任一 FAIL → FAIL;任一 NOT_VERIFIABLE → NOT_VERIFIABLE;否则 PASS
     6. 写 artifacts/evidence/testnet/g5-certificate.json + g5-evidence.json
     7. verify_g5_certificate 自检(commit/场景集/notional),与现有一致
```

- 兼容性:现有 `--plan --symbol --confirm-testnet` 保留;新增 `--scenario <name>`、
  `--skip-restart`(跳过重启组,供日常回归)、`--list`。
- S1-S7 legacy 观察场景保留为证书 `observations` 字段(不影响 PASS 判定)。
- 证书绑定运行时的 HEAD commit;提交代码后证书失效需重跑 —— 现有设计,保持不动。

## 4. 错误处理与测试策略

### 错误处理

- 每场景 catch 一切异常 → ScenarioResult(FAIL/NOT_VERIFIABLE + error_type + 截断 error),
  框架本身绝不崩溃;单场景失败不阻断后续场景(重启组除外:失败时 runner 停在该组,
  剩余场景标记未执行)。
- notional 记账器超限 → fail-fast 立即中止全部剩余场景(资金保护优先)。
- 场景内写操作顺序:只读查询基线 → 记录 → 写操作 → finally 恢复(撤单/平仓);
  任何写操作前打印操作意图与金额。
- 证据文件写入失败 → 场景判 FAIL(没有 durable evidence 的结果不算结果)。

### 测试策略(TDD)

- 纯逻辑单测 `tests/unit/test_g5_scenarios/`:UNKNOWN 状态机转换、幂等键生成、fencing
  判定、对账 diff 计算、证书汇总逻辑(注入假场景结果)、notional 记账器。
- 场景骨架契约测试:每场景提供 dry-run 形态(协议组构造请求不发送;引擎组注入 fake
  adapter),验证场景骨架与证据 schema。
- 交易所真实行为属认证运行时,不在单测覆盖范围;场景中可判定逻辑必须提取为纯函数。
- 新代码零 mypy/ruff 豁免。

## 范围外

- G7 认证(72h 运行窗口证据)不在本设计范围;G5 PASS 是 G7 的前置。
- 不改变 preflight 严格语义与 DEV_FAST_START 豁免机制。
- 不修改 verify_g5_certificate 的检查项(除非实现中发现检查项与场景证据格式冲突,
  冲突时优先适配证据格式)。
