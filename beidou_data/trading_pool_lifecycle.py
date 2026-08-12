"""交易池动态生命周期管理 — BD-04。

根据点差、深度、成交量、上市时间、规则稳定性、拒单率和容量动态管理标的生命周期。
状态: OBSERVING → PROMOTED → ACTIVE → QUARANTINED → DELISTED。
有观察期证据、迟滞和自动降级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class PoolStatus(str, Enum):
    OBSERVING = "OBSERVING"  # 观察期，不可交易
    PROMOTED = "PROMOTED"  # 晋级中，只读
    ACTIVE = "ACTIVE"  # 可交易
    QUARANTINED = "QUARANTINED"  # 隔离，仅平仓
    DELISTED = "DELISTED"  # 下架


# P1-037: 默认评分权重 — 可被签名策略覆盖
_DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "spread": 0.25,
    "depth": 0.25,
    "volume": 0.20,
    "stability": 0.15,
    "capacity": 0.15,
}


@dataclass
class InstrumentScore:
    """标的综合评分。"""

    instrument_id: str
    spread_score: float = 0.0  # 点差评分 (0-1, 越高越好)
    depth_score: float = 0.0  # 深度评分
    volume_score: float = 0.0  # 成交量评分
    stability_score: float = 0.0  # 稳定性评分 (拒单率等)
    capacity_score: float = 0.0  # Alpha容量评分
    overall: float = 0.0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def compute_overall(self, weights: dict[str, float] | None = None) -> float:
        # P1-037: 默认权重 — 可被签名策略覆盖
        w = weights or _DEFAULT_SCORE_WEIGHTS
        self.overall = (
            self.spread_score * w.get("spread", 0.25)
            + self.depth_score * w.get("depth", 0.25)
            + self.volume_score * w.get("volume", 0.20)
            + self.stability_score * w.get("stability", 0.15)
            + self.capacity_score * w.get("capacity", 0.15)
        )
        return self.overall


@dataclass
class PoolEntry:
    """交易池条目。"""

    instrument_id: str
    status: PoolStatus = PoolStatus.OBSERVING
    observing_since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    promoted_at: datetime | None = None
    quarantine_reason: str | None = None
    scores: list[InstrumentScore] = field(default_factory=list)
    min_observation_hours: float = 24.0  # 最少观察24小时
    capacity_used_pct: float = 0.0  # BD-FIX: 容量使用率追踪
    max_position_notional: float = 0.0  # BD-FIX: 最大持仓名义值


class TradingPool:
    """交易池 — 点时宇宙管理。

    特性:
    - 有观察期证据才能 promote（不能任意直接 promote）
    - 自动降级：评分连续低于阈值→QUARANTINED
    - 迟滞：降级后需要持续改进才能重新 promote
    """

    PROMOTE_THRESHOLD = 0.6  # 综合评分 >= 0.6 才考虑晋级
    DEGRADE_THRESHOLD = 0.3  # 综合评分 < 0.3 触发降级
    DEGRADE_CONSECUTIVE = 3  # 连续3次低于阈值才降级（迟滞）
    policy_version: str = ""  # P1-037: 签名策略版本

    def __init__(
        self,
        max_instruments: int = 50,
        event_sink: Any = None,
        initial_state: list[dict[str, Any]] | None = None,
        policy_version: str = "UNKNOWN",
        source: str = "MARKET_QUALITY_OBSERVATION",
    ):
        self._pool: dict[str, PoolEntry] = {}
        self._max_instruments = max_instruments
        self._event_sink = event_sink
        self._policy_version = policy_version
        self._source = source
        # Restore persisted state on startup
        if initial_state:
            for state in initial_state:
                inst_id = str(state.get("instrument_id", ""))
                if not inst_id:
                    continue
                entry = PoolEntry(instrument_id=inst_id)
                raw_status = str(state.get("status", "OBSERVING"))
                try:
                    entry.status = PoolStatus(raw_status)
                except ValueError:
                    entry.status = PoolStatus.OBSERVING
                # BD-FIX: 恢复观察期起点与最近评分，否则每次重启
                # observing_since 重置为 now，24h 观察期重新计时，
                # 标的可能永远无法晋级 ACTIVE。
                detail = state.get("score_detail")
                if isinstance(detail, dict):
                    raw_observing = detail.get("observing_since")
                    if raw_observing:
                        try:
                            parsed_observing = datetime.fromisoformat(str(raw_observing))
                            if parsed_observing.tzinfo is None:
                                parsed_observing = parsed_observing.replace(tzinfo=timezone.utc)
                            entry.observing_since = parsed_observing
                        except (TypeError, ValueError):
                            pass
                    raw_promoted = detail.get("promoted_at")
                    if raw_promoted:
                        try:
                            parsed_promoted = datetime.fromisoformat(str(raw_promoted))
                            if parsed_promoted.tzinfo is None:
                                parsed_promoted = parsed_promoted.replace(tzinfo=timezone.utc)
                            entry.promoted_at = parsed_promoted
                        except (TypeError, ValueError):
                            pass
                    if detail.get("quarantine_reason"):
                        entry.quarantine_reason = str(detail["quarantine_reason"])
                    raw_scores = detail.get("scores")
                    if isinstance(raw_scores, list):
                        restored_scores: list[InstrumentScore] = []
                        for raw_score in raw_scores:
                            try:
                                restored_scores.append(
                                    InstrumentScore(instrument_id=inst_id, overall=float(raw_score))
                                )
                            except (TypeError, ValueError):
                                continue
                        entry.scores = restored_scores
                self._pool[inst_id] = entry

    def add(self, instrument_id: str) -> PoolEntry:
        if instrument_id not in self._pool:
            entry = PoolEntry(instrument_id=instrument_id)
            self._pool[instrument_id] = entry
        return self._pool[instrument_id]

    def _persist(self, entry: PoolEntry) -> None:
        """BD-FIX: 持久化标的生命周期状态（含观察期起点与最近评分）。

        此前 event_sink 只被存储从不被调用，trading_pool_events 表永远为空：
        每次重启所有标的回到 OBSERVING 且观察期重置，宇宙无法晋级。
        持久化失败只记录告警，不得阻断评分流程本身。
        """
        if self._event_sink is None:
            return
        try:
            detail: dict[str, Any] = {
                "observing_since": entry.observing_since.isoformat(),
                "promoted_at": entry.promoted_at.isoformat() if entry.promoted_at else None,
                "quarantine_reason": entry.quarantine_reason,
                "scores": [float(s.overall) for s in entry.scores[-20:]],
            }
            self._event_sink(
                {
                    "instrument_id": entry.instrument_id,
                    "status": entry.status.value,
                    "score": float(entry.scores[-1].overall) if entry.scores else 0.0,
                    "score_detail": detail,
                }
            )
        except Exception as exc:
            logging.getLogger("beidou.trading_pool").warning(
                "trading-pool persistence failed for %s: %s", entry.instrument_id, type(exc).__name__
            )

    def score(self, instrument_id: str, score: InstrumentScore) -> None:
        entry = self._pool.get(instrument_id)
        if not entry:
            return
        score.compute_overall()
        entry.scores.append(score)

        # 自动降级检查
        if entry.status == PoolStatus.ACTIVE:
            recent = entry.scores[-self.DEGRADE_CONSECUTIVE :]
            if len(recent) >= self.DEGRADE_CONSECUTIVE:
                if all(s.overall < self.DEGRADE_THRESHOLD for s in recent):
                    entry.status = PoolStatus.QUARANTINED
                    entry.quarantine_reason = (
                        f"Score below {self.DEGRADE_THRESHOLD} for {self.DEGRADE_CONSECUTIVE} consecutive evaluations"
                    )
        self._persist(entry)

    def try_promote(self, instrument_id: str) -> bool:
        """尝试晋级。需要观察期满 + 评分达标。

        PKG18 (BDS-P0-022): 三重验证 — finite + range + threshold。
        NaN/Inf 评分不能绕过晋级阈值（NaN < threshold 在 Python 中为 False）。
        """
        import math as _math

        entry = self._pool.get(instrument_id)
        if not entry or entry.status != PoolStatus.OBSERVING:
            return False

        # 观察期检查
        elapsed = (datetime.now(timezone.utc) - entry.observing_since).total_seconds() / 3600
        if elapsed < entry.min_observation_hours:
            return False

        # 评分检查 — PKG18: finite + range + threshold 三重验证
        if not entry.scores:
            return False
        latest = entry.scores[-1]

        # Step 1: finite 检查 — NaN/Inf 绝不晋级
        if not _math.isfinite(latest.overall):
            return False
        for score_attr in ("spread_score", "depth_score", "volume_score", "stability_score", "capacity_score"):
            val = getattr(latest, score_attr, 0.0)
            if not _math.isfinite(val):
                return False

        # Step 2: range 检查 — 评分必须在 [0, 1] 范围内
        if not (0.0 <= latest.overall <= 1.0):
            return False

        # Step 3: threshold 检查
        if latest.overall < self.PROMOTE_THRESHOLD:
            return False

        entry.status = PoolStatus.PROMOTED
        entry.promoted_at = datetime.now(timezone.utc)
        self._persist(entry)
        return True

    def activate(self, instrument_id: str) -> bool:
        entry = self._pool.get(instrument_id)
        if not entry or entry.status != PoolStatus.PROMOTED:
            return False
        if self.active_count() >= self._max_instruments:
            return False
        entry.status = PoolStatus.ACTIVE
        self._persist(entry)
        return True

    def quarantine(self, instrument_id: str, reason: str) -> None:
        entry = self._pool.get(instrument_id)
        if entry:
            entry.status = PoolStatus.QUARANTINED
            entry.quarantine_reason = reason
            self._persist(entry)

    def active_instruments(self) -> list[str]:
        return [iid for iid, e in self._pool.items() if e.status == PoolStatus.ACTIVE]

    def active_count(self) -> int:
        return len(self.active_instruments())

    def is_tradable(self, instrument_id: str) -> bool:
        entry = self._pool.get(instrument_id)
        if entry is None or entry.status != PoolStatus.ACTIVE:
            return False
        # BD-FIX: 容量门禁 — capacity_used_pct >= 100% 不可交易
        return not entry.capacity_used_pct >= 100.0

    def update_capacity(self, instrument_id: str, notional: float) -> None:
        """BD-FIX: 更新容量使用率。下单时增加，平仓后减少。"""
        entry = self._pool.get(instrument_id)
        if entry is not None and entry.max_position_notional > 0:
            entry.capacity_used_pct = min(100.0, max(0.0, (notional / entry.max_position_notional) * 100.0))

    def set_max_position_notional(self, instrument_id: str, max_notional: float) -> None:
        """BD-FIX: 设置最大持仓名义值（用于容量计算）。"""
        entry = self._pool.get(instrument_id)
        if entry is not None:
            entry.max_position_notional = max_notional
