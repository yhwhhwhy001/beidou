# T0-T5 测试策略

## T0 静态与供应链
compile、ruff、format、mypy strict、secret、hardcoded、architecture、dependency audit、SBOM、migration lint。

## T1 单元与不变量
property-based、state-machine、deterministic hash、risk invariants、ledger balance、Filter/Exit 类型约束、数值稳定性。

## T2 数据库与集成
PostgreSQL transaction/outbox/replay/migration/API/control/restart/double-worker/fencing。

## T3 Binance 公共 API
server time、exchangeInfo、filters、ticker/depth/klines、closed-bar、rate-limit headers；只读。

## T4 Binance Testnet
create/cancel/query、stable clientOrderId、partial fill、cancel/fill race、user stream、native protection、restart/reconciliation、credential/time/rate failures。

## T5 真实经过时间
7 天 Shadow、多日 Testnet、30 天无人值守。经过时间证据不得压缩或模拟。
