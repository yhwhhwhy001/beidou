"""Deterministic execution probe; this is deliberately not an Alpha model."""

from __future__ import annotations

from typing import Any

from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha.contracts import EntryProposal, StrategyProposal
from beidou_strategy.kernel_parity import StrategyKernel, StrategyKernelContract


class ExecutionProbeKernel(StrategyKernel):
    """Generate a labelled alternating proposal solely to exercise execution."""

    def __init__(self, episode_number: int) -> None:
        if type(episode_number) is not int or episode_number < 1:
            raise ValueError("episode_number must be a positive integer")
        super().__init__(mode="TESTNET")
        self.episode_number = episode_number

    def active_component_manifest(self) -> list[dict[str, str]]:
        return [
            {
                "component_id": "execution-probe-direction-v1",
                "component_type": "EXECUTION_PROBE",
                "version": "execution-probe-v1",
                "graph_hash": "NOT_ALPHA_GRAPH",
            }
        ]

    async def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        instrument = InstrumentId(str(context.get("instrument_id", "")))
        side = OrderSide.BUY if self.episode_number % 2 else OrderSide.SELL
        entry = EntryProposal(
            strategy_id=StrategyId("execution-probe-v1"),
            instrument_id=instrument,
            venue_id=VenueId("BINANCE"),
            side=side,
            strength=1.0,
            confidence=1.0,
            model_version=SchemaVersion("execution-probe-v1"),
            metadata={
                "evidence_class": "EXECUTION_PROBE",
                "alpha_evidence": False,
                "episode_number": self.episode_number,
            },
        )
        proposal = StrategyProposal(
            strategy_id=entry.strategy_id,
            instrument_id=entry.instrument_id,
            venue_id=entry.venue_id,
            side=side,
            strength=1.0,
            confidence=1.0,
            entry_proposals=[entry],
            policy_version="execution-probe-v1",
            feature_snapshot_ref=StrategyKernelContract.compute_proposal_hash(context.get("features", {})),
        )
        return {
            "proposal": proposal,
            "component_outputs": {"execution-probe-direction-v1": entry},
            "kernel": "execution_probe",
            "mode": "TESTNET",
            "graph_hash": "NOT_ALPHA_GRAPH",
            "context_hash": StrategyKernelContract.compute_proposal_hash(context),
            "evidence_class": "EXECUTION_PROBE",
            "alpha_evidence": False,
        }
