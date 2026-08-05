# 架构决策记录 (ADR) 索引

| ADR | 标题 | 状态 | 日期 |
|-----|------|------|------|
| ADR-001 | 多时钟决策架构与风险无旁路 | Accepted | 2026-08-05 |
| ADR-002 | 交易所抽象与 VenueInstrument 身份 | Accepted | 2026-08-05 |
| ADR-003 | 风险三段式与同步不变量 | Accepted | 2026-08-05 |
| ADR-004 | 双存储与事实源 | Accepted | 2026-08-05 |
| ADR-005 | 实盘 L0 到 L5 阶梯 | Accepted | 2026-08-05 |
| ADR-006 | Python 与 Rust 热路径边界 | Accepted | 2026-08-05 |
| ADR-007 | 故障指纹只推荐已批准 Runbook | Accepted | 2026-08-05 |
| ADR-008 | 应急平仓独立权威协议 | Accepted | 2026-08-05 |

## 规范

- 每个 ADR 包含：标题、状态、决策、后果、日期
- 状态：Proposed → Accepted → Deprecated → Superseded
- ADR 文件命名：`ADR-NNN_简短描述.md`
- 版本化管理，修改已 Accept 的 ADR 必须创建新的 ADR 并标记旧 ADR 为 Superseded

## 架构原则

1. **风险无旁路**：所有风险增加行为必须经过完整授权链
2. **事实源唯一**：每个事实只有一个权威存储源
3. **Fail-Closed**：关键输入 UNKNOWN 时，风险路径必须关闭
4. **时钟隔离**：三层时钟通过版本化合同交换，禁止跨层直连
5. **证据驱动**：所有 Gate 和晋级需要独立可验证证据
