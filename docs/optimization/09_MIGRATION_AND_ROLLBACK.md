# 迁移与回滚

1. 先保存当前 PID 的 DB/WAL/日志/supervisor state 和旧 LaunchAgent；不得直接重启。
2. 在隔离副本执行 schema forward migration、checksum、replay、账本平衡和三方对账。
3. 通过 safety-only 启动新制品；没有 fresh account/position/algo/order facts 不得 RESUME。
4. 回滚只允许 schema 兼容的已签名制品；禁止 `git reset --hard`、覆盖用户未提交改动或直接删除 UNKNOWN。
5. 任一失败：`NO_NEW_RISK/CLOSE_ONLY`，保留 venue 原生保护，人工决定是否撤单/平仓；恢复必须重新 reconciliation。

