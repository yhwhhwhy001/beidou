# 术语表

这份词表定的是**怎么写**，不是概念本身是什么意思。概念的定义在 `CONTEXT.md`，实现决策在 `docs/ARCHITECTURE.md`，判定过程在 `docs/RESEARCH_LOG.md`。

## 判据

1. 这个概念在仓库里有对应标识符（`grep` 得到 `ledger`、`overlay`、`holdout`……），且现有中文写法是临时译的 → **写英文原形**。
2. 中文量化写作里本来就这么说（回撤、夏普、敞口、再平衡） → **写中文**。
3. 两条都不沾 → 写英文，并把结果补进本表。

不要为了避免中英混排而发明新中文词。混排可以接受，两套写法并存不行。

## 写英文

| 写这个 | 不要再写 | 代码里的标识符 |
| --- | --- | --- |
| holdout | 留出、留出集、留出期 | `holdout` |
| fold | 折、折号、折级、逐折、负折 | `n_folds`、`min_train` |
| ledger | 账本 | `ledger` |
| trials ledger | 试验账本 | `ledger` |
| ratchet | 棘轮 | `ratchet` |
| source budget ratchet | 行数棘轮 | `ratchet` |
| incumbent | 在位者 | `incumbent` |
| headroom | 余量 | `headroom` |
| exit overlay | 退出层、退出叠加层 | `overlay` |
| overlay | 叠加层 | `overlay` |
| sleeve | 分仓、袖子 | `sleeve` |
| universe | 标的池、宇宙 | `universe` |
| throttle | 节流阀 | `drawdown_throttle` |
| family gate | 家族门 | `family_gate` |
| ceiling | 天花板 | `ceiling` |
| alert | 呼叫（名词用法） | `alerts` |
| regime | 行情状态 | `regime_split_sharpes`、`regime_window` |
| beta | 贝塔 | `rolling_beta`、`beidou report beta` |
| VaR、ES | 在险价值、预期损失 | —（仓库没有实现，2026-09-23 清点） |

## 保留中文的近形词

这些字面上像术语，其实是正常中文，不要替换：折算、折扣、打折、折现、折减、折叠、折成、折进。「呼叫操作者」「BLIND 不呼叫」里的`呼叫`是动词，也保留——只有指 `alerts` 里那条记录时才写 `alert`。

## 写中文

这些词有现成的中文说法，不要改成英文：回撤、最大回撤、夏普、杠杆、敞口、毛敞口、净敞口、再平衡、再平衡带、止盈、止损、移动止损、样本外、样本内、置信区间、风险预算、风险预算阶梯、换手、滑点、成交、权益、波动率、逆波动率定价、基准、三分位。

`书级` / `仓位级`这对说法改成**组合层** / **仓位层**——两个都是通用中文，不必用 `book-level`。

`预期书`是自造词，`expectation book` 也是自造的英文，两个都不用。写**预期说明**。

## 没定的怎么办

写英文原形，然后在同一个提交里把它加进上面的表。不要留着两种写法等以后统一。
