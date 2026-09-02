"""Offline contracts for the bounded execution-probe soak campaign."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from apps.testnet_soak.cli import main
from apps.testnet_soak.config import SoakConfig
from apps.testnet_soak.kernel import ExecutionProbeKernel
from apps.testnet_soak.runtime import SoakCampaignRunner
from beidou_shared.types import OrderSide


def _config(tmp_path: Path, **overrides: Any) -> SoakConfig:
    values: dict[str, Any] = {
        "api_key": "fixture-key",
        "api_secret": "fixture-secret",
        "account_id": "dedicated-soak-account",
        "confirm_testnet": True,
        "trace_path": tmp_path / "trace.jsonl",
        "pool_state_path": tmp_path / "pool.json",
        "kill_switch_path": tmp_path / "KILL_SWITCH",
        "evidence_dir": tmp_path / "evidence",
    }
    values.update(overrides)
    return SoakConfig(**values)


def test_soak_config_defaults_are_the_authorized_bounds(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.episodes == 30
    assert config.max_duration_seconds == 3600
    assert config.cycle_interval_seconds == 120
    assert config.redacted_dict()["cycle_interval_semantics"] == "START_TO_START"
    assert config.target_notional == 100
    assert config.absolute_notional_ceiling == 500
    assert config.max_leverage == 3
    assert config.symbol == "BTCUSDT"
    verifier = config.verifier_config()
    assert verifier.max_notional == 100
    assert verifier.max_account_exposure == 500
    assert verifier.max_instruments == 1
    assert verifier.close_after_verify is True
    assert verifier.once is True
    assert verifier.allowed_symbols == ("BTCUSDT",)
    assert verifier.task_id == "testnet-soak-campaign"
    assert verifier.entrypoint == "apps.testnet_soak"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("episodes", 0),
        ("episodes", 31),
        ("max_duration_seconds", 0),
        ("max_duration_seconds", 3601),
        ("cycle_interval_seconds", 59),
        ("target_notional", 0),
        ("target_notional", 100.01),
        ("absolute_notional_ceiling", 500.01),
        ("absolute_notional_ceiling", 99),
        ("max_leverage", 3.01),
        ("symbol", "BTC/USDT"),
        ("symbol", "btcusdt"),
    ],
)
def test_soak_config_rejects_any_bound_expansion(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(_config(tmp_path), **{field: value}).validate()


def test_soak_cli_help_is_side_effect_free(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "execution-probe" in result.output
    assert not list(tmp_path.iterdir())


def test_soak_cli_requires_confirmation_before_building_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []
    monkeypatch.setattr("apps.testnet_soak.cli.build_runner", lambda config: built.append(config))

    result = CliRunner().invoke(main, [])

    assert result.exit_code != 0
    assert "--confirm-testnet" in result.output
    assert built == []


@pytest.mark.asyncio
async def test_execution_probe_kernel_is_labelled_and_alternates_direction() -> None:
    first = ExecutionProbeKernel(episode_number=1)
    second = ExecutionProbeKernel(episode_number=2)

    first_result = await first.evaluate({"instrument_id": "BTCUSDT", "features": {"close": 100.0}})
    second_result = await second.evaluate({"instrument_id": "BTCUSDT", "features": {"close": 100.0}})

    assert first_result["proposal"].side is OrderSide.BUY
    assert second_result["proposal"].side is OrderSide.SELL
    assert first_result["kernel"] == "execution_probe"
    assert first_result["evidence_class"] == "EXECUTION_PROBE"
    assert first_result["alpha_evidence"] is False
    assert "alpha" not in first_result["proposal"].policy_version.lower()


class _FakeRuntime:
    def __init__(
        self,
        episode_number: int,
        *,
        fail_at: int | None = None,
        reconcile_status: str = "RECONCILED_FLAT",
        extra_unknown: bool = False,
    ):
        self.episode_number = episode_number
        self.fail_at = fail_at
        self.reconcile_status = reconcile_status
        self.extra_unknown = extra_unknown
        self.namespaces: list[str] = []

    async def run_once(self, *, episode_identity_namespace: str = "") -> Any:
        self.namespaces.append(episode_identity_namespace)
        status = "NOT_VERIFIABLE" if self.episode_number == self.fail_at else "EPISODE_COMPLETED"
        episode_status = "UNKNOWN" if status != "EPISODE_COMPLETED" else "CLOSED"
        episodes = [{"status": episode_status}]
        if self.extra_unknown:
            episodes.append({"status": "UNKNOWN"})
        return SimpleNamespace(
            run_id=f"run-{self.episode_number}",
            status=status,
            episodes=episodes,
            trace_ids=[f"trace-{self.episode_number}", f"trace-{self.episode_number}-c"],
            manifest_path=f"verifier-{self.episode_number}.json",
        )

    async def reconcile_flat_account(self) -> dict[str, Any]:
        return {
            "status": self.reconcile_status,
            "position_mode": "ONE_WAY",
            "nonzero_positions": [],
            "regular_open_order_count": 0,
            "algo_open_order_count": 0,
            "unresolved_trace_count": 0,
        }


@pytest.mark.asyncio
async def test_campaign_completes_30_unique_episodes_and_engages_kill_switch(tmp_path: Path) -> None:
    runtimes: list[_FakeRuntime] = []
    sleeps: list[float] = []
    now = [0.0]

    def factory(_config: SoakConfig, episode_number: int) -> _FakeRuntime:
        runtime = _FakeRuntime(episode_number)
        runtimes.append(runtime)
        return runtime

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    runner = SoakCampaignRunner(_config(tmp_path), runtime_factory=factory, clock=lambda: now[0], sleep=sleep)
    summary = await runner.run()

    assert summary.status == "COMPLETED"
    assert len(summary.episodes) == 30
    assert len(runtimes) == 30
    assert [runtime.namespaces for runtime in runtimes] == [[f"execution-probe-{index:02d}"] for index in range(1, 31)]
    assert sleeps == [120.0] * 29
    assert _config(tmp_path).kill_switch_path.read_text(encoding="utf-8") == "engaged\n"
    assert Path(summary.manifest_path).exists()


@pytest.mark.asyncio
async def test_campaign_uses_fixed_start_to_start_cadence_when_episodes_take_time(tmp_path: Path) -> None:
    """Thirty real cycles must fit because execution time consumes the interval, not extend it."""

    now = [0.0]
    sleeps: list[float] = []

    class TimedRuntime(_FakeRuntime):
        async def run_once(self, *, episode_identity_namespace: str = "") -> Any:
            now[0] += 8.0
            return await super().run_once(episode_identity_namespace=episode_identity_namespace)

    def factory(_config: SoakConfig, episode_number: int) -> TimedRuntime:
        return TimedRuntime(episode_number)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    runner = SoakCampaignRunner(_config(tmp_path), runtime_factory=factory, clock=lambda: now[0], sleep=sleep)
    summary = await runner.run()

    assert summary.status == "COMPLETED"
    assert len(summary.episodes) == 30
    assert sleeps == [112.0] * 29
    assert summary.elapsed_seconds == 3488.0
    assert summary.elapsed_seconds <= _config(tmp_path).max_duration_seconds


@pytest.mark.asyncio
async def test_campaign_stops_immediately_on_unknown_and_does_not_start_next_episode(tmp_path: Path) -> None:
    created: list[int] = []

    def factory(_config: SoakConfig, episode_number: int) -> _FakeRuntime:
        created.append(episode_number)
        return _FakeRuntime(episode_number, fail_at=3)

    async def no_wait(_seconds: float) -> None:
        return None

    runner = SoakCampaignRunner(_config(tmp_path), runtime_factory=factory, sleep=no_wait)
    summary = await runner.run()

    assert summary.status == "STOPPED"
    assert summary.stop_reason == "EPISODE_NOT_COMPLETED"
    assert created == [1, 2, 3]
    assert _config(tmp_path).kill_switch_path.exists()


@pytest.mark.asyncio
async def test_campaign_rejects_completed_summary_containing_any_unknown_fact(tmp_path: Path) -> None:
    created: list[int] = []

    def factory(_config: SoakConfig, episode_number: int) -> _FakeRuntime:
        created.append(episode_number)
        return _FakeRuntime(episode_number, extra_unknown=True)

    runner = SoakCampaignRunner(_config(tmp_path), runtime_factory=factory)
    summary = await runner.run()

    assert summary.status == "STOPPED"
    assert summary.stop_reason == "EPISODE_NOT_COMPLETED"
    assert created == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reconcile_status",
    ["POSITION_MODE_UNKNOWN", "HEDGE_MODE", "RESIDUAL_POSITION", "OPEN_ORDERS", "UNRESOLVED_TRACES"],
)
async def test_campaign_stops_after_any_nonflat_reconciliation(tmp_path: Path, reconcile_status: str) -> None:
    created: list[int] = []

    def factory(_config: SoakConfig, episode_number: int) -> _FakeRuntime:
        created.append(episode_number)
        return _FakeRuntime(episode_number, reconcile_status=reconcile_status)

    runner = SoakCampaignRunner(_config(tmp_path), runtime_factory=factory, sleep=lambda _seconds: None)
    summary = await runner.run()

    assert summary.status == "STOPPED"
    assert summary.stop_reason == "POST_EPISODE_RECONCILIATION_FAILED"
    assert created == [1]


@pytest.mark.asyncio
async def test_campaign_stops_before_sleep_would_exceed_duration(tmp_path: Path) -> None:
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    config = _config(tmp_path, episodes=2, max_duration_seconds=100, cycle_interval_seconds=120)
    runner = SoakCampaignRunner(
        config,
        runtime_factory=lambda _config, episode_number: _FakeRuntime(episode_number),
        clock=lambda: now[0],
        sleep=sleep,
    )
    summary = await runner.run()

    assert summary.status == "STOPPED"
    assert summary.stop_reason == "TIME_BUDGET_EXHAUSTED"
    assert len(summary.episodes) == 1
    assert now[0] == 0
