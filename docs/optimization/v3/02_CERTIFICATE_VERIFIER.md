# V3 证书语义门禁

## 原则

G5/G7 runner 只能采集和写出证据，不能自证通过。独立 verifier 从 JSON 语义重新计算结果；任一关键字段缺失、矛盾、模拟、未来时间或证据不完整都返回 `NOT_VERIFIABLE`。

## G5 必须同时满足

- `gate/status` 为 `G5/PASS`，且 `commit` 精确等于当前待认证提交。
- Testnet URL 可识别且不是 Mainnet；`mainnet_prohibited=true`；`is_simulated=false`。
- 有非空 evidence hash、真实 `started_at/ended_at`，结束时间不在未来。
- 场景集合与 `config/g5-testnet-plan.yaml` 完全一致（当前 16 项），每项都只能是 `PASS`；`WARN`、缺失和 `NOT_VERIFIABLE` 均阻断。
- `account_access.can_withdraw=false`，无 P0/blocker，测试名义金额不超过计划上限。

当前 runner 将尚未实现的协议场景明确写成 `NOT_VERIFIABLE`，因此不会把现有 7 项浅层探针误发成 G5 PASS。

## G7 必须同时满足

- `gate/status` 为 `G7/PASS`，精确绑定运行 commit 与 G5 evidence hash。
- `mainnet_prohibited=true`、`is_simulated=false`，免责声明不得含 simulated/fast-forward。
- 真实起止时间至少 30 天，结束时间不在未来；至少 200 个 SLI 样本和 30 份日报。
- SLI 通过率为 100%，P0、重置和活动事故均为 0。

`--fast-forward` 只保留为框架演示，输出强制为 `NOT_VERIFIABLE`，不能计入真实窗口。

## 当前决策上限

本门禁关闭历史证书继承路径，但不等于 G5/G7 已通过。当前交易链、持久化事实源、保护/对账、备份恢复和 Alpha 统计仍未完成；在这些 P0 关闭并重新取得独立证据前，Testnet 继续 HOLD，Mainnet 继续 PROHIBITED。
