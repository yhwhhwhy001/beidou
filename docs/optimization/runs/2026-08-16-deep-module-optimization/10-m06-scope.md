# M06 策略模块 — 范围定义

## Phase A 结论（已核实）

| 缺陷 | 位置 | 判定 |
|---|---|---|
| 类型化图 Exit 输出在实盘被丢弃：kernel 返回 dict 无 exit_signals 键（engine.py:8401 读空列表）；且一旦传播，Exit 提案（side=None）会经 all_signals 分支混入入场流程（best_signal → sizing/BUY/SELL） | kernel_parity.py:203-210; engine.py:8400-8423 | **P0** |
| 8 个 alpha 组件 validate() 全部 return True 空桩（参数从不校验） | engine.py（8 处） | P1 |
| 两套内核语义分裂：TypedStrategyKernel（仅测试）vs TypedAlphaGraph（生产）；DEGRADE 乘数 0.5 硬编码两处不一致 | typed_kernel.py:161-162 vs typed_graph.py:365-366 | P2 |
| 纯指标条件拼接（无统计/ML 模型）；71 个 ACTIVE 模板因子 + 8 个 IDEA 有名组件（实盘策略本质是模板因子组合） | — | 架构事实,登记 |
| **R-M03-1 阈值重标定**：Wilder RSI/ATR/年化修正后策略阈值（rsi<70 等、vol 档位、stress 门）基于旧数学标定 | engine.py:719/722/1002; adaptive.py:232-241 | **P0 级行为变更（待回测证据）** |

## 任务

- F01（P0）：kernel 显式返回 exit_signals（EXIT 节点 data 非空集合）；engine typed 分支分离处理 —— 入场提案走现有 strength 排名，退出信号独立收集（审计计数+日志，不混入入场流程）；退出执行（reduce-only target delta）登记 M06-R2/M12
- F02（P1）：8 个 validate() 实现真实参数校验（finite/范围/一致性），无效参数 → False 且接线期拒绝
- F03（P2）：DEGRADE 乘数收敛 —— 生产路径 TypedAlphaGraph 与 TypedStrategyKernel 使用同一常量来源；登记语义统一
- F04：策略行为契约测试 —— typed graph 全节点 fail-closed 场景、exit 不新增风险（side None 强制）、kernel 返回形状契约
- F05：回归 + 对抗审查 + 证据

## 不做（登记残余）

- 阈值重标定（R-M03-1）—— 需要回测证据（M08 完成真实成本模型后执行），本模块只登记 + 加审计日志（当前运行引擎用旧阈值=旧行为,重标定前不得改动阈值）
- 退出信号的真实执行（reduce-only 平仓）→ M06-R2/M12
- Alpha 模型升级（统计/ML）→ 研究演进
