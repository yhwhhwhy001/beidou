# 任务依赖图

```text
BD-P0-01 取证/冻结/旧进程处置（授权后）
   └─> BD-P0-02 authority + readiness 证书
        ├─> BD-P0-03 PG/WAL/Outbox 原子事务
        ├─> BD-P0-04 user-stream replay + REST gap-fill
        └─> BD-P0-05 protection owner/semantic exact match
BD-P0-03 + BD-P0-04 + BD-P0-05
   └─> BD-P0-06 三方 reconciliation / crash chaos
BD-P0-06
   └─> BD-P1-01 PIT/manifest/research gate
        └─> BD-P1-02 StrategyKernel parity + Paper/Shadow
             └─> BD-P1-03 real G5/G7 window
                  └─> BD-P2-01 production operations / DR / launch
```

现有 `delivery/task-packages/BD-T00..BD-T19` 可作为历史切片；新增任务必须写明输入证据、失败优先测试、回滚、人工授权和禁止动作。

