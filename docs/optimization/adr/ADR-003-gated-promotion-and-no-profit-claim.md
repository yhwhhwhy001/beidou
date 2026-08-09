# ADR-003：按证据 Gate 晋级，不以回测或代码承诺盈利

## 状态

Accepted; current decision is `HOLD`。

## 决策

晋级顺序固定为：`P0 事实链/资金安全 → Paper/Shadow parity → 真实 Testnet → G7 无人值守 → 独立发布审批`。G0–G7 任一关键证据缺失、UNKNOWN、P0/P1 未关闭或覆盖率门失败时停止晋级。Mainnet 和真实资金不由代码变更自动开放。

收益只能在点时因果、独立未来标签、净手续费/资金费/点差/冲击、容量、跨 regime OOS、PBO/DSR/CPCV 和经过时间的 Paper/Testnet 证据齐全后评估；任何报告不得把回测、IC、模拟成交或历史证书写成持续盈利证明。

## 原因

量化收益最容易被前视偏差、数据泄漏、选择偏差、成本遗漏和容量假设伪造。生产安全也不能由单一健康端点或定时器证明。证据 Gate 将“代码存在”“测试通过”“能够下单”“真实可盈利”分离。

## 后果

- 当前 Paper、Testnet、Mainnet 和 24×7 持续盈利均不得宣称 READY。
- G5/G7 必须从零开始积累真实窗口，旧证书和 fast-forward 计数不继承。
- 研究参数不能未经 Shadow/批准直接改变风险资金参数。

## 验收与回滚

以 `docs/optimization/06_ACCEPTANCE_MATRIX.md`、`08_ACCEPTANCE_MATRIX.md`、`13_FINAL_ACCEPTANCE_REPORT.md` 和 `15_EXECUTION_DELIVERY.md` 为追踪入口。当前本地全量回归为 1244 passed、1 skipped，但覆盖率 66.49%（终端总计约 67%）< 85%，当前 preflight 33 项中 5 个 P0 FAIL，另有 venue `canWithdraw=true` 的独立账户能力 P0，因此决策保持 `HOLD`。PostgreSQL Outbox 恢复还要求隔离当前进程代际，旧 owner 即使 token 相同且 lease 未过期也必须进入 `UNKNOWN`；user-stream listen-key/WS 生命周期已接入本地可写引擎，但真实 replay/gap-fill/重连证据仍是硬门禁；保护恢复现在要求 ACK-backed durable rows 与 venue inventory 语义一致并重建本地投影，避免重启重复下发；任何运行故障都回到 `NO_NEW_RISK/EXIT_ONLY`，恢复必须经过新鲜三方对账和具名授权。
