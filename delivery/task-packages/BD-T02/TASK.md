# BD-T02 — 统一配置提供器

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 0
- 前置依赖：BD-T00
- 目标：建立唯一、版本化、可校验的配置入口，禁止核心模块固定读取 env.testnet.yaml 或使用隐式默认交易参数。

## 2. 问题证据

- Autopilot 默认 BEIDOU_ENV=testnet，但核心 engine/feed 固定打开 config/env.testnet.yaml。
- 仓库模板与实际运行配置路径不一致。

## 3. 实施范围

- 涉及模块：beidou_shared/config, beidou_policy, beidou_core, apps
- 预计修改文件：beidou_shared/config/*, beidou_policy/registry.py, beidou_core/engine.py, beidou_core/feed.py, apps/autopilot/__main__.py, config/*.yaml, tests/config/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在配置提供器中存储明文密钥；不得自动回退到 Testnet 或任何可写环境。

## 4. 必须实现

1. 实现 ConfigProvider + TypedSettings，加载顺序为 CLI explicit path > environment variables > environment-specific file > safe non-trading defaults。
2. 未知环境、缺少配置或 schema 不匹配必须回退 SAFETY_ONLY，而不是 testnet。
3. 业务参数进入 versioned Policy Registry，配置快照包含 version、checksum、effective_at、source。
4. 去除 engine/feed 内部文件路径和 YAML 读取逻辑，通过依赖注入获得配置。
5. 配置加载时验证 URL、mode、数据库、凭据引用和风险参数范围；错误聚合并阻断启动。
6. 提供 config/env.paper.yaml.example、env.shadow.yaml.example、env.testnet.yaml.example，均不含密钥。

## 5. 接口与合同

- `ConfigProvider.load(environment, explicit_path)->SettingsSnapshot`
- `PolicyRegistry.activate(policy_version)->ActivationResult`

## 6. 数据迁移

现有配置映射到新 schema；提供 scripts/migrate_config_v2.py，仅转换非敏感字段。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/config -q
```
```bash
pytest -q -k 'config and (unknown or missing or checksum or safety_only)'
```
```bash
python scripts/migrate_config_v2.py --check config/env.template.yaml
```

## 9. 验收标准

AC-BD-T02-01. 核心模块不直接 open config/env.testnet.yaml。
AC-BD-T02-02. UNKNOWN/缺失环境启动为 SAFETY_ONLY 且写交易能力为 false。
AC-BD-T02-03. 同一配置生成稳定 checksum；篡改后 checksum 或签名校验失败。
AC-BD-T02-04. 配置中不存在明文 API key/secret/password。

验收状态仅允许：`PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE`。

## 10. 交付证据

- 修改文件清单与 Git diff --stat
- 执行命令、退出码、开始/结束时间、stdout/stderr 原文及 SHA-256
- 测试报告与覆盖率报告
- 数据库/API/运行证据（如适用）
- 本任务验收矩阵逐项结果
- 遗留问题、风险与回滚命令
- commit SHA、branch、config hash、policy version、agent ID

## 11. 完成后动作

- 更新 `docs/optimization/06_ACCEPTANCE_MATRIX.md`；
- 生成 `artifacts/evidence/BD-T02/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
