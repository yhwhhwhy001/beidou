"""mainnet 的 profile 与 registry 只在该不同的地方与 demo 不同（切换方案 M2，2026-10-19）。

L-B 是 demo 照跑、旁边多一个小额 mainnet 循环，两者差的应该只有账户：主机、凭据的变量名、保证金模式、状态目录，
外加操作者 07:26:40Z 在卡片上定的 k = 0.50 和随 k 走的档位。差别多一个键，mainnet 的读数就不再能跟 demo 比，
而这个比较是校准循环存在的理由。所以这里逐键钉住差别的清单，构造上只许 `vol_target` 不同。

demo 的东西一个字节不动，这也在这里核：demo 的 R8 档位还是 `Policy` 的，`policy_digest` 不动，60 天冻结不受影响
（操作者卡片「不算，现在做」）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from beidou_alpha.registry import Registry, parse_registry
from beidou_exchange.guard import MAINNET_HOSTS
from beidou_governance.policy import VENUE_DRAWDOWN_LADDERS, Policy, venue_drawdown_ladder
from beidou_live.config import account_gate, live_config, registry_evidence_problems
from beidou_live.engine import LiveConfig, construction_fingerprint, evidence_construction
from beidou_live.risk_budget import RiskBudgetParams

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "config" / "live.demo.yaml"
MAINNET = ROOT / "config" / "live.mainnet.yaml"

#: 两份 profile 不同的全部键，和 mainnet 的值。改这张表就是改 mainnet 与 demo 的关系，要在 PR 里说为什么。
MAINNET_ONLY: dict[str, Any] = {
    "profile": "mainnet",
    "venue.rest_url": "https://fapi.binance.com",
    "venue.api_key_env": "BEIDOU_MAINNET_API_KEY",
    "venue.api_secret_env": "BEIDOU_MAINNET_API_SECRET",
    "venue.multi_assets_margin": False,
    "registry": "config/alpha_registry.mainnet.yaml",
    "portfolio.vol_target": 0.50,
    "risk_budget.deescalate_at": 0.49,
    "risk_budget.rollback_at": 0.70,
    "risk_budget.deescalate_to": 0.375,
    "risk_budget.rollback_to": 0.25,
    "risk_budget.vol_band": [0.4333333333, 0.6333333333],
    "risk_budget.usdt_baseline": None,
    "guards.kill_switch_path": ".beidou/live-mainnet/KILL_SWITCH",
    "paths.state_dir": ".beidou/live-mainnet",
    "paths.reports_dir": ".beidou/reports-mainnet",
}


def _profile(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload


def _flat(path: Path) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for name, value in _profile(path).items():
        if isinstance(value, dict):
            flat |= {f"{name}.{leaf}": leaf_value for leaf, leaf_value in value.items()}
        else:
            flat[name] = value
    return flat


def _registry(path: str) -> Registry:
    return parse_registry(yaml.safe_load((ROOT / path).read_text(encoding="utf-8")))


def _config(path: Path) -> LiveConfig:
    profile = _profile(path)
    return live_config(profile, ["BTCUSDT"], _registry(str(profile["registry"])), dry_run=True)


def _differences(left: Any, right: Any, at: str = "") -> set[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        return set().union(
            *(
                _differences(left.get(key), right.get(key), f"{at}.{key}".lstrip("."))
                for key in left.keys() | right.keys()
            )
        )
    return set() if left == right else {at}


def test_the_profiles_differ_in_exactly_these_keys() -> None:
    demo, mainnet = _flat(DEMO), _flat(MAINNET)
    assert demo.keys() == mainnet.keys()
    assert {key for key in demo if demo[key] != mainnet[key]} == set(MAINNET_ONLY)
    assert {key: mainnet[key] for key in MAINNET_ONLY} == MAINNET_ONLY


def test_the_construction_differs_only_in_the_vol_target() -> None:
    demo, mainnet = _config(DEMO), _config(MAINNET)
    assert _differences(evidence_construction(demo), evidence_construction(mainnet)) == {"portfolio.vol_target"}
    fingerprints = [
        {key: value for key, value in construction_fingerprint(config).items() if key != "digest"}
        for config in (demo, mainnet)
    ]
    assert _differences(*fingerprints) == {"portfolio.vol_target"}
    assert demo.account_gate is None and mainnet.account_gate is not None


def test_both_registries_run_the_same_signals() -> None:
    """只许 `evidence` 与 `probe` 不同：那两样描述 k，而信号不随 k 变。"""
    demo, mainnet = _registry("config/alpha_registry.yaml"), _registry("config/alpha_registry.mainnet.yaml")

    def signals(registry: Registry) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in dataclasses.asdict(entry).items() if key not in ("evidence", "probe")}
            for entry in registry.enabled
        ]

    assert signals(demo) == signals(mainnet)
    assert (demo.ensemble_method, demo.turnover_penalty, demo.universe) == (
        mainnet.ensemble_method,
        mainnet.turnover_penalty,
        mainnet.universe,
    )


def test_every_mainnet_host_has_rungs_derived_for_the_profiles_k() -> None:
    assert set(VENUE_DRAWDOWN_LADDERS) == set(MAINNET_HOSTS)
    profile = _profile(MAINNET)
    gate = account_gate(profile)
    assert gate is not None
    rungs = dict(venue_drawdown_ladder(gate.host, float(profile["portfolio"]["vol_target"])))
    # the reporting copy and the acting copy, joined the way demo's are (`Policy.drawdown_ladder` <-> live.demo.yaml)
    block = RiskBudgetParams.from_mapping(profile["risk_budget"])
    assert rungs[-block.deescalate_at] == pytest.approx(block.deescalate_to)
    assert rungs[-block.rollback_at] == pytest.approx(block.rollback_to)
    # the rule: 70% of the -70% budget and the budget itself, on an all-USDT account (factor 1.0)
    assert sorted(rungs) == [-0.70, -0.49]


def test_the_band_moves_with_the_mainnet_vol_target() -> None:
    profile = _profile(MAINNET)
    target = float(profile["portfolio"]["vol_target"])
    low, high = RiskBudgetParams.from_mapping(profile["risk_budget"]).vol_band
    assert abs(low / target - 0.26 / 0.30) < 1e-9
    assert abs(high / target - 0.38 / 0.30) < 1e-9


def test_a_ladder_is_refused_for_an_undeclared_host_or_another_k() -> None:
    with pytest.raises(RuntimeError, match=r"no R8 ladder is declared for demo-fapi\.binance\.com"):
        venue_drawdown_ladder("demo-fapi.binance.com", 0.175)
    with pytest.raises(RuntimeError, match="re-derive both together"):
        venue_drawdown_ladder("fapi.binance.com", 0.55)


def test_demo_keeps_the_policy_ladder() -> None:
    assert Policy().drawdown_ladder == ((-0.2803, 0.13125), (-0.4005, 0.0875))
    assert account_gate(_profile(DEMO)) is None


def test_mainnet_cannot_start_on_evidence_measured_at_another_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """今天这条就是那道拒绝：证据是 demo 在 0.175 上的。按 0.50 重出之后，止损也必须在 0.50 上重量。"""
    monkeypatch.chdir(ROOT)
    profile = _profile(MAINNET)
    registry = _registry(str(profile["registry"]))
    problems = registry_evidence_problems(registry, profile)
    if problems:
        assert "tsmom: portfolio vol_target is 0.5 live but 0.175 in the cited evidence" in problems
        return
    for entry in registry.enabled:
        measured = ((entry.probe or {}).get("stop") or {}).get("measured_at") or {}
        assert measured.get("vol_target") == profile["portfolio"]["vol_target"], (
            f"{entry.id}: the evidence is now at k={profile['portfolio']['vol_target']}, but the probe stop was "
            f"measured at {measured.get('vol_target')}; re-measure it in the same change"
        )
