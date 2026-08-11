# Testnet 安全语义分叉审查报告 (BD-CV01)

生成时间: 2026-08-12
基线: aeaa44700956eccba6e8fc5be7646ca42ce395d5

## 已修复的语义分叉

### 1. engine.py — 订单 TIF 语义

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:4128-4137` | IOC/FOK 在 testnet 被静默改为 GTC | 删除 testnet TIF 覆写。所有环境使用相同 TIF 语义 |

### 2. engine.py — Algo 订单库存

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:1786-1802` | API 失败时 testnet 返回空列表，其他环境返回 None | 统一返回 None，让调用方处理失败 |

### 3. engine.py — 用户流就绪

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:2382-2403` | Testnet 跳过 projector 检查（SEQUENCE_UNAVAILABLE bypass） | 统一 readiness 检查 |
| `engine.py:5631-5637` | 未知事件在 testnet 被忽略，其他环境 fail-closed | 统一 fail-closed |

### 4. engine.py — 订单清理与恢复

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:5968-6005` | Testnet 无主订单清理逻辑不同（仅 testnet 执行） | 统一为所有环境执行清理 |
| `engine.py:6084-6089` | Nearline 清理在 testnet skip | 统一 skip 逻辑 |
| `engine.py:6198-6203` | Protection retry 在 testnet skip | 统一 skip 逻辑 |
| `engine.py:7747-7791` | Startup 阶段无主订单处理 testnet 特殊路径 | 统一处理逻辑 |

### 5. engine.py — 风险上下文

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:6840-6845` | Liquidation price 缺失时 testnet 用估算值 | 删除估算，如实返回 UNKNOWN |
| `engine.py:6873` | Rolling sharpe testnet 返回 0.0 vs None | 统一返回 None |
| `engine.py:6885-6889` | min_liquidation_distance_pct testnet=1.0% vs 5.0% | 统一使用策略参数 |
| `engine.py:6897-6901` | can_withdraw testnet 强制覆写为 False | 使用交易所实际返回值 |

### 6. engine.py — 提款权限

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:7437-7438` | R9 提款检查 testnet 豁免 | 统一检查 |
| `engine.py:7482-7483` | Venue 验证 testnet 豁免 | 统一验证 |

### 7. engine.py — Protection 与 Nearline

| 位置 | 问题 | 修复 |
|------|------|------|
| `engine.py:7956-7964` | Protection placement testnet 绕过所有权检查 | 统一阻断 |
| `engine.py:8135` | Nearline interval testnet=30s vs 300s | 统一 300s |

### 8. supervisor.py — 控制面

| 位置 | 问题 | 修复 |
|------|------|------|
| `supervisor.py:81` | Exchange algo snapshot 初始化 testnet=True, 其他=False | 统一初始化为 False |
| `supervisor.py:106-108` | Degrade/lock 时间 testnet=30/999 vs 6/12 | 统一 6/12 |
| `supervisor.py:244-255` | NO_NEW_RISK 在 testnet 不执行(被屏蔽) | 统一执行 |
| `supervisor.py:469` | Engine probe 仅 testnet 执行 | 统一执行 |
| `supervisor.py:505-506` | 算法探测 mask (testnet-bypass) | 使用真实探测结果 |
| `supervisor.py:644-647` | 监控探测 mask (testnet-bypass) | 使用真实探测结果 |
| `supervisor.py:838-842` | Testnet 永不死锁（DEGRADED vs LOCKED） | 统一 LOCKED |
| `supervisor.py:993-1026` | S17: 防抖不降级、自动恢复 RESUME、计数不fail | 统一降级/恢复逻辑 |
| `supervisor.py:1139` | DEV_FAST_START/testnet 跳过深度验证 | 仅 DEV_FAST_START 跳过 |

### 9. runtime.py

| 位置 | 问题 | 修复 |
|------|------|------|
| `runtime.py:279-285` | 行情数据 severity testnet=P2 vs P0 | 统一 P0 |
| `runtime.py:391-413` | 对账 max_age=300s vs 60s, severity P1 vs P0 | 统一 60s, P0 |

### 10. monitoring/

| 位置 | 问题 | 修复 |
|------|------|------|
| `monitoring/__init__.py:323-374` | 对账检查 max_age=300s, P0→P1, FAIL→WARN | 统一 60s, P0, FAIL |
| `monitoring/checks/execution.py:39-44` | 订单追踪 FAIL→WARN | 统一 FAIL |
| `monitoring/checks/protection.py:115-117` | 保护覆盖 P0→P1, FAIL→WARN | 统一 P0, FAIL |
| `monitoring/checks/account.py:82-85` | 提款权限 testnet 豁免 | 统一检查 |

## 允许保留的环境差异 (infrastructure only)

以下差异属于基础设施层面，允许保留：
- WS/REST endpoint URL (testnet vs production)
- Credential ID
- Capital ceiling
- Symbol allowlist

## 历史 evidence 标记

所有旧 testnet-bypass evidence 标记为 LEGACY_NOT_VERIFIABLE。
