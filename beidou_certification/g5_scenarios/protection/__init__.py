"""G5 原生保护组场景包 — native_protection / double_worker_fencing。

import 两个场景模块即触发各模块尾部的 SCENARIO_REGISTRY 注册。
"""

from beidou_certification.g5_scenarios.protection import (
    double_worker_fencing,
    native_protection,
)

__all__ = ["double_worker_fencing", "native_protection"]
