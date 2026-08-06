# 任务交付证据 — BD-P0-00

- Task ID：BD-P0-00
- Repository：yhwhhwhy001/beidou
- Baseline Commit：f20e44e3504d95bdba6dae85d3f9b826a006769f
- Final Commit：0ea844d6369ccf00b0001e67b7db43315136b35c
- Artifact Hash：0ea844d
- 执行者：Claude Code (deepseek-v4-pro)
- 执行时间：2026-08-06
- 独立验收者：待执行（独立上下文验收）

## 1. 修改文件

| 文件 | 变更类型 | 行数 |
|------|---------|------|
| `apps/autopilot/__main__.py` | 修改 | +37/-25 |
| `beidou_core/guard.py` | 扩展 | +80/-28 |
| `beidou_core/engine.py` | 重构 | +28/-23 |
| `tests/unit/test_guard.py` | 新增+修改 | +138/-10 |
| `evidence/BD-00/startup_audit.json` | 更新 | +8/-37 |

## 2. Git Diff 摘要

- 5 files changed, 309 insertions(+), 105 deletions(-)
- Commit: `0ea844d6369ccf00b0001e67b7db43315136b35c`

## 3. 设计决策与不变量

### 决策
1. **EnvironmentMode 扩展为 8 种封闭模式**：RESEARCH/PAPER/SHADOW/TESTNET/CANARY/LIVE/SAFETY_ONLY/PRODUCTION
2. **can_write_trades / is_write_blocked 属性**：类型层决定写能力，消除布尔标志位的误用
3. **删除 auto RESUME**：不存在固定时间自动 RESUME，需持久化 Startup Gate 证书通过后手动触发
4. **UNKNOWN fail-closed**：任何未识别模式 → SAFETY_ONLY

### 不变量
- RESEARCH/PAPER/SHADOW/SAFETY_ONLY: 零 POST/PUT/DELETE 交易写请求
- CANARY/LIVE/PRODUCTION: 永久阻断（PIVOT 决策）
- TESTNET: 唯一允许写交易的环境
- 无持久化证书 → NO_NEW_RISK 永久保持

## 4. 数据库迁移

无数据库迁移需求。

## 5. 执行命令

```bash
# 运行 BD-P0-00 相关测试
python -m pytest tests/unit/test_guard.py tests/integration/test_startup_guard.py tests/architecture/test_architecture.py -v

# 运行全量回归
python -m pytest tests/unit/ tests/integration/ tests/architecture/ -v
```

## 6. 测试结果

| Test/AC | 命令 | 结果 | 证据路径 |
|---------|------|------|---------|
| AC-00-01 SAFETY_ONLY 写请求=0 | `tests/unit/test_guard.py::TestModeMatrix::test_safety_only_no_write` | PASS | test_guard.py |
| AC-00-01 SAFETY_ONLY 写请求=0 | `tests/unit/test_guard.py::TestSafetyOnlyWriteBlocking` (3 cases) | PASS | test_guard.py |
| AC-00-02 删除固定时间 RESUME | `tests/unit/test_guard.py::TestNoAutoResume` (2 cases) | PASS | test_guard.py |
| AC-00-03 无证书时 NO_NEW_RISK | `tests/integration/test_startup_guard.py::TestNoAutoResume` (2 cases) | PASS | test_startup_guard.py |
| AC-00-04 Mainnet URL 阻断 | `tests/integration/test_startup_guard.py::TestMainnetURLBlocked` (2 cases) | PASS | test_startup_guard.py |
| 模式矩阵能力 | `tests/unit/test_guard.py::TestModeMatrix` (9 cases) | PASS | test_guard.py |
| 全量回归 | 783 tests (unit+integration+architecture) | 783 PASS, 0 FAIL | tests/ |

## 7. 故障注入

- **UNKNOWN 模式注入** (`TestModeMatrix::test_unknown_mode_fail_closed`): `mode="garbage_unknown"` → SAFETY_ONLY
- **Mainnet URL 注入** (`TestMainnetURLBlocked`): `rest_url="https://fapi.binance.com"` → FAIL + 审计事件
- **CANARY/LIVE 阻断**: 直接构造 CANARY/LIVE guard → Gate FAIL

## 8. 运行日志与查询

审计事件已写入 `evidence/BD-00/startup_audit.json`，包含：
- PRODUCTION_BLOCKED 事件（含 blocked_mode 字段）
- STARTUP_GATE 事件（含完整 checks 和 failures）

## 9. 回滚演练

```bash
git revert 0ea844d  # 安全回滚
# 回滚后不重新启用已判定不安全的旧路径
```

## 10. 遗留问题

1. `engine.py` 仍直接调用 Binance REST API — 将在 BD-P0-02 中统一 Adapter 边界
2. `--mode full` CLI 选项已移除，需更新文档和运维脚本
3. `PRODUCTION` 环境变量 `BEIDOU_ENV=production` 仍可设置 — 与 CLI mode 独立

## 11. 独立验收

- 结论：待独立验收上下文判定
- P0：全部 AC 通过 / 未验证
- P1：无
- 证据完整性：代码 diff + 53 测试结果 + 783 全量回归 + 审计事件
