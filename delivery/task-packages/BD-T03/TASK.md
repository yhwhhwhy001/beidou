# BD-T03 — 单一 Exchange Gateway

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T01, BD-T02
- 目标：让 BinanceUsdmAdapter 成为唯一交易所边界，REST/WebSocket Client 仅为其内部传输实现，删除业务层原始端点和存根返回。

## 2. 问题证据

- engine.py 直接依赖 BinanceRESTClient。
- feed.py 自行使用 urllib/HMAC。
- BinanceUsdmAdapter 的账户、订单、撤单和查询行为仍为构造性返回。

## 3. 实施范围

- 涉及模块：beidou_exchange, beidou_core, beidou_data, beidou_safety
- 预计修改文件：beidou_exchange/core/protocol.py, beidou_exchange/binance_usdm/adapter.py, beidou_exchange/binance_usdm/rest_client.py, beidou_exchange/binance_usdm/ws_client.py, beidou_core/engine.py, beidou_core/feed.py, tests/contracts/exchange/*, tests/integration/exchange/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在 Adapter 外新增网络调用；不得把失败转 EMPTY；不得在本任务开放 Mainnet。

## 4. 必须实现

1. 定义 MarketDataExchangePort、AccountExchangePort、TradingExchangePort、UserStreamPort 的异步命名方法。
2. BinanceUsdmAdapter 内部组合 REST/WebSocket client；业务模块只依赖 Protocol。
3. 删除 Adapter 中固定 can_trade=True、EMPTY、NEW、CANCELED 等构造性结果。
4. 所有结果返回类型化 Result，包含 status、category、retryable、source、observed_at、correlation_id。
5. 实现 server time 校准、exchangeInfo 缓存与变更检测、请求权重、订单限频、Retry-After、熔断、凭据能力检查。
6. 用架构扫描禁止 Adapter 外出现 binance URL、/fapi/、urllib/requests/httpx/aiohttp。
7. 保留 Testnet 写能力为启动门禁关闭，直到 BD-T18。

## 5. 接口与合同

- `get_exchange_info(symbol?)`
- `get_closed_klines(symbol, interval, limit)`
- `get_account_snapshot()`
- `create_order(command)`
- `query_order_by_client_id(symbol, client_id)`
- `cancel_order(command)`
- `stream_user_events()`

## 6. 数据迁移

无业务数据迁移；替换依赖注入图。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/contracts/exchange tests/integration/exchange -q
```
```bash
python scripts/check_forbidden_patterns.py --repo .
```
```bash
pytest -q -k 'exchange and (error_taxonomy or rate_limit or clock or capability)'
```

## 9. 验收标准

AC-BD-T03-01. Adapter 外无 Binance 网络库、URL 和原始端点字符串。
AC-BD-T03-02. 账户查询失败返回 UNKNOWN/ERROR，绝不返回 EMPTY。
AC-BD-T03-03. create/cancel/query 使用真实传输层，且 Testnet contract test 可切换执行。
AC-BD-T03-04. 同一 correlation_id 贯穿 adapter request/response/log/metric。

验收状态仅允许：`PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE`。

## 10. 交付证据

- 修改文件清单与 Git diff --stat
- 执行命令、退出码、开始/结束时间、stdout/stderr 原文及 SHA-256
- 测试报告与覆盖率报告
- 数据库/API/运行证据（如适用）
- 本任务验收矩阵逐项结果
- 遗留问题、风险与回滚命令
- commit SHA、branch、config hash、policy version、agent ID

## 11. 完成后动作

- 更新 `docs/optimization/06_ACCEPTANCE_MATRIX.md`；
- 生成 `artifacts/evidence/BD-T03/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
