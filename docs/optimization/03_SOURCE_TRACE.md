# 源码与事实链追踪

| 链路 | 主要位置 | 关键不变量 |
|---|---|---|
| Health/authority | `beidou_core/health.py`, `beidou_launcher/supervisor.py` | `/ready` 不得越过 fresh engine/supervisor facts |
| Intent/outbox | `beidou_safety/execution/intent.py`, `beidou_core/engine.py` | idempotency、UNKNOWN、dead-letter 可持久重放 |
| Exchange adapter | `beidou_exchange/binance_usdm/adapter.py` | venue write 只有一个 typed boundary |
| Order/fill | `beidou_core/store.py`, `beidou_safety/execution/order_state.py` | cumulative fill 只消费 delta；失败进入 UNKNOWN |
| Protection | `beidou_core/engine.py`, `beidou_observability/monitoring/checks/protection.py` | ACTIVE + venue id + owner/semantic exact match |
| Market/PIT | `beidou_core/feed.py`, `beidou_data/market.py` | 缺 metadata 不默认 closed；只用 close_time 已过的 bar |
| Live factor IC | `beidou_core/engine.py` | prediction(t) 只配 next closed bar forward return |
| Offline mining | `beidou_research/mining/runner.py`, `label_builder.py` | manifest、closed bar、PIT、Purged WFO/CPCV |
| Strategy kernel | `beidou_strategy/kernel_parity.py`, `alpha/typed_graph.py` | TypedGraph 是唯一可执行图 |
| Paper/Shadow | `beidou_strategy/paper_shadow.py` | queued/rejected/partial/actual cost 不得伪装成交 |

