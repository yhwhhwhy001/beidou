你正在执行北斗仓库 `yhwhhwhy001/beidou` 的任务 `BD-T16 — 真实 Paper 撮合与成本模型`。

基线：`main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341`。当前项目状态必须保持 PIVOT；Mainnet/真实资金永久禁止。本任务前置依赖：BD-T05, BD-T09, BD-T10。

先读取：
1. `00_EXECUTION_MASTER.md`
2. `01_AGENT_OPERATING_PROTOCOL.md`
3. `delivery/task-packages/BD-T16/TASK.md`
4. `delivery/task-packages/BD-T16/ACCEPTANCE.yaml`
5. `delivery/task-packages/BD-T16/ROLLBACK.md`

执行要求：
- 先运行前置测试并保存真实失败；不得先修改测试。
- 严格限制修改范围；禁止无关重构。
- 实现 TASK.md 中所有编号行为，不得以“后续”“MVP”省略。
- UNKNOWN/ERROR 必须 fail-closed。
- 不得增加 mock/fake/stub 生产实现，不得弱化 CI、断言、覆盖率、扫描器或类型检查。
- 所有命令使用 `delivery/scripts/collect_evidence.py` 采集证据。
- 完成 targeted tests 后执行适用的全量回归。
- 演练回滚并保存证据。
- 一个任务一个 commit，不推送或合并到 main。

最终输出必须包含：
1. 设计决策和不变量；
2. 修改文件与 diff 摘要；
3. 数据迁移；
4. 所有测试命令、退出码和证据路径；
5. 验收标准逐项 PASS/FAIL/NOT_VERIFIABLE；
6. 遗留风险；
7. 回滚命令；
8. 当前 Gate 状态。

任一 P0 或关键证据缺失时，明确返回 FAIL/NOT_VERIFIABLE，不得声称完成。
