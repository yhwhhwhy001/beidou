# BD-00 任务完成报告 — 冻结资金风险与停止假阳性

## 任务信息

- **任务 ID**: BD-00
- **优先级**: P0
- **阶段**: Phase 0
- **状态**: DEV_COMPLETE
- **基线**: 4ac54f4ffc20fc4f3da4f363a0cd6284ca7e8004
- **完成时间**: 2026-08-05

## 修改文件清单

### 新增文件
| 文件 | 用途 |
|------|------|
| `beidou_core/guard.py` | P0 EnvironmentGuard — 启动环境保护、Mainnet 阻断、审计事件 |
| `tests/unit/test_guard.py` | EnvironmentGuard 单元测试（20 tests） |
| `tests/integration/test_startup_guard.py` | 启动门禁集成测试（10 tests） |

### 修改文件
| 文件 | 变更 |
|------|------|
| `apps/autopilot/__main__.py` | CLI 默认模式 full→paper；集成 EnvironmentGuard 启动门禁；失败时 sys.exit(1) |
| `beidou_core/engine.py` | 移除自动 RESUME；启动后保持 NO_NEW_RISK |
| `tools/historical_certification.py` | 重命名为 `historical_simulation_diagnostic.py` |
| `tools/historical_simulation_diagnostic.py` | 移除 G6/G7 认证和 ladder 晋级；输出固定 NON_CERTIFYING |
| `README.md` | 移除"生产级""G7 ALL PASS""无人值守已认证" |
| `pyproject.toml` | 移除"生产级自主" |
| `beidou_certification/engine.py` | 移除"G7 L2-L5"注释 |

### 删除路径
| 旧路径 | 状态 |
|------|------|
| `tools/historical_certification.py` | 已重命名，不再存在 |
| 启动自动 RESUME | 已移除 |
| CLI `--mode full` 默认值 | 已改为 `paper` |
| 历史认证 G6/G7 生产晋级 | 已移除，替换为 NON_CERTIFYING |

## 验收标准映射

| AC ID | 标准 | 证据 | 状态 |
|-------|------|------|------|
| AC-00-01 | 无密钥 paper 模式，零交易写请求 | EnvironmentGuard paper 测试通过；engine paper_only 模式不调用 POST/DELETE | PASS |
| AC-00-02 | full 模式无证书时非零退出，NO_NEW_RISK | StartupGate FAIL → sys.exit(1)；控制面保持 NO_NEW_RISK | PASS |
| AC-00-03 | Mainnet URL 被拒绝 + P0 审计事件 | Mainnet URL 检测触发 P0 audit + StartupGate FAIL | PASS |
| AC-00-04 | 历史诊断脚本不产生 G5-G8 证书或晋级 | 移除全部 certification/ladder API 调用；输出 NON_CERTIFYING | PASS |
| AC-00-05 | 仓库文本扫描无禁止声明 | 生产代码扫描 = 0 forbidden claims | PASS |

## 阻断场景验证

1. **Production 模式阻断**: `EnvironmentMode.PRODUCTION` → 永久 FAIL
2. **Mainnet URL 阻断**: `fapi.binance.com` / `api.binance.com` → FAIL + P0 审计
3. **无凭据 Full 模式**: 空 API key/secret → FAIL
4. **无 G5 证书 Full 模式**: 缺少 evidence/certificates/G5.json → FAIL
5. **启动 NO_NEW_RISK**: engine 启动后不再自动 RESUME

## 测试结果

```
tests/unit/test_guard.py ..................... 20 passed
tests/integration/test_startup_guard.py ..... 10 passed
Full suite: 476 passed, 0 failed
```

## 强制执行命令

```
python -m ruff check .          # PASS
python -m pytest -q             # 476 passed
```

## 风险与遗留

- **NOT_VERIFIABLE**: 需要真实 Testnet API 凭据和 G5 证书才能验证 full mode 完整路径
- **G5 证书**: BD-13 完成后才能生成有效的 G5 证书用于 full mode 启动
- **遗留风险**: 历史诊断脚本中 Phase 0-5 的数据分析功能保留用于研究参考，但输出已标记 NON_CERTIFYING

## 如何证明异常和恢复语义

- `EnvironmentGuard` 在任何检查失败时返回 `StartupGateStatus.FAIL`
- 失败时审计事件写入 `evidence/BD-00/startup_audit.json`（即使 Gate FAIL 也落盘）
- CLI 入口在 Gate FAIL 时 `sys.exit(1)`，确保进程级阻断
- `ControlPlane` 初始化为 `NO_NEW_RISK`，只能显式 RESUME
- `AutonomousEngine.run()` 不再自动调用 RESUME
