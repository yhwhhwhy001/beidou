"""G5 协议组场景包 — create_query_cancel / stable_client_order_id / clock_skew。

import 三个场景模块即触发各模块尾部的 SCENARIO_REGISTRY 注册。
"""

from beidou_certification.g5_scenarios.protocol import clock_skew, create_query_cancel, stable_client_order_id

__all__ = ["clock_skew", "create_query_cancel", "stable_client_order_id"]
