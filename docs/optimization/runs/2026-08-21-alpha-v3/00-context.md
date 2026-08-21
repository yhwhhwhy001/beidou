# 北斗 Alpha Engine V3：上下文与证据基础

## 1. 范围与权限

- 运行 ID：`2026-08-21-alpha-v3`
- 执行包：`/Users/maguannan/Downloads/BEIDOU_ALPHA_V3_EXECUTION_PACKAGE.zip`
- 基线仓库：`/Users/maguannan/beidou`
- 实施 worktree：`/Users/maguannan/beidou-worktrees/alpha-v3-20260821`
- 分支：`codex/alpha-v3-20260821`
- 本轮授权：用户授权隔离 worktree 内全部源码、测试、注册表、CI、证据和
  文档修改，并授权完成受控 Paper/Shadow 重启验证。
- 明确边界：不触碰 Mainnet、真实资金、交易所写接口、下单/撤单/平仓、生产
  晋级或外部消息；旧 Testnet 进程不作为新代码证据。

## 2. 指令优先级

1. 系统/开发者安全、文件和工具约束。
2. 用户本轮执行包审查、补齐、100% V3 覆盖和重启验证请求。
3. 压缩包中的阶段依赖、Gate、禁止事项和验收要求。
4. 仓库 README、Makefile、pyproject 和历史 run 文档；历史 PASS 不继承。

附件 `reference/contract_skeletons.py` 仅作 reference，不直接复制为生产实现。

## 3. 固定基线与运行事实

| 项目 | 当前事实 |
|---|---|
| Package baseline SHA | `b156881710aeeec278bfbbe75973aa8dbc83b614` |
| Code validation HEAD | `8bffe2014205bb7dfae1a5274d88c43e365fcd6b`；代码提交已完成，运行时 JSONL 仍按约定不入库 |
| Archive manifest | 13 个文件 SHA-256/字节数校验 PASS |
| Python | `/Users/maguannan/ueds/.venv/bin/python`，3.14.6 |
| 原始运行 | PID 98924，`/Users/maguannan/beidou`，Testnet，127.0.0.1:9090；只读确认、未重启 |
| 最终合并后运行 | `fa52771f8936447aa58069e74f16699bfc4eb4b0` 上的独立 Paper，127.0.0.1:19090；启动后 DEGRADED/ready=false，安全停止，见 `14-production-validation.md` |

## 4. 本轮补齐

- `MarketState`、Benchmark、五类 AlphaForecast、MR consolidation、calibration
  registry、全入口 fusion、ExposureGovernor、ActivePortfolioOptimizer 和完整
  PnL attribution 已实现并接入只读 V3 shadow trace。
- V3 核心 22 个模块加入显式 100% line/branch CI gate；全局 coverage gate 已按执行包
  要求恢复为 `85%`，并有 `85.02%` 的最新全量证据。
- 官方 write-capability registry 已按当前源码重建，独立 oracle PASS。
- 环境缺失的运行目录 `.beidou/` 与 `evidence/bootstrap/` 已补齐；未伪造缺失的
  signed policy、OOS 数据、Paper 经济窗口或账户保护事实。

## 5. Gate 结论

- `G-A0`～`G-A6`：PASS，限定为离线合同、静态链路、fixture/property/integration
  和只读 shadow probe；不等价于经济晋级或生产就绪。
- `G-A7`：FAIL/NOT_VERIFIABLE。缺 sealed real same-data/same-cost OOS 与完整 Paper
  shadow 窗口，不能作收益或 promotion 结论。
- `GLOBAL-CI`：PASS。全量 `3532 passed`、coverage `85.020340...%`，超过 `fail_under=85`；
  静态/安全/包/registry 门禁均通过；V3 核心 3497 statements/1070 branches 为 100%/100%。
- Restart validation：PASS_WITH_FAIL_CLOSED_RUNTIME。最终合并提交上的 Paper 进程可启动并提供
  HTTP 200 健康端点，但状态为 DEGRADED；protection/reconciliation UNKNOWN 使 `/ready` 保持
  503、控制面保持 NO_NEW_RISK，停止后端口关闭。

## 6. 停止条件

- 需要触碰 PID 98924、其数据库或交易所写路径；
- 需要把 UNKNOWN 转成零值、默认成本、默认 venue 规则或伪造政策；
- 新代码形成第二套 MarketState/Fusion/Portfolio 主路径；
- 绕过既有 Risk/Approval/Execution/Protection；
- 把 fixture、只读 Paper 健康或 coverage PASS 扩大成收益/生产/晋级结论。
