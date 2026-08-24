"""G5 重启组场景包 — process_restart / database_restart / user_stream_reconnect。

import 场景模块即触发各模块尾部的 SCENARIO_REGISTRY 注册(重启组语义见
runner.RESTART_GROUP,run_g5.py 的 --skip-restart 即按该集合跳过本组场景)。
"""

from beidou_certification.g5_scenarios.restart import (
    database_restart,
    process_restart,
    user_stream_reconnect,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

# Imports are kept in canonical order for tooling, then the registry is
# explicitly ordered so the destructive restart probes run before the
# user-stream probe can induce a transient incident.
for _scenario_id in ("process_restart", "database_restart", "user_stream_reconnect"):
    _scenario = SCENARIO_REGISTRY.pop(_scenario_id)
    SCENARIO_REGISTRY[_scenario_id] = _scenario

__all__ = ["database_restart", "process_restart", "user_stream_reconnect"]
