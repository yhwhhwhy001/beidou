"""全链路真实数据 Replay、反作弊与确定性认证。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    replay_id: str
    deterministic: bool
    cheat_checks: dict[CheatDetection, bool] = field(default_factory=dict)
    output_hash: str = ""
    pnl_deviation_pct: float = 0.0
    correlation_id: CorrelationId | None = None


class ReplayValidator:
    """Replay 反作弊验证器。检查未来函数、幸存者偏差等。"""

    def __init__(self):
        self._baseline_hash: str | None = None

    def set_baseline(self, result_hash: str) -> None:
        self._baseline_hash = result_hash

    def check_future_function(self, signal_time: datetime, data_available_time: datetime) -> bool:
        """检查未来函数：信号时间必须 >= 数据可用时间。

        如果 signal_time < data_available_time，说明信号使用了未来才能获得的数据。
        """
        return signal_time >= data_available_time

    def check_event_time_inversion(self, events: list[tuple[datetime, datetime]]) -> list[int]:
        """检查事件时间是否单调递增。"""
        inversions: list[int] = []
        for i in range(1, len(events)):
            if events[i][0] < events[i - 1][0]:
                inversions.append(i)
        return inversions

    def check_survivorship(self, instruments_at_time: set[str], current_instruments: set[str]) -> set[str]:
        """检查是否存在幸存者偏差 — 使用当前活跃品种进行历史回测。"""
        return current_instruments - instruments_at_time

    def verify_determinism(self, result: ReplayResult) -> bool:
        if self._baseline_hash is None:
            return True
        return result.output_hash == self._baseline_hash

    def all_checks_pass(self, result: ReplayResult) -> bool:
        return all(result.cheat_checks.values())

"""历史 replay 模拟：为 PAPER_TRADING/CHALLENGER 级提供证据（仅 testnet 语义）。"""


@dataclass
class PaperReplayResult:
    paper_sharpe: float = 0.0
    paper_drawdown_pct: float = 0.0
    signal_consistency: float = 0.0
    challenger_icir: float = 0.0
    window_bars: int = 0
    n_trades: int = 0
    evidence_source: str = "historical_replay"


def simulate_paper_window(
    factor_values: list[float],
    closes: list[float],
    cost_bps: float,
    *,
    min_window_bars: int = 500,
    bars_per_year: int = 24 * 365,
) -> PaperReplayResult | None:
    """历史窗口 paper 模拟。

    无前视：bar i 收盘出信号，bar i+1 开盘执行。
    换仓时扣 cost_bps/10000 名义成本。有效 bar 不足 min_window_bars 返回 None。
    """
    import math

    n = min(len(factor_values), len(closes))
    if n < min_window_bars + 1:
        return None
    equity = 1.0
    returns: list[float] = []
    positions: list[float] = [0.0] * n
    for i in range(n):
        value = factor_values[i]
        positions[i] = 1.0 if value > 1e-9 else (-1.0 if value < -1e-9 else 0.0)
    trades = 0
    equity_curve: list[float] = [1.0]
    peak = 1.0
    max_dd = 0.0
    prev_pos = 0.0  # 窗口起点持有现金：首次建仓同样按换仓扣费
    for i in range(n - 1):
        pos = positions[i]
        if pos != prev_pos:
            trades += 1
            ret_cost = -cost_bps / 10000.0
            returns.append(ret_cost)
            equity *= 1.0 + ret_cost
            equity_curve.append(equity)
            peak = max(peak, equity)
            dd = (equity - peak) / peak
            max_dd = min(max_dd, dd)
        prev_pos = pos
        if closes[i] <= 0:
            continue
        ret = pos * (closes[i + 1] - closes[i]) / closes[i]
        returns.append(ret)
        equity *= 1.0 + ret
        equity_curve.append(equity)
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)

    mean_ret = sum(returns) / len(returns) if returns else 0.0
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    std_ret = math.sqrt(max(var_ret, 0.0))
    # M08-F04: 年化频率参数化 —— 旧实现硬编码 √24(仅 1h 正确)。
    sharpe = (mean_ret / std_ret) * math.sqrt(bars_per_year) if std_ret > 0 else 0.0

    consistency = 0.0
    forward_bars = 4
    hits = 0
    denom = 0
    for i in range(n - forward_bars):
        if abs(factor_values[i]) <= 1e-9:
            continue
        fwd = (closes[i + forward_bars] - closes[i]) / closes[i] if closes[i] > 0 else 0.0
        denom += 1
        if fwd * factor_values[i] > 0:
            hits += 1
    if denom:
        consistency = hits / denom

    # M08-F04: challenger_icir 字段语义修正 —— 旧实现把 signal×return
    # 乘积序列的 mean/std 冒充 "IC 序列 IR"(不是相关系数,且单标的窗口
    # 内无法构造 IC 序列)。诚实语义 = paper PnL 的信息比率(即 sharpe,
    # 年化口径一致)。字段名保留兼容,含义更正。
    return PaperReplayResult(
        paper_sharpe=round(sharpe, 6),
        paper_drawdown_pct=round(max_dd * 100, 6),
        signal_consistency=round(consistency, 6),
        challenger_icir=round(sharpe, 6),
        window_bars=n,
        n_trades=trades,
    )
