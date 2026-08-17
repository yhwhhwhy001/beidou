"""G5 协议测试场景包 — 场景基类与共享基础设施。

每个场景模块继承 g5_scenarios.base 的 ScenarioBase 并注册到 SCENARIO_REGISTRY。
Ruling-6:engine/protocol/restart/protection 四个子包均在包导入时接线注册
(Task 4/5 的协议组场景、Task 9-11 的重启组与原生保护组不再游离于注册表之外)。
"""

from beidou_certification.g5_scenarios import engine, protection, protocol, restart
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

# Ruling-20(认证轮 #10 证据):partial_fill 部分成交撤单后引擎 order_state 卡
# PARTIALLY_FILLED ghost 订单(引擎对 CANCELED-with-executedQty 事件不落
# order_state 终态,缺陷待修)—— 该状态让后续场景 reconciliation_mismatch
# 恢复超时 FAIL、重启组前置 NOT_VERIFIABLE。裁决:partial_fill 移至注册表
# 末尾最后执行(认证轮结束后由运维清理 ghost order_state 并 kickstart
# 恢复引擎);场景语义与 EXPECTED_ORDER pin 同步更新。
SCENARIO_REGISTRY["partial_fill"] = SCENARIO_REGISTRY.pop("partial_fill")

__all__ = ["engine", "protection", "protocol", "restart"]
