# 北斗全系统优化方案：独立对抗式审查

## 1. 审查边界

- 审查对象：00-context.md、04-task-plan.md、用户提示词与 main@e2819c4 的已记录证据。
- 审查方式：独立审查 agent 未参与方案修订、未写文件、未执行源码或外部系统动作；先审首版冻结草案，再对修订项做两轮只读复验。
- 独立性限制：审查者与作者共享对话上下文和可变工作区，因此属于程序性独立复验，不是盲审、第三方合规审计或加密冻结审计。
- 本文件不证明源码缺陷已修复，也不授权进程、交易所、数据库、部署或 Git 动作。

## 2. 首轮结论

首版草案被判定为 PIVOT，DA-G6=FAIL。审查发现 10 个 P0 和 4 个 P1，核心问题包括依赖环、旧图/旧 PASS 污染、Gate 与 Evidence 命名冲突、UNKNOWN 下终端写旁路、Testnet 账户与语义分叉、直接 RESUME、证据验证器自证、M00 过宽、覆盖率游戏化和盈利指标后验选择。

## 3. Kill Register 最终状态

| Kill | 最终状态 | 修订后的计划处置 | 系统事实 |
|---|---|---|---|
| KILL-001 研究 DAG 环 | CLOSED_BY_PLAN_REVISION | W4 改为 M03→TASK-M06-K00→M08→M04→M05；任务级 DAG 已验证无环 | 尚未实施 |
| KILL-002 旧图/旧 PASS 污染 | CLOSED_BY_PLAN_REVISION | 新建绑定当前 SHA 的 versioned DAG；旧 delivery/BD 状态只作意图来源且不可导入 | 新图尚未创建 |
| KILL-003 Gate 命名冲突 | CLOSED_BY_PLAN_REVISION | 分离 DA-G、ENG-G、CERT-G 命名空间 | 不继承任何旧 PASS |
| KILL-004 Evidence ID 冲突 | CLOSED_BY_PLAN_REVISION | Context 使用 CTX-E-*，计划使用 E-*，run 内唯一 | 当前文档结构已验证 |
| KILL-005 UNKNOWN 下 cancel/reduce/delete 仍可写 | OPEN_IMPLEMENTATION | 所有终端写分类并绑定 capability/owned object；归属或数量 UNKNOWN 时全部禁写 | 当前代码未关闭 |
| KILL-006 直接 adapter/Engine/script 旁路 | OPEN_IMPLEMENTATION | Write Capability Registry 覆盖 CLI/module/shell/plist/script/adapter/Engine/Algo/recovery | Registry 与阻断器未实现 |
| KILL-007 Testnet 特殊语义/账户隔离 | OPEN_DECISION | 要求专用独占账户、机器可读 semantic manifest 和所有 OPEN diff 强制 HOLD | 账户、Owner、初态均 UNKNOWN/TBD |
| KILL-008 域内直接 RESUME | OPEN_IMPLEMENTATION | 所有 RESUME 先阻断；只允许 recovery request→fresh 三方对账→一次性具名授权 | 当前 Engine/Supervisor 路径未改 |
| KILL-009 CONTRACT_READY 与 PASS 混淆 | CLOSED_BY_PLAN_REVISION | CONTRACT_READY 只允许离线依赖开发，不解锁运行、写能力或证书 | 无行为 PASS |
| KILL-010 producer/verifier 信任根 | OPEN_DECISION | 独立 artifact digest、Quality+Approval 双批准、签名 rotation manifest、单调版本与 anti-rollback | 信任根、Owner 和首个 digest 未确定 |
| KILL-011 M00 过宽/内部顺序环 | CLOSED_BY_PLAN_REVISION | 拆为 C→E→V01/V02→T01/T02→V03；V03 才形成总体 Gate | 逐任务授权仍待确认 |
| KILL-012 NOT_VERIFIABLE 与下游推进冲突 | CLOSED_BY_PLAN_REVISION | 行为验收与 CONTRACT_READY 分离 | 不得用合同状态冒充运行验收 |
| KILL-013 coverage 可游戏化 | OPEN_IMPLEMENTATION | 冻结 denominator/omit/pragma/阈值，增加 changed-code 与 mutation 门和配置 diff | QA 基线与 CI 证据尚无 |
| KILL-014 经济门后验调参 | OPEN_DECISION | 看结果前预注册资本/DD/样本/试验预算/成本/容量/DSR/PBO，并比较简单替代方案 | Learning Owner 与数值阈值未具名 |

## 4. 第二轮新增项与关闭

| 新发现 | 严重度 | 最终状态 | 关闭依据 |
|---|---:|---|---|
| NEW-P0-001 V/T 顺序环 | P0 | CLOSED_BY_PLAN_REVISION | 顺序统一为 C→E→V01/V02→T01/T02→V03，且只有 V03 可形成 M00 Gate |
| NEW-P1-001 C 在 E 穷举前声称全覆盖 | P1 | CLOSED_BY_PLAN_REVISION | C 仅可 CONDITIONAL_PASS(scope=known shared choke-points)，E 后才可入口覆盖 PASS |
| NEW-P1-002 M08 kernel contract 无任务节点 | P1 | CLOSED_BY_PLAN_REVISION | 新增 TASK-M06-K00，绑定依赖、双 Owner、digest、Exit Gate 和失败动作 |
| NEW-P1-003 verifier trust root 不完整 | P1 | CLOSED_BY_PLAN_REVISION | 固定 approved digest、双 Owner、rotation manifest、最低版本和防回退规则 |

## 5. 最终 Gate

- Revised-plan DA-G6：PASS。
- 计划作为可执行路线图：GO。
- 源码实施：HOLD，下一候选仅 TASK-M00-C00，仍需单任务授权。
- 当前 Testnet/运行激活：HOLD；现有进程存在不等于加载修复或 READY。
- Mainnet/真实资金：PROHIBITED。
- 盈利：UNKNOWN；M-007 未预注册，不得晋级或声明持续盈利。
- 最终复验新增 P0：none。

审查 PASS 只覆盖“计划能否按 fail-closed 顺序执行”，不关闭表中 OPEN_IMPLEMENTATION/OPEN_DECISION，也不替代任何 ENG 或 CERT Gate。
