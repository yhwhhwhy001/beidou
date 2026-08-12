from __future__ import annotations

import math

import pytest

from beidou_research.mining.generators.bayesian_search import (
    BayesianConfig,
    BayesianParameterSearch,
    ParameterSpace,
)
from beidou_research.mining.generators.interaction import (
    InteractionConfig,
    InteractionGenerator,
)
from beidou_research.mining.generators.regime_conditioned import (
    RegimeConditionedGenerator,
    RegimeConfig,
    RegimeType,
)
from beidou_research.mining.generators.residual import ResidualConfig, ResidualGenerator
from beidou_research.mining.generators.symbolic_gp import GPConfig, SymbolicGPGenerator


def test_symbolic_gp_runs_with_unique_bounded_offspring() -> None:
    generator = SymbolicGPGenerator(
        GPConfig(
            population_size=8,
            generations=2,
            elitism_count=1,
            max_search_budget=30,
            random_seed=7,
        )
    )
    progress: list[tuple[int, int]] = []

    result = generator.run(
        ["close", "volume"],
        lambda expression_hash: {
            "sharpe": float(int(expression_hash[:2], 16)),
            "pareto_rank": 0.0,
        },
        progress_callback=lambda current, total: progress.append((current, total)),
    )

    assert len(result) == 8
    assert len({individual.expression_hash for individual in result}) == 8
    assert all(individual.node_count <= 30 for individual in result)
    assert progress == [(1, 2), (2, 2)]
    assert generator.get_search_statistics()["seen_hashes"] <= 30


def test_symbolic_gp_rejects_invalid_initialization_and_marks_evaluation_error() -> None:
    generator = SymbolicGPGenerator(GPConfig(population_size=2, max_search_budget=2))
    with pytest.raises(ValueError, match="primitives"):
        generator.initialize_population([])

    population = generator.initialize_population(["close"])
    evaluated = generator.evaluate_fitness(
        population,
        lambda _expression_hash: (_ for _ in ()).throw(RuntimeError("bad evaluator")),
    )
    assert all(individual.fitness == {"sharpe": -999.0, "icir": -999.0} for individual in evaluated)


def test_bayesian_search_samples_all_types_and_resets_between_runs() -> None:
    search = BayesianParameterSearch(
        BayesianConfig(n_trials=4, n_inner_folds=3, early_stopping_rounds=0, random_seed=11)
    )
    spaces = [
        ParameterSpace("window", "int", low=2, high=8),
        ParameterSpace("threshold", "float", low=0.001, high=0.1, log_scale=True),
        ParameterSpace("mode", "categorical", choices=["fast", "slow"]),
    ]

    first = search.search(spaces, lambda params: {"metric": 1.0 + params["_fold"] * 0.1})
    second = search.search(spaces, lambda params: {"metric": 2.0})

    assert first.n_trials_completed == 4
    assert second.n_trials_completed == 4
    assert len(second.trials) == 4
    assert 2 <= second.best_params["window"] <= 8
    assert 0.001 <= second.best_params["threshold"] <= 0.1
    assert second.best_params["mode"] in {"fast", "slow"}
    assert second.outer_test_warning.startswith("OUTER_TEST_SET_NOT_EVALUATED")


def test_bayesian_search_rejects_invalid_spaces_and_all_failed_trials() -> None:
    search = BayesianParameterSearch(BayesianConfig(n_trials=2, n_inner_folds=1))
    with pytest.raises(ValueError, match="log-scale"):
        search.sample_parameters(
            [ParameterSpace("bad", "float", low=0.0, high=1.0, log_scale=True)],
            1,
        )

    result = search.search(
        [ParameterSpace("window", "int", low=2, high=3)],
        lambda _params: (_ for _ in ()).throw(RuntimeError("evaluation failed")),
    )
    assert result.n_trials_completed == 0
    assert result.best_params == {}


def test_residual_generator_removes_linear_control_and_checks_variance() -> None:
    generator = ResidualGenerator(ResidualConfig(min_effective_samples=10))
    controls = [float(index) for index in range(20)]
    factor = [3.0 + 2.0 * value for value in controls]

    residual = generator.compute_residual_values(factor, {"market_beta": controls})

    assert max(abs(value) for value in residual) < 1e-10
    assert generator.check_independence(factor, residual) == (False, 0.0)
    specs = generator.generate_residuals(
        [{"factor_id": "momentum", "complexity_score": 2.0}],
        {"market_beta": controls, "liquidity": controls},
    )
    assert {spec.residual_id for spec in specs} == {
        "momentum_residual_market_beta",
        "momentum_residual_liquidity",
    }
    assert all(spec.complexity_score == 4.0 for spec in specs)


def test_residual_generator_preserves_short_or_uncontrolled_series() -> None:
    generator = ResidualGenerator(ResidualConfig(min_effective_samples=5))
    assert generator.compute_residual_values([1.0, 2.0], {"x": [1.0, 2.0]}) == [1.0, 2.0]
    values = [float(index) for index in range(5)]
    assert generator.compute_residual_values(values, {"short": [1.0]}) == values
    assert generator.check_independence(values[:3], values[:3]) == (False, 1.0)


def test_regime_generator_validates_labels_and_masks_non_target_values() -> None:
    generator = RegimeConditionedGenerator(RegimeConfig(regimes=[RegimeType.TRENDING]))
    specs = generator.generate_regime_variants([{"factor_id": "momentum", "complexity_score": 1.5}])
    assert len(specs) == 1
    assert specs[0].regime is RegimeType.TRENDING
    assert specs[0].expected_behavior == "趋势市中动量效应最显著"
    assert specs[0].failure_regime == "ranging"

    masked = generator.filter_by_regime(
        [1.0, 2.0, 3.0],
        ["trending", "ranging", "trending"],
        "trending",
    )
    assert masked[0] == 1.0
    assert math.isnan(masked[1])
    assert masked[2] == 3.0
    assert generator.check_lookahead_bias([], "") == (False, "regime_model_not_frozen")
    assert generator.check_lookahead_bias([], "2026-01-01T00:00:00Z") == (True, "ok")
    with pytest.raises(ValueError, match="unsupported regimes"):
        generator.generate_regime_variants([], ["future_best_regime"])


def test_interaction_generator_honors_order_and_known_rationale() -> None:
    disabled = InteractionGenerator(InteractionConfig(max_order=1))
    assert disabled.generate_interactions(["momentum", "liquidity"]) == []

    generator = InteractionGenerator(InteractionConfig(max_order=3))
    specs = generator.generate_interactions(["liquidity", "momentum", "volatility"])

    assert len(specs) == 4
    assert len({spec.canonical_hash() for spec in specs}) == 4
    momentum_liquidity = next(spec for spec in specs if set(spec.factors) == {"momentum", "liquidity"})
    assert momentum_liquidity.economic_rationale == "趋势在高流动性环境下更可靠"
    assert momentum_liquidity.complexity_score == 4.0
    assert any(spec.order == 3 and spec.complexity_score == 6.0 for spec in specs)
