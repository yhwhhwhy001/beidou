"""BD-CV13: Trading Universe 滞回与最小驻留期。

晋升/降级使用滞回，禁止单次最新评分决定生命周期。
回测只能使用当时可得 UniverseSnapshot，禁止 survivorship/look-ahead。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UniverseEntry:
    symbol: str
    listing_age_days: float = 0.0
    daily_volume: float = 0.0
    # M02-R2（对抗审查 CE-3）: None = 未知/未提供 —— 旧实现用 0.0 作
    # UNKNOWN 哨兵,把合法零费率/零 OI 误判为关键字段缺失阻断晋级。
    funding_rate: float | None = None
    open_interest: float | None = None
    dq_ok: bool = False
    capacity_score: float = 0.0  # 0.0-1.0
    is_executable: bool = False
    exclude_reason: str = ""
    observed_at: str = ""
    source: str = ""

    def is_promotable(self) -> bool:
        """BD-CV13 AC-13-02: 关键字段 UNKNOWN（None）→ 不允许晋级。"""
        if self.funding_rate is None or self.open_interest is None:
            return False
        if self.capacity_score <= 0.0:
            return False
        return self.dq_ok


@dataclass
class UniverseHysteresis:
    """BD-CV13: Universe 滞回管理。

    晋升: 需要至少 MIN_DWELL 个连续观测周期满足条件。
    降级: 需要至少 MIN_DWELL 个连续观测周期不满足条件。
    禁止单次最新评分决定生命周期。
    """

    MIN_DWELL_PERIODS: int = 3  # 最小驻留期（观测周期数）
    PROMOTION_THRESHOLD: float = 0.6  # 晋升阈值
    DEMOTION_THRESHOLD: float = 0.3  # 降级阈值

    entries: dict[str, UniverseEntry] = field(default_factory=dict)
    promotion_streaks: dict[str, int] = field(default_factory=dict)
    demotion_streaks: dict[str, int] = field(default_factory=dict)

    def observe(self, entry: UniverseEntry) -> None:
        """BD-CV13: 记录一次观测。更新晋升/降级 streak。"""
        symbol = entry.symbol
        self.entries[symbol] = entry

        if entry.is_promotable() and entry.capacity_score >= self.PROMOTION_THRESHOLD:
            self.promotion_streaks[symbol] = self.promotion_streaks.get(symbol, 0) + 1
            self.demotion_streaks[symbol] = 0
        elif not entry.is_promotable() or entry.capacity_score <= self.DEMOTION_THRESHOLD:
            self.demotion_streaks[symbol] = self.demotion_streaks.get(symbol, 0) + 1
            self.promotion_streaks[symbol] = 0
        else:
            # 中间状态 — 重置 streaks
            self.promotion_streaks[symbol] = 0
            self.demotion_streaks[symbol] = 0

    def should_promote(self, symbol: str) -> bool:
        """BD-CV13: 需要 MIN_DWELL 连续满足 → 才晋升。"""
        streak = self.promotion_streaks.get(symbol, 0)
        return streak >= self.MIN_DWELL_PERIODS

    def should_demote(self, symbol: str) -> bool:
        """BD-CV13: 需要 MIN_DWELL 连续不满足 → 才降级。"""
        streak = self.demotion_streaks.get(symbol, 0)
        return streak >= self.MIN_DWELL_PERIODS

    def executable_symbols(self) -> list[str]:
        """返回应晋升的 symbol 列表。"""
        result = []
        for symbol, entry in self.entries.items():
            if self.should_promote(symbol) and entry.is_promotable():
                entry.is_executable = True
                result.append(symbol)
            elif self.should_demote(symbol):
                entry.is_executable = False
                entry.exclude_reason = "UNIVERSE_DEMOTED"
        return result

    def snapshot_for_backtest(self, as_of: str) -> list[UniverseEntry]:
        """BD-CV13 AC-13-01: 回测只能使用当时可得 UniverseSnapshot。

        禁止 survivorship/look-ahead：返回 as_of 时间点之前的观测结果。
        """
        return [e for e in self.entries.values() if e.observed_at <= as_of and e.is_executable]
