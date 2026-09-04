"""The evidence gate also compares the portfolio construction, not only the signal parameters."""

from __future__ import annotations

from beidou_alpha.registry import CONSTRUCTION_KEYS, StrategyEntry, construction_problems, evidence_problems

LIVE = {
    "vol_target": 0.15,
    "vol_halflife": 48,
    "covariance_halflife": 96,
    "min_asset_vol": 0.10,
    "max_weight": 0.15,
    "max_gross": 2.0,
    "max_scalar": 3.0,
    "no_trade_band": 0.005,
    "no_trade_rel_band": 0.40,
}
ENTRY = StrategyEntry("tsmom", params={"horizons": [168, 336, 720]})


def test_the_real_divergence_is_caught() -> None:
    """P10 cell B moved the live band to 0.40 while tsmom's evidence had been validated at 0.25."""
    report = {"kind": "validation", "portfolio": {**LIVE, "no_trade_rel_band": 0.25}}
    problems = construction_problems(ENTRY, report, LIVE)
    assert problems == ["tsmom: portfolio no_trade_rel_band is 0.4 live but 0.25 in the cited evidence"]


def test_a_matching_construction_is_silent_and_every_weight_bearing_key_is_covered() -> None:
    assert construction_problems(ENTRY, {"portfolio": dict(LIVE)}, LIVE) == []
    for key in CONSTRUCTION_KEYS:
        drifted = {**LIVE, key: (LIVE[key] * 2 if isinstance(LIVE[key], int | float) else LIVE[key])}
        assert construction_problems(ENTRY, {"portfolio": drifted}, LIVE), f"{key} must be compared"


def test_values_that_crossed_a_yaml_json_boundary_still_compare_equal() -> None:
    report = {"portfolio": {**LIVE, "no_trade_rel_band": 0.4000000000000001, "max_gross": 2}}
    assert construction_problems(ENTRY, report, LIVE) == []


def test_reports_without_the_block_are_skipped_rather_than_refused() -> None:
    """Every report in flight today predates the field; refusing them would stop the loop."""
    assert construction_problems(ENTRY, {"kind": "validation", "best_params": {}}, LIVE) == []
    assert construction_problems(ENTRY, {"portfolio": {}}, LIVE) == []
    assert construction_problems(ENTRY, {"portfolio": dict(LIVE)}, None) == []


def test_it_is_wired_into_the_startup_gate() -> None:
    entry = StrategyEntry(
        "tsmom",
        params={"horizons": [168, 336, 720]},
        evidence={"report": "r.json", "sha256": "a" * 64, "verdict": "PASS"},
    )
    report = {
        "kind": "validation",
        "strategy": "tsmom",
        "best_params": {"horizons": [168, 336, 720]},
        "portfolio": {**LIVE, "no_trade_rel_band": 0.25},
    }
    problems = evidence_problems(
        entry,
        exists=lambda _: True,
        sha256_of=lambda _: "a" * 64,
        read_report=lambda _: report,
        canonical_params=lambda _sid, params: dict(params),
        live_portfolio=LIVE,
    )
    assert any("no_trade_rel_band" in problem for problem in problems)
    # without the live construction the gate behaves exactly as before
    assert (
        evidence_problems(
            entry,
            exists=lambda _: True,
            sha256_of=lambda _: "a" * 64,
            read_report=lambda _: report,
            canonical_params=lambda _sid, params: dict(params),
        )
        == []
    )
