# 北斗 Beidou 工程可执行开发包 V1.0

- 目标仓库：`yhwhhwhy001/beidou`
- 审查基线：`main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341`
- 生成日期：2026-08-06
- 当前决策：`PIVOT`
- Paper / Shadow / Testnet：`HOLD`
- Mainnet：`PROHIBITED`

本包将《北斗 Beidou 全项目深度分析与受控收敛优化方案》转换为可直接交给 Claude Code、Cloud Code、Codex 或其他工程代理执行的任务合同。

## 使用顺序

1. 阅读 `00_EXECUTION_MASTER.md` 和 `01_AGENT_OPERATING_PROTOCOL.md`。
2. 在目标仓库执行 `bash delivery/scripts/preflight.sh`。
3. 执行 `python delivery/scripts/validate_package.py` 校验包结构。
4. 严格按 `delivery.yaml` 的拓扑顺序执行任务。
5. 第一批只执行 `BD-T00` 至 `BD-T03`，完成独立验收后才允许继续。
6. 每个任务独立 commit，并使用 `collect_evidence.py` 留存原始证据。
7. 任一 P0 失败，立即停止后续任务并将控制状态保持为 `LOCK` 或 `NO_NEW_RISK`。

## 重要边界

- 本包是开发执行合同，不是“生产已就绪”证明。
- 不允许使用 Mainnet 或真实资金执行任何测试。
- 不保证盈利；盈利能力必须由净成本后、统计可信、真实经过时间的证据证明。
- 任务完成状态只能是 `PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE`。

## 目录

```text
00_EXECUTION_MASTER.md
01_AGENT_OPERATING_PROTOCOL.md
02_PACKAGE_MANIFEST.md
delivery.yaml
docs/optimization/
delivery/task-packages/BD-T00..BD-T19/
delivery/scripts/
delivery/schemas/
delivery/templates/
config/
artifacts/evidence/
```
