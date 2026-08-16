"""G5 引擎组场景包 — ack_loss / timeout_unknown_recovery。

import 两个场景模块即触发各模块尾部的 SCENARIO_REGISTRY 注册。
"""

from beidou_certification.g5_scenarios.engine import ack_loss, timeout_unknown_recovery

__all__ = ["ack_loss", "timeout_unknown_recovery"]
