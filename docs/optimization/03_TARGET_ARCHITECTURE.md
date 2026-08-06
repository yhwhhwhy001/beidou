# 唯一目标架构

## 实时事实链

```text
BinanceUsdmAdapter
  ├─ REST Transport
  └─ WebSocket Transport
       ↓
Raw Event Store → ClosedBar/DQ/PIT Features → Typed Strategy Kernel
       ↓                                      ↓
Reference Data                       Portfolio Decision
                                             ↓
Risk Snapshot → Signed Approval → Persisted Intent + Tx Outbox
                                             ↓
Unified Order Aggregate → Fill → Position → Native Protection
                                             ↓
Double-entry Ledger → Independent Reconciliation → Control/Recovery
```

## 依赖方向

```text
shared/domain ← data/strategy/risk/execution/ledger
ports ← adapters
apps/core orchestration → ports/services
infrastructure 不得反向进入领域模型
```

## 必须删除的平行事实源

- engine/feed 直接 HTTP；
- Adapter 构造性账户/订单结果；
- 两套 Outbox；
- 两套订单状态机；
- 内存保护仓位作为 Position truth；
- SQLite 作为生产事实库；
- 单条 debit=credit 账本；
- 同源对账。
