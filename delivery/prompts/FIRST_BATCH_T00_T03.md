# 北斗首批工程执行提示词：T00-T03

你是北斗工程收敛执行代理。目标仓库为 `yhwhhwhy001/beidou`。当前基线包记录为 `main@6b0d95dfa67465be421d9a4f5eaa5a406e7c3341`；如果实际 HEAD 不同，先生成 baseline delta 报告，不得直接假定方案仍完全适用。

## 强制读取

1. `00_EXECUTION_MASTER.md`
2. `01_AGENT_OPERATING_PROTOCOL.md`
3. `delivery.yaml`
4. `delivery/task-packages/BD-T00..BD-T03/` 全部文件
5. `delivery/task-packages/BD-T15/` 全部文件

## 执行顺序

```text
BD-T00
├─ BD-T01
├─ BD-T02
└─ BD-T15 基础门禁
BD-T01 + BD-T02 → BD-T03
```

严格一个任务一个 commit。每个任务开始前验证依赖 PASS；每个任务完成后运行 targeted tests、适用全量回归、回滚演练，并生成 evidence manifest。

## 本批必须达到

- 当前源码可编译、可收集测试；
- 默认审批密钥与无签名旁路彻底移除；
- 未知/缺失配置回退 SAFETY_ONLY；
- BinanceUsdmAdapter 成为唯一交易所边界；
- Adapter 外无 Binance URL、原始 `/fapi/`、urllib/requests/httpx/aiohttp；
- 账户/订单查询失败不返回 EMPTY；
- Testnet 写门保持关闭；Mainnet 永久禁止。

## 禁止

不得开始策略收益、因子、仓位、杠杆或止盈止损参数优化；不得删除/弱化测试；不得扩大 ignore/allowlist；不得使用 Mock 替代 Exchange Gateway 生产实现。

## 停止与输出

完成 T03 后立即停止，不自动开始 T04。输出每个任务的 commit、修改文件、迁移、命令/退出码/证据、验收结果、剩余风险、回滚命令和 G0/G1 当前状态。任何 P0 或关键证据缺失时返回 FAIL/NOT_VERIFIABLE。
