"""G5 协议测试场景包 — 场景基类与共享基础设施。

每个场景模块继承 g5_scenarios.base 的 ScenarioBase 并注册到 SCENARIO_REGISTRY。
"""

from beidou_certification.g5_scenarios import engine

__all__ = ["engine"]
