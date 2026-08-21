# Alpha V3 交易域就绪门

## 决策

`NO-GO_FOR_PAPER_PROMOTION_AND_LIVE_ACTIVATION`

代码、合同、测试、静态门禁和 V3 100% 覆盖已通过；交易域仍不能晋级，原因是执行包
要求的真实经济证据和运行治理事实没有闭合。保持 `NO_NEW_RISK` 是正确结果。

## 当前证据

- `G-A0`～`G-A6`：离线/只读合同 PASS。
- `G-A7`：FAIL/NOT_VERIFIABLE；没有 sealed same-data/same-cost OOS、walk-forward
  真实窗口和完整 Paper shadow。
- 隔离 Paper 重启：健康与算法探针 PASS，但 protection ownership、reconciliation 和
  signed policy 事实 UNKNOWN/缺失，因此 `/ready=503`。
- 无任何交易所写操作、下单、撤单、平仓、Mainnet 切换或生产部署。

## 不可用的替代品

- fixture、synthetic calibration、默认成本、默认 venue 规则不能替代真实经济证据；
- `SIGNED_POLICY_UNAVAILABLE` 不能用伪造签名策略补齐；
- unowned protection/order UNKNOWN 不能转为“无订单”或自动清理；
- coverage 100%、健康接口 200 或 Paper 进程存活不能证明盈利、稳健或生产就绪。

## 后续解除条件

需由独立授权流程提供并审查：真实 sealed OOS/Paper 窗口、同数据同成本 lineage、完整
attribution、signed policy、保护归属、reconciliation 和回滚证据；在此之前，V3 仅可作为
离线/只读 shadow 使用。
