"""G5 协议测试场景包 — 场景基类与共享基础设施。

每个场景模块继承 g5_scenarios.base 的 ScenarioBase 并注册到 SCENARIO_REGISTRY。
Ruling-6:engine/protocol/restart/protection 四个子包均在包导入时接线注册
(Task 4/5 的协议组场景、Task 9-11 的重启组与原生保护组不再游离于注册表之外)。
"""

from beidou_certification.g5_scenarios import engine, protection, protocol, restart

__all__ = ["engine", "protection", "protocol", "restart"]
