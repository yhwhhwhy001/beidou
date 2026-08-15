# 北斗全系统可执行优化方案：当前上下文

## 1. 范围与权限

- 仓库：/Users/maguannan/beidou
- 目标：先生成并复验固定到当前源码的可执行优化方案；2026-08-15 22:05+08:00 起，用户进一步授权依方案实施、验证、提交并推送 main。
- 当前授权：本地源码/测试/文档修改，隔离 worktree，本地验证，提交并推送 main；每个任务仍需记录 Task ID、baseline、文件范围与证据。
- 当前未授权：停止或重启现有进程、下单、撤单、平仓、修改仓位/杠杆/账户权限/密钥、连接后写入交易所、迁移或修改运行数据库、部署、Mainnet 或真实资金。
- 敏感信息处理：未读取 .env、API Key、Secret、DSN 密码、账户余额或仓位明细。

## 2. 固定基线

| 项目 | 当前事实 |
|---|---|
| 分支 | main |
| HEAD | e2819c4dfd7786d68674d85c4aad286eef8872c1 |
| 最近提交时间 | 2026-08-15T20:02:14+08:00 |
| 工作树 | 本轮开始时无未提交条目 |
| 与 origin/main 关系 | 2026-08-15 22:05+08:00 已 fetch；origin/main=7bbd7d83ef3b0bd8032d487d45acc771b6393bf6，本地 main ahead 21 |
| 本地增量集中区 | 21 个提交均只修改 beidou_core/engine.py，共 385 行新增、49 行删除 |
| Python 运行环境 | 本地检查使用 Python 3.14.6；CI 只声明 Python 3.12，存在环境一致性缺口 |

所有结论只适用于以上 HEAD。历史审计、README、旧证书和旧运行报告只作为线索，不继承 PASS。

## 3. 当前运行边界

只读进程元数据显示：

- .beidou/beidou.pid 对应一个仍在运行的 Python 进程；
- 入口为 .venv/bin/beidou start；
- mode=testnet；
- 显式传入 200 个 symbol；
- 本轮没有读取其账户、订单、仓位、保护、数据库或控制状态。

因此，当前运行事实统一标记为 UNKNOWN。不得因进程存活、健康端口、旧监督器文件或 Testnet 名称推断安全、归属、保护覆盖或交易就绪。任何实现阶段开始前，必须先由具名人类 Owner 选择：

1. 保持运行 checkout 不变，在独立工作树中开发；或
2. 先完成只读账户/订单/仓位/保护/数据库取证，再单独授权受控停机。

用户已授权实施；为避免触碰现有运行进程，本轮选择第 1 项：保持运行 checkout 不变，在独立 codex worktree 中开发。受控停机、运行验证和交易所动作仍需新的明确授权。

## 4. 适用约束及优先级

1. 用户本轮提示词与十条跨模块不变量。
2. 资金与账户安全 > 事实链正确性 > 数据和订单一致性 > 风险调整后净收益 > 可靠性 > 自动化 > 性能 > 功能数量。
3. 00_EXECUTION_MASTER.md 与 01_AGENT_OPERATING_PROTOCOL.md 的 fail-closed、证据、任务隔离和停止条件。
4. 当前 delivery.yaml、现有 BD-T00 至 BD-T19 任务包；这些任务绑定旧基线，需映射和重开，不得直接宣称完成。
5. README 和历史报告仅作意图说明，不是当前实现或运行证据。

仓库内未发现适用的 AGENTS.md。docs/optimization/_shared/templates 目录不存在，因此本运行复用现有 runs 文档格式，并显式记录这一模板缺口。

## 5. 2026-08-15 新鲜本地证据

| Context Evidence ID | 检查 | 结果 | 限制 |
|---|---|---|---|
| CTX-E-001 | git status、HEAD、log | 固定到 e2819c4；工作树起始干净；本地 main ahead 21 | 未 fetch，不证明远端 |
| CTX-E-002 | AST 包依赖与规模提取 | 20 个 beidou_* 包；beidou_core 约 14,840 LOC，beidou_research 约 14,902 LOC；核心编排高度集中 | 静态依赖，不证明运行可达 |
| CTX-E-003 | 启动入口代码 | pyproject 指向 beidou_launcher；start_beidou.sh、apps.autopilot、apps.safety_executor、apps.strategy_engine 仍可执行 | 未启动新进程 |
| CTX-E-004 | start_beidou.sh 静态审查 | 默认 Testnet/固定 symbols；可自动启动基础设施、生成签名策略，并生成 status=PASS、details=DEV_BYPASS 的 CERT-G5 证书 | E1 代码事实 |
| CTX-E-005 | Supervisor/Engine 静态审查 | Testnet 存在更长 LOCK 窗口、自动重新授权 RESUME、提款权限豁免、1% 对账容差、300 秒新鲜度、事件漂移改写、保护默认值等特殊语义 | E1 代码事实；运行影响尚未在线验证 |
| CTX-E-006 | Ruff | FAIL，27 个问题；含 engine.py 的未定义 order_id | 未自动修复 |
| CTX-E-007 | Ruff format | FAIL | 未自动格式化 |
| CTX-E-008 | forbidden-pattern scan | FAIL，10 项 | 扫描器只遍历 Python，未覆盖 shell/YAML/JSON/plist |
| CTX-E-009 | hardcoded scan | FAIL，6 个吞异常项 | 扫描范围未覆盖 start_beidou.sh |
| CTX-E-010 | test-quality scan | FAIL，4 个无断言测试 | 仅静态测试质量规则 |
| CTX-E-011 | mypy | PASS | beidou_core.engine 等关键模块仍在 ignore_errors 范围内，PASS 不能证明关键链类型安全 |
| CTX-E-012 | delivery package validator | PASS | 只能证明包结构，未发现本轮语义 P0 |
| CTX-E-013 | pytest collect-only | 2,486 tests collected | 未运行全量测试、覆盖率或外部集成 |
| CTX-E-014 | 关键 Testnet 语义测试 | 53 passed | 测试明确把 Testnet 自动 RESUME、陈旧用户流可 ready、300 秒对账窗和读时刷新保护事实作为期望行为，因此绿灯不等于满足 INV-006 |
| CTX-E-015 | 本地进程元数据 | 一个 Testnet 进程，200 symbols | 未读取账户/订单/仓位/保护，运行状态 UNKNOWN |
| CTX-E-016 | 当前证书验证器离线反例 | 当前 verify_g5_certificate 接受 status=PASS、全部 scenario=PASS、details=DEV_BYPASS 的 16 场景证书，返回 passed=true、failures=[] | 仅证明当前验证逻辑可接受语义假阳性；未触碰运行进程或交易所 |

## 6. 当前停止条件

以下任一条件出现，计划执行必须停止并保持 NO_NEW_RISK/EXIT_ONLY/LOCK 语义，不得进入下一模块：

- 需要触碰当前 Testnet 进程、交易所写接口或运行数据库，但没有独立授权；
- 无法证明现有仓位、订单、保护和本地事实的归属；
- CERT-G5、CERT-G7、健康或盈利证据由默认值、旧文件、模拟数据或同一来源自证；
- Testnet 通过降低生产安全语义获得可运行性；
- 无法保留当前 21 个本地提交或出现不明并发改动；
- P0 测试失败、证据缺失或回滚不可验证；
- Mainnet URL、真实资金或提款/权限变更进入范围。

## 7. 上下文 Gate

- Interaction Mode：Yellow（可继续编制方案）；任何运行/资金/Git 变更为 Red。
- 任务等级：L。
- Onboarding 结论：PASS_WITH_CONDITIONS。
- 条件：当前运行事实、远端状态、PostgreSQL authority、Testnet 账户归属和真实保护覆盖均为 UNKNOWN；只允许继续文档与离线方案工作。

Gate 名称在后续文档中强制分域：DA-G0 至 DA-G7 表示深度分析 Gate，ENG-G0 至 ENG-G8 表示工程交付 Gate，CERT-G5/CERT-G7/CERT-G8 表示 Testnet、无人值守和 Mainnet 证书 Gate。三者不得互相替代或继承 PASS。
