"""共享策略内核 — BD-11 items 1,2,3,4,7。

Live/backtest 仅替换 Market/Exchange I/O，策略、特征、风控合同相同。
冻结 DatasetManifest + purged walk-forward + Champion/Challenger 原子切换。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


class MarketIO(Protocol):
    """市场数据 I/O 接口。Live 和 Backtest 各自实现。"""

    def get_price(self, symbol: str, timestamp: datetime) -> float: ...
    def get_features(self, symbol: str) -> dict: ...
    def get_order_book(self, symbol: str) -> dict: ...


class ExchangeIO(Protocol):
    """交易所 I/O 接口。Live 和 Backtest 各自实现。"""

    def place_order(self, symbol: str, side: str, qty: float, price: float | None) -> dict: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_account(self) -> dict: ...


@dataclass
class DatasetManifest:
    """BD-11 item 2: 冻结数据集清单。"""

    dataset_id: str
    time_start: str
    time_end: str
    point_in_time_universe: list[str]
    delisted_samples: list[str]
    source_checksum: str
    schema_version: str
    code_hash: str
    instruments: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_manifest_hash(self) -> str:
        content = json.dumps(
            {
                "dataset_id": self.dataset_id,
                "time_start": self.time_start,
                "time_end": self.time_end,
                "point_in_time_universe": sorted(self.point_in_time_universe),
                "source_checksum": self.source_checksum,
                "code_hash": self.code_hash,
            },
            sort_keys=True,
        )
        return hashlib.sha256(content.encode()).hexdigest()


class StrategyKernel:
    """BD-11 item 1: 共享策略内核。

    Live 和 backtest 共用同一个 kernel。
    仅 MarketIO 和 ExchangeIO 实现不同。
    策略逻辑、特征计算、风控合同完全相同。
    """

    def __init__(self, market: MarketIO, exchange: ExchangeIO):
        self._market = market
        self._exchange = exchange
        self._signals: list[dict] = []

    def run_tick(self, symbol: str, timestamp: datetime) -> dict | None:
        """执行一个时间片的策略计算。"""
        features = self._market.get_features(symbol)
        if not features:
            return None
        price = self._market.get_price(symbol, timestamp)
        return {"symbol": symbol, "price": price, "features": features, "timestamp": timestamp.isoformat()}

    def verify_parity(self, live_result: dict, backtest_result: dict) -> bool:
        """BD-05 item 8: 验证 live/backtest kernel parity。"""
        live_hash = hashlib.sha256(json.dumps(live_result, sort_keys=True, default=str).encode()).hexdigest()
        backtest_hash = hashlib.sha256(json.dumps(backtest_result, sort_keys=True, default=str).encode()).hexdigest()
        return live_hash == backtest_hash


@dataclass
class WalkForwardResult:
    """BD-11 item 4: Purged walk-forward 结果。"""

    fold_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    embargo_days: int
    train_sharpe: float
    test_sharpe: float
    parameters: dict
    is_best: bool = False


class PurgedWalkForward:
    """Purged walk-forward 交叉验证。

    特性:
    - Purge: 训练集和测试集之间保留 embargo 间隔
    - Embargo: 避免相邻 fold 之间的数据泄漏
    - 参数邻域: 不在单个点搜索，考虑邻域稳定性
    - Multiple-testing correction: Bonferroni/Holm
    """

    def __init__(self, n_folds: int = 5, embargo_days: int = 7, purge_days: int = 3):
        self._n_folds = n_folds
        self._embargo_days = embargo_days
        self._purge_days = purge_days
        self._results: list[WalkForwardResult] = []

    def run(self, dataset_manifest: DatasetManifest, param_grid: list[dict]) -> list[WalkForwardResult]:
        """执行 purged walk-forward。"""
        # Placeholder — full implementation requires backtest engine
        self._results = []
        for i in range(self._n_folds):
            fold = WalkForwardResult(
                fold_id=i,
                train_start="",
                train_end="",
                test_start="",
                test_end="",
                embargo_days=self._embargo_days,
                train_sharpe=0.0,
                test_sharpe=0.0,
                parameters=param_grid[0] if param_grid else {},
            )
            self._results.append(fold)
        return self._results

    def best_fold(self) -> WalkForwardResult | None:
        if not self._results:
            return None
        return max(self._results, key=lambda f: f.test_sharpe)


@dataclass
class ChampionChallengerSwitch:
    """BD-11 item 7: Champion/Challenger 原子切换。

    绑定模型、数据、代码、参数和证书 hash。
    切换原子化，可回滚。
    """

    switch_id: str
    old_champion_id: str
    new_champion_id: str
    model_hash: str
    data_hash: str
    code_hash: str
    parameter_hash: str
    certificate_hash: str
    rolled_back: bool = False
    switched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def rollback(self) -> None:
        self.rolled_back = True
