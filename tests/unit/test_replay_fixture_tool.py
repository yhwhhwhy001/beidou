"""BD-T04 replay 工具与 normalizer 演进语义测试（M01-F05，P1-08）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_data.market import BarIntegrity, ClosedBarNormalizer
from beidou_shared.types import InstrumentId, VenueId, VenueInstrument

_VI = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _raw(open_hour: int, *, closed: bool | None = None) -> dict:
    open_time = datetime(2026, 1, 1, open_hour, 0, tzinfo=timezone.utc)
    raw: dict = {
        "open_time": open_time.isoformat(),
        "close_time": (open_time + timedelta(hours=1)).isoformat(),
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "10",
    }
    if closed is not None:
        raw["is_closed"] = closed
    return raw


def test_unclosed_then_closed_is_normal_evolution_not_duplicate() -> None:
    normalizer = ClosedBarNormalizer()
    first = normalizer.normalize(_raw(10, closed=False), _VI, "1h")
    assert first.status is BarIntegrity.NOT_CLOSED
    second = normalizer.normalize(_raw(10, closed=True), _VI, "1h")
    assert second.status is BarIntegrity.OK, f"闭合演进被误判: {second.status}"
    third = normalizer.normalize(_raw(10, closed=True), _VI, "1h")
    assert third.status is BarIntegrity.DUPLICATE


def test_closed_bar_duplicate_still_detected() -> None:
    normalizer = ClosedBarNormalizer()
    assert normalizer.normalize(_raw(10, closed=True), _VI, "1h").status is BarIntegrity.OK
    assert normalizer.normalize(_raw(10, closed=True), _VI, "1h").status is BarIntegrity.DUPLICATE


def test_replay_tool_is_deterministic() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    # 绝对路径锚定 __file__ —— 其他测试会 chdir（已知 CWD 污染,M21 修复），
    # 相对路径在组合运行时不可靠。
    repo_root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "run_replay_fixture", repo_root / "scripts" / "run_replay_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_replay_fixture"] = module
    spec.loader.exec_module(module)

    raw_klines = [
        {
            "open_time": datetime(2026, 1, 1, h, 0, tzinfo=timezone.utc).isoformat(),
            "close_time": datetime(2026, 1, 1, h + 1, 0, tzinfo=timezone.utc).isoformat(),
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100.5",
            "volume": "10",
            "is_closed": True,
        }
        for h in range(5)
    ]
    results1, features1, rh1, fh1 = module.replay(raw_klines, "BTCUSDT")
    _results2, _features2, rh2, fh2 = module.replay(raw_klines, "BTCUSDT")
    assert rh1 == rh2
    assert fh1 == fh2
    assert len(results1) == 5
    assert all(r.status is BarIntegrity.OK for r in results1)
    assert len(features1) == 5
    for fv in features1:
        assert fv.input_hash
        assert fv.feature_version == "v1"
        assert fv.data_quality_tier in ("PASS", "CONDITIONAL")
