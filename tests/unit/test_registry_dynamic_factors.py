from beidou_launcher.models import CheckStatus
from beidou_launcher.registry import inspect_engine_wiring


class _FakeComponent:
    def validate(self) -> bool:
        return True


class _FakeGraph:
    def __init__(self, component_ids: list[str]) -> None:
        self._components = {cid: _FakeComponent() for cid in component_ids}

    def topological_order(self) -> list[str]:
        return sorted(self._components)


class _FakeRecord:
    def __init__(self, state: str) -> None:
        self.lifecycle = _FakeLifecycle(state)


class _FakeLifecycle:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeRegistry:
    def __init__(self, factors: dict[str, str]) -> None:
        self._factors = {fid: _FakeRecord(state) for fid, state in factors.items()}


class _FakePool:
    def __init__(self) -> None:
        self._pool: dict = {}

    def active_count(self) -> int:
        return 0


class _FakeEngine:
    def __init__(self) -> None:
        self._alpha_graph = _FakeGraph(
            [
                "meanrev_entry_v1",
                "trend_entry_v1",
                "breakout_entry_v1",
                "momentum_filter_v1",
                "volatility_filter_v1",
                "volume_filter_v1",
                "trailing_exit_v1",
                "time_exit_v1",
                "mined_factor_abc",  # 动态挖掘因子：合法扩展，不应 FAIL
            ]
        )
        self._factor_registry = _FakeRegistry(
            {
                "meanrev_entry_v1": "ACTIVE",
                "trend_entry_v1": "ACTIVE",
                "breakout_entry_v1": "ACTIVE",
                "momentum_filter_v1": "ACTIVE",
                "volatility_filter_v1": "ACTIVE",
                "volume_filter_v1": "ACTIVE",
                "trailing_exit_v1": "ACTIVE",
                "time_exit_v1": "ACTIVE",
                "mined_factor_abc": "ACTIVE",
            }
        )
        self._trading_pool = _FakePool()
        self._strategy_risk = None
        self._autopilot_strategy_id = "autopilot"


def test_dynamic_factor_components_do_not_fail_graph_check() -> None:
    results = inspect_engine_wiring(_FakeEngine(), mode="testnet")
    graph = next(r for r in results if r.check_id == "runtime.algorithms.alpha_graph")
    assert graph.status != CheckStatus.FAIL, graph.message
    factor = next(r for r in results if r.check_id == "runtime.algorithms.factor_lifecycle")
    assert factor.status != CheckStatus.FAIL, factor.message


def test_factor_lifecycle_message_distinguishes_idea_from_degraded() -> None:
    engine = _FakeEngine()
    engine._factor_registry._factors["meanrev_entry_v1"].lifecycle.value = "IDEA"

    results = inspect_engine_wiring(engine, mode="testnet")
    factor = next(r for r in results if r.check_id == "runtime.algorithms.factor_lifecycle")

    assert factor.status == CheckStatus.WARN
    assert "未进入可执行生命周期" in factor.message
    assert "部分因子降级(DEGRADED)" not in factor.message
