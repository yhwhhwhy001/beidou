
"""全链路真实数据 Replay、反作弊与确定性认证。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import CorrelationId

class CheatDetection(str, Enum):
    FUTURE_FUNCTION = "FUTURE_FUNCTION"
    EVENT_TIME_INVERSION = "EVENT_TIME_INVERSION"
    SURVIVORSHIP_BIAS = "SURVIVORSHIP_BIAS"
    SELECTION_BIAS = "SELECTION_BIAS"
    COST_MODEL_FORK = "COST_MODEL_FORK"
    UNREGISTERED_PARAM_CHANGE = "UNREGISTERED_PARAM_CHANGE"
    TEST_DATA_LEAKAGE = "TEST_DATA_LEAKAGE"

@dataclass
class ReplayResult:
    replay_id: str; deterministic: bool; cheat_checks: dict[CheatDetection, bool] = field(default_factory=dict)
    output_hash: str = ""; pnl_deviation_pct: float = 0.0
    correlation_id: CorrelationId | None = None

class ReplayValidator:
    """Replay 反作弊验证器。检查未来函数、幸存者偏差等。"""
    def __init__(self):
        self._baseline_hash: str | None = None

    def set_baseline(self, result_hash: str) -> None:
        self._baseline_hash = result_hash

    def check_future_function(self, signal_time: datetime, data_available_time: datetime) -> bool:
        return signal_time <= data_available_time

    def check_event_time_inversion(self, events: list[tuple[datetime, datetime]]) -> list[int]:
        """检查事件时间是否单调递增。"""
        inversions: list[int] = []
        for i in range(1, len(events)):
            if events[i][0] < events[i-1][0]:
                inversions.append(i)
        return inversions

    def check_survivorship(self, instruments_at_time: set[str], current_instruments: set[str]) -> set[str]:
        """检查是否存在幸存者偏差 — 使用当前活跃品种进行历史回测。"""
        return current_instruments - instruments_at_time

    def verify_determinism(self, result: ReplayResult) -> bool:
        if self._baseline_hash is None: return True
        return result.output_hash == self._baseline_hash

    def all_checks_pass(self, result: ReplayResult) -> bool:
        return all(result.cheat_checks.values())
