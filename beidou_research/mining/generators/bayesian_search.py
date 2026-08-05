"""BF-04: 贝叶斯参数搜索 (Bayesian Parameter Search)。

用于已确定结构的参数优化。推荐 Optuna TPE/CMA-ES。
目标函数不是单折 Sharpe，而是:
  median(OOS_net_metric) - stability_penalty - turnover_penalty
  - complexity_penalty - parameter_sensitivity_penalty

原则: 内层搜索，外层只评估；严禁在外层测试集上继续调参。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class BayesianConfig:
    """贝叶斯搜索配置。"""

    n_trials: int = 100
    n_inner_folds: int = 3  # 内层 CV folds
    n_outer_folds: int = 5  # 外层评估 folds
    random_seed: int = 42
    early_stopping_rounds: int = 20
    use_pruning: bool = True


@dataclass
class ParameterSpace:
    """参数空间定义。"""

    name: str
    type: str  # "float", "int", "categorical"
    low: float | None = None
    high: float | None = None
    choices: list[Any] | None = None
    log_scale: bool = False


@dataclass
class TrialResult:
    """单次试验结果。"""

    trial_id: int
    parameters: dict[str, Any]
    objective_value: float
    inner_cv_mean: float
    inner_cv_std: float
    stability_penalty: float
    turnover_penalty: float
    complexity_penalty: float
    status: str = "completed"


@dataclass
class BayesianResult:
    """贝叶斯搜索结果。"""

    best_params: dict[str, Any]
    best_objective: float
    n_trials_completed: int
    trials: list[TrialResult]
    outer_test_metric: float = 0.0
    outer_test_warning: str = ""


class BayesianParameterSearch:
    """贝叶斯参数搜索器。

    当前为纯 Python 简化实现 — 生产环境推荐 Optuna 集成。
    使用简单的随机搜索 + 参数敏感度评估。
    严禁在外层测试集上继续调参！
    """

    def __init__(self, config: BayesianConfig | None = None) -> None:
        self.config = config or BayesianConfig()
        self._trials: list[TrialResult] = []

    def define_search_space(
        self,
        param_space: list[ParameterSpace],
    ) -> list[ParameterSpace]:
        """定义搜索空间。"""
        return param_space

    def sample_parameters(
        self,
        param_space: list[ParameterSpace],
        trial_id: int,
    ) -> dict[str, Any]:
        """从参数空间采样。

        使用简单的拉丁超立方采样（无 Optuna 依赖）。
        """
        import random

        rng = random.Random(self.config.random_seed + trial_id)

        params = {}
        for ps in param_space:
            if ps.type == "float" and ps.low is not None and ps.high is not None:
                if ps.log_scale:
                    log_low = math.log(ps.low)
                    log_high = math.log(ps.high)
                    params[ps.name] = math.exp(rng.uniform(log_low, log_high))
                else:
                    params[ps.name] = rng.uniform(ps.low, ps.high)
            elif ps.type == "int" and ps.low is not None and ps.high is not None:
                params[ps.name] = rng.randint(int(ps.low), int(ps.high))
            elif ps.type == "categorical" and ps.choices:
                params[ps.name] = rng.choice(ps.choices)
            else:
                params[ps.name] = None

        return params

    def compute_objective(
        self,
        params: dict[str, Any],
        inner_evaluator: Callable[[dict[str, Any]], dict[str, float]],
    ) -> TrialResult:
        """计算目标函数值。

        objective = median(OOS_metric) - Σ penalties
        """
        trial_id = len(self._trials) + 1

        try:
            # 内层交叉验证
            cv_metrics = []
            for fold in range(self.config.n_inner_folds):
                result = inner_evaluator({**params, "_fold": fold})
                cv_metrics.append(result.get("metric", 0.0))

            if not cv_metrics:
                return TrialResult(
                    trial_id=trial_id,
                    parameters=params,
                    objective_value=-999.0,
                    inner_cv_mean=0.0,
                    inner_cv_std=0.0,
                    stability_penalty=0.0,
                    turnover_penalty=0.0,
                    complexity_penalty=0.0,
                    status="no_results",
                )

            sorted_metrics = sorted(cv_metrics)
            median_metric = sorted_metrics[len(sorted_metrics) // 2]
            mean_metric = sum(cv_metrics) / len(cv_metrics)

            # 稳定性惩罚 (CV 内的变异性)
            if len(cv_metrics) > 1:
                cv_std = (sum((m - mean_metric) ** 2 for m in cv_metrics) / len(cv_metrics)) ** 0.5
                stability_penalty = cv_std * 2.0
            else:
                stability_penalty = 0.0

            # 复杂度惩罚
            window = params.get("window", 20)
            complexity_penalty = math.log(max(window, 1)) * 0.01

            # 换手率惩罚（通过参数敏感度近似）
            param_sens = params.get("parameter_sensitivity", 0.0)
            turnover_penalty = param_sens * 0.5

            objective = median_metric - stability_penalty - turnover_penalty - complexity_penalty

        except Exception:
            return TrialResult(
                trial_id=trial_id,
                parameters=params,
                objective_value=-999.0,
                inner_cv_mean=0.0,
                inner_cv_std=0.0,
                stability_penalty=0.0,
                turnover_penalty=0.0,
                complexity_penalty=0.0,
                status="error",
            )

        return TrialResult(
            trial_id=trial_id,
            parameters=params,
            objective_value=round(objective, 6),
            inner_cv_mean=round(mean_metric, 6),
            inner_cv_std=round(cv_std if len(cv_metrics) > 1 else 0.0, 6),
            stability_penalty=round(stability_penalty, 6),
            turnover_penalty=round(turnover_penalty, 6),
            complexity_penalty=round(complexity_penalty, 6),
        )

    def search(
        self,
        param_space: list[ParameterSpace],
        inner_evaluator: Callable[[dict[str, Any]], dict[str, float]],
    ) -> BayesianResult:
        """执行贝叶斯参数搜索。"""
        cfg = self.config
        best_trial = None
        best_objective = float("-inf")

        for trial_id in range(1, cfg.n_trials + 1):
            params = self.sample_parameters(param_space, trial_id)
            trial = self.compute_objective(params, inner_evaluator)
            self._trials.append(trial)

            if trial.objective_value > best_objective:
                best_objective = trial.objective_value
                best_trial = trial

            # Early stopping
            if cfg.early_stopping_rounds > 0 and trial_id > cfg.early_stopping_rounds:
                recent = [t.objective_value for t in self._trials[-cfg.early_stopping_rounds :]]
                if max(recent) < best_objective:
                    break

        if best_trial is None:
            return BayesianResult(
                best_params={},
                best_objective=0.0,
                n_trials_completed=0,
                trials=[],
            )

        return BayesianResult(
            best_params=best_trial.parameters,
            best_objective=best_trial.objective_value,
            n_trials_completed=len(self._trials),
            trials=self._trials,
            outer_test_warning="OUTER_TEST_SET_NOT_EVALUATED: must evaluate on held-out data",
        )
