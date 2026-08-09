# 算法与研究审查

## 已修复的确定性偏差

- 实时 IC 不再把当前预测和过去收益按列表位置配对；只在下一个相邻闭合 bar 计算 forward return。
- prediction samples 按 venue/symbol/timeframe/horizon 隔离；同一 bar 重复轮询、缺口、乱序不产生样本。
- `MarketDataFeed`、`ClosedBarNormalizer`、`LabelBuilder` 拒绝缺失/未闭合未来 bar；CLI 传递 close/PIT 元数据。
- Paper/Shadow 记录 QUEUED、REJECTED、PARTIALLY_FILLED、手续费、spread/slippage 和实际成本，成本偏差进入 Gate。

## 仍禁止的推断

- 正 IC、历史 Sharpe、学习 multiplier 不能推出未来盈利。
- `ACTIVE` 因子、Champion、交易池成员必须有外部 manifest、sealed OOS、成本容量、观察期和签名 Gate。
- 多 symbol 指标不得混合成一个生命周期/Champion 决策；当前多 scope 仅诊断，不能自动晋级。

