# 迁移与回滚总则

1. 所有数据库迁移 forward-only；已写事实不得通过 down migration 删除。
2. 迁移顺序：schema → dual-read validation（不得 dual-write 形成双事实源）→ historical import as NOT_VERIFIED → cutover → disable legacy → remove legacy。
3. 每次 cutover 前保存 schema hash、row count、sample hash、backup/restore 验证。
4. 回滚优先回滚应用流量和控制状态，不删除新事实表。
5. 订单、成交、仓位、保护、账本、对账的任何迁移失败均进入 LOCK。
6. 历史 SQLite/内存数据只能作为迁移参考，不能自动标记 VERIFIED。
