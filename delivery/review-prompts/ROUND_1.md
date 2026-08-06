# Round 1: 代码与架构独立审查

使用与开发代理不同的独立上下文。只基于当前 commit 和原始证据审查，不接受提交说明作为证明。

重点：网络旁路、双事实源、默认值、静默失败、类型豁免、状态机和事务漏洞。

输出每项 Finding：ID、级别、代码/证据位置、触发条件、失败模式、影响、为什么测试未发现、修复、验收、Falsifier。

结论只能是 PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE。发现 P0 立即阻断后续 Gate。
