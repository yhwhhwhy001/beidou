"""Factor Miner worker entrypoints.

The module keeps the spawn-safe single-symbol research worker together with
the fail-closed resume resolver.  Both are importable from a stable module
path so the CLI and its tests share the same implementation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from beidou_research.mining.runner import ResumableMiningRunner, ResumeRunError

DEFAULT_RESUME_ROOT = Path(".beidou/factor-miner-runs")


def run_symbol_worker(payload: dict) -> dict:
    """Execute one symbol's complete offline mining flow."""
    sym = payload["symbol"]

    from beidou_core.feed import MarketDataFeed
    from beidou_research.data.dataset_manifest import DatasetManifest
    from beidou_research.data.kline_store import KlineStore, frame_to_price_data
    from beidou_research.mining.runner import (
        MiningRunner,
        PipelineConfig,
        compute_feature_manifest_hash,
    )

    store = KlineStore(root=payload["data_root"])
    manifest_hash = ""
    if payload["from_store"] or store.has_data(sym, payload["interval"], min_rows=100):
        if not store.has_data(sym, payload["interval"], min_rows=100):
            raise RuntimeError(f"SYMBOL_LOCAL_DATA_MISSING:{sym}/{payload['interval']}")
        frame = store.load(sym, payload["interval"])
        price_data = frame_to_price_data(frame)
        manifest = DatasetManifest.read(store.manifest_path(sym, payload["interval"]))
        if manifest is None:
            raise RuntimeError(f"DATASET_NOT_ECONOMIC_RESEARCH_ELIGIBLE:{sym}/{payload['interval']}:MANIFEST_MISSING")
        assessment = DatasetManifest.assess_economic_research(frame, manifest, sym, payload["interval"])
        if not assessment.eligible:
            reasons = ",".join(assessment.reasons)
            raise RuntimeError(f"DATASET_NOT_ECONOMIC_RESEARCH_ELIGIBLE:{sym}/{payload['interval']}:{reasons}")
        manifest_hash = assessment.manifest_hash
        source = "local"
    else:
        feed = MarketDataFeed()
        klines = feed.fetch_klines(sym, interval=payload["interval"], limit=payload["limit"])
        if len(klines) < 100:
            return {"symbol": sym, "skipped": True, "reason": "insufficient_klines"}
        price_data = [
            {
                "timestamp": k["open_time"],
                "close": k["close"],
                "open": k["open"],
                "high": k["high"],
                "low": k["low"],
                "volume": k["volume"],
                "is_closed": k.get("is_closed") is True,
            }
            for k in klines
        ]
        source = "api"

    pipeline_config = PipelineConfig.from_yaml(payload["policy"])
    pipeline_config.evidence_dir = payload["output_dir"]
    pipeline_config.dataset_manifest_hash = manifest_hash
    # The feature schema manifest is bound independently of the dataset payload.
    pipeline_config.feature_manifest_hash = compute_feature_manifest_hash()
    runner = MiningRunner(pipeline_config)

    result = runner.run(
        price_data=price_data,
        venue="BINANCE",
        symbol=sym,
        timeframe=payload["interval"],
    )
    return {
        "symbol": sym,
        "result": result,
        "manifest_hash": manifest_hash,
        "source": source,
        "rows": len(price_data),
        "pid": os.getpid(),
    }


def resume_enabled() -> bool:
    """Rollback switch: only an explicit false value disables resume routing."""

    return os.environ.get("BEIDOU_FACTOR_MINER_RESUME_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def resolve_run_directory(state_root: str | Path, run_id: str) -> Path:
    """Resolve exactly one manifest whose immutable identity matches ``run_id``."""

    requested = run_id.strip()
    if not requested:
        raise ResumeRunError("RUN_ID_REQUIRED")
    root = Path(state_root)
    if not root.is_dir():
        raise ResumeRunError("RESUME_ROOT_MISSING")

    matches: list[Path] = []
    corrupt_named_match = False
    for manifest_path in sorted(root.glob(f"**/{ResumableMiningRunner.manifest_name}")):
        try:
            manifest = json.loads(manifest_path.read_text())
            identity = manifest["identity"]
            if isinstance(identity, dict) and identity.get("run_id") == requested:
                matches.append(manifest_path.parent)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            if manifest_path.parent.name == requested:
                corrupt_named_match = True

    if corrupt_named_match and not matches:
        raise ResumeRunError("CORRUPT_RUN_MANIFEST")
    if not matches:
        raise ResumeRunError("RUN_ID_NOT_FOUND")
    if len(matches) != 1:
        raise ResumeRunError("AMBIGUOUS_RUN_ID")
    return matches[0]


def resume_run(run_id: str, *, state_root: str | Path = DEFAULT_RESUME_ROOT) -> dict:
    """Resume one run; rejected routing can never call the fresh-run API."""

    if not resume_enabled():
        raise ResumeRunError("RESUME_DISABLED")
    run_dir = resolve_run_directory(state_root, run_id)
    return ResumableMiningRunner.resume(run_dir)
