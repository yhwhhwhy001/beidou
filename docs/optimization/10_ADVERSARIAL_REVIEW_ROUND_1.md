# 对抗性审查 Round 1：事实链

最强反证：旧实例在实时心跳陈旧、订单链卡死、保护为空时仍呈现 HEALTHY/READY/RESUME。结论：P0 不是“增加重试”，而是让证书和写入互锁使用 fresh durable facts。Round 1 结果：**FAIL/HOLD**。

