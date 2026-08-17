"""G5 重启组场景包 — process_restart / database_restart / user_stream_reconnect。

import 场景模块即触发各模块尾部的 SCENARIO_REGISTRY 注册(重启组语义见
runner.RESTART_GROUP,run_g5.py 的 --skip-restart 即按该集合跳过本组场景)。
"""

from beidou_certification.g5_scenarios.restart import (
    database_restart,
    process_restart,
    user_stream_reconnect,
)

__all__ = ["database_restart", "process_restart", "user_stream_reconnect"]
