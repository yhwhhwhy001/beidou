# 工程代理运行协议

## 1. 每次任务开始

1. 读取本任务 `TASK.md`、`ACCEPTANCE.yaml`、`ROLLBACK.md` 和 `AGENT_PROMPT.md`。
2. 确认所有依赖任务均有 PASS 证书；否则停止。
3. 确认工作树干净，记录 HEAD、branch、配置 hash。
4. 将控制状态设置为本任务允许的最低风险状态；默认 `NO_NEW_RISK`。
5. 先运行任务前置测试，保存基线失败，不得直接修改测试。

## 2. 修改规则

- 严格限制在允许文件范围；额外文件必须在任务报告中说明原因。
- 先实现领域合同和不变量，再接入运行链。
- 所有异常必须类型化；UNKNOWN/ERROR 禁止增加风险。
- 数据库变更使用 forward-only migration；禁止手工改生产 schema。
- 不得把测试 fixture、evidence 或本地数据库当作生产事实源。
- 不得在一个任务同时重构无关模块。

## 3. 测试顺序

```text
compile → targeted unit → property/state-machine → integration → architecture → regression → coverage/security → rollback rehearsal
```

## 4. 证据

所有命令使用 `delivery/scripts/collect_evidence.py` 执行。不得只粘贴摘要；必须保存 stdout/stderr、退出码和 hash。

## 5. 提交

提交格式：

```text
<task-id>: <imperative summary>

Evidence: artifacts/evidence/<task-id>/manifest.json
Gate: <gate result>
```

## 6. 禁止伪完成

以下均不算完成：新增类但未进入主链；测试仅验证对象存在；用 Mock 代替数据库/Testnet；返回 EMPTY 代替查询失败；提交说明声称测试通过但无原始结果；覆盖率配置存在但 CI 未强制。
