# 回滚方案

1. 停止 PromotionService；保留事件表；将运行状态降为 PAPER/HOLD，不反向删除生命周期事件。

回滚后必须重新执行本任务前置 Gate，并保存回滚证据。
