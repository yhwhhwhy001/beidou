"""Finite foreground orchestration for a bounded Testnet execution probe."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from apps.testnet_verify.runtime import VerificationRuntime

from .config import SoakConfig
from .kernel import ExecutionProbeKernel


class RuntimeFactory(Protocol):
    def __call__(self, config: SoakConfig, episode_number: int) -> VerificationRuntime: ...


@dataclass(slots=True)
class CampaignSummary:
    campaign_id: str
    status: str
    stop_reason: str
    elapsed_seconds: float
    episodes: list[dict[str, Any]] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    manifest_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "elapsed_seconds": self.elapsed_seconds,
            "episodes": self.episodes,
            "trace_ids": self.trace_ids,
            "manifest_path": self.manifest_path,
        }


def _default_runtime_factory(config: SoakConfig, episode_number: int) -> VerificationRuntime:
    return VerificationRuntime(config.verifier_config(), kernel=ExecutionProbeKernel(episode_number))


class SoakCampaignRunner:
    """Run a fixed number of episodes and stop on the first uncertain fact."""

    def __init__(
        self,
        config: SoakConfig,
        *,
        runtime_factory: RuntimeFactory = _default_runtime_factory,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
    ) -> None:
        config.validate()
        self.config = config
        self._runtime_factory = runtime_factory
        self._clock = clock
        self._sleep = sleep

    def _engage_durable_kill_switch(self) -> None:
        path = self.config.kill_switch_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write("engaged\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    async def _bounded_sleep(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _episode_payload(number: int, summary: Any, reconciliation: dict[str, Any]) -> dict[str, Any]:
        return {
            "episode_number": number,
            "namespace": f"execution-probe-{number:02d}",
            "run_id": str(summary.run_id),
            "status": str(summary.status),
            "episodes": list(summary.episodes),
            "trace_ids": [str(value) for value in summary.trace_ids],
            "verifier_manifest_path": str(summary.manifest_path),
            "reconciliation": reconciliation,
            "evidence_class": "EXECUTION_PROBE",
            "alpha_evidence": False,
        }

    @staticmethod
    def _all_episode_facts_closed(summary: Any) -> bool:
        episode_facts = getattr(summary, "episodes", None)
        if not isinstance(episode_facts, list) or not episode_facts:
            return False
        return all(isinstance(item, dict) and str(item.get("status", "")).upper() == "CLOSED" for item in episode_facts)

    def _write_manifest(self, summary: CampaignSummary) -> Path:
        destination = self.config.evidence_dir / summary.campaign_id
        destination.mkdir(parents=True, exist_ok=True)
        real_write = False
        real_write_attempted = False
        real_write_unknown = False
        for episode in summary.episodes:
            path = Path(str(episode.get("verifier_manifest_path", "")))
            if not path.is_file():
                continue
            try:
                verifier_manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            real_write = real_write or verifier_manifest.get("real_testnet_write") is True
            real_write_attempted = real_write_attempted or verifier_manifest.get("real_testnet_write_attempted") is True
            real_write_unknown = (
                real_write_unknown or verifier_manifest.get("real_testnet_write_outcome_unknown") is True
            )
        manifest = {
            **summary.to_dict(),
            "config": self.config.redacted_dict(),
            "config_hash": self.config.config_hash(),
            "execution_mode": "EXECUTION_PROBE",
            "alpha_evidence": False,
            "economic_truth_e0_e6": "NOT_EVALUATED",
            "real_testnet_write": real_write,
            "real_testnet_write_attempted": real_write_attempted,
            "real_testnet_write_outcome_unknown": real_write_unknown,
            "kill_switch_engaged": self.config.kill_switch_path.exists(),
            "completed_episode_count": sum(1 for item in summary.episodes if item["status"] == "EPISODE_COMPLETED"),
        }
        path = destination / "campaign-manifest.json"
        temporary = destination / "campaign-manifest.json.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        return path

    async def run(self) -> CampaignSummary:
        started = self._clock()
        campaign_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + self.config.config_hash()[:12]
        episodes: list[dict[str, Any]] = []
        trace_ids: list[str] = []
        seen_trace_ids: set[str] = set()
        status = "STOPPED"
        stop_reason = "CAMPAIGN_NOT_STARTED"
        cancelled: asyncio.CancelledError | None = None
        try:
            for number in range(1, self.config.episodes + 1):
                elapsed = self._clock() - started
                if elapsed >= self.config.max_duration_seconds:
                    stop_reason = "TIME_BUDGET_EXHAUSTED"
                    break
                runtime = self._runtime_factory(self.config, number)
                namespace = f"execution-probe-{number:02d}"
                verifier_summary = await runtime.run_once(episode_identity_namespace=namespace)
                current_trace_ids = [str(value) for value in verifier_summary.trace_ids]
                if seen_trace_ids.intersection(current_trace_ids) or len(current_trace_ids) != len(
                    set(current_trace_ids)
                ):
                    reconciliation = {"status": "DUPLICATE_TRACE_IDENTITY"}
                    episodes.append(self._episode_payload(number, verifier_summary, reconciliation))
                    stop_reason = "DUPLICATE_TRACE_IDENTITY"
                    break
                seen_trace_ids.update(current_trace_ids)
                trace_ids.extend(current_trace_ids)
                if verifier_summary.status != "EPISODE_COMPLETED" or not self._all_episode_facts_closed(
                    verifier_summary
                ):
                    self._engage_durable_kill_switch()
                    reconciliation = await runtime.reconcile_flat_account()
                    episodes.append(self._episode_payload(number, verifier_summary, reconciliation))
                    stop_reason = "EPISODE_NOT_COMPLETED"
                    break
                reconciliation = await runtime.reconcile_flat_account()
                episodes.append(self._episode_payload(number, verifier_summary, reconciliation))
                if reconciliation.get("status") != "RECONCILED_FLAT":
                    self._engage_durable_kill_switch()
                    stop_reason = "POST_EPISODE_RECONCILIATION_FAILED"
                    break
                if number == self.config.episodes:
                    status = "COMPLETED"
                    stop_reason = "ALL_EPISODES_RECONCILED_FLAT"
                    break
                next_episode_start = started + number * self.config.cycle_interval_seconds
                campaign_deadline = started + self.config.max_duration_seconds
                if next_episode_start >= campaign_deadline:
                    stop_reason = "TIME_BUDGET_EXHAUSTED"
                    break
                sleep_seconds = max(0.0, next_episode_start - self._clock())
                if sleep_seconds > 0:
                    await self._bounded_sleep(sleep_seconds)
        except asyncio.CancelledError as exc:
            stop_reason = "CAMPAIGN_CANCELLED"
            cancelled = exc
        except Exception as exc:  # pragma: no cover - exact branches are exercised via fault tests
            stop_reason = f"UNEXPECTED_EXCEPTION:{type(exc).__name__}"
        finally:
            self._engage_durable_kill_switch()

        summary = CampaignSummary(
            campaign_id=campaign_id,
            status=status,
            stop_reason=stop_reason,
            elapsed_seconds=max(0.0, self._clock() - started),
            episodes=episodes,
            trace_ids=trace_ids,
        )
        summary.manifest_path = str(self.config.evidence_dir / campaign_id / "campaign-manifest.json")
        summary.manifest_path = str(self._write_manifest(summary))
        if cancelled is not None:
            raise cancelled
        return summary


def build_runner(config: SoakConfig) -> SoakCampaignRunner:
    return SoakCampaignRunner(config)
