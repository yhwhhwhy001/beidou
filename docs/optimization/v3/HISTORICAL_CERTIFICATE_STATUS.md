# 历史证书状态

以下材料在 V3 执行基线下仅作为历史调查材料，不可用于 Testnet-ready、G7 或 Mainnet 候选证明：

- 绑定旧 commit 的 G5 certificate；
- `is_simulated=true` 或 fast-forward 生成的 G7 certificate；
- 未来日期报告、缺失原始命令输出或场景集合不完整的 manifest；
- 旧 Acceptance Matrix 中与当前运行事实冲突的 PASS。

V3 不删除或覆盖原文件。新的 verifier 必须拒绝 commit/config/policy/schema/dataset/account/environment/时间窗口任一漂移，以及模拟标记、场景缺失、提款权限和开放 P0。
