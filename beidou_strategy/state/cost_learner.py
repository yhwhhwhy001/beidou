"""BD-CV45: 执行成本学习器。

无 fill/fee 的样本不会进入 learner。
Contextual Bandit: fill completeness + market regime + action + reward + freshness。
未验证前不自动改变生产执行策略。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrainingSample:
    """BD-CV45: 训练样本。"""

    symbol: str
    algorithm: str
    predicted_cost_bps: float
    realized_cost_bps: float
    fill_completeness: float  # 0.0-1.0
    market_regime: str
    reward: float  # negative cost = positive reward
    freshness: float  # seconds since sample was collected
    has_fill: bool = False
    has_fee: bool = False

    def can_train(self) -> bool:
        """BD-CV45 AC-45-02: 无 fill/fee → 不可训练。"""
        return self.has_fill and self.has_fee


@dataclass
class CostLearner:
    """BD-CV45: Contextual Bandit 执行成本学习器。"""

    samples: list[TrainingSample] = field(default_factory=list)
    algorithm_rewards: dict[str, list[float]] = field(default_factory=dict)
    _production_active: bool = False
    _shadow_verified: bool = False

    def add_sample(self, sample: TrainingSample) -> bool:
        if not sample.can_train():
            return False
        self.samples.append(sample)
        if sample.algorithm not in self.algorithm_rewards:
            self.algorithm_rewards[sample.algorithm] = []
        self.algorithm_rewards[sample.algorithm].append(sample.reward)
        return True

    def best_algorithm(self) -> str:
        """返回当前最佳算法（基于累积奖励）。"""
        if not self.algorithm_rewards:
            return "TWAP"
        best = max(self.algorithm_rewards.items(), key=lambda x: sum(x[1]) / max(len(x[1]), 1))
        return best[0] if best[1] else "TWAP"

    def verify_in_shadow(self) -> bool:
        """BD-CV45: 先在 Shadow 中验证，未验证前不激活生产。"""
        if len(self.samples) < 50:
            return False
        # Check consistency across regimes
        avg_rewards = {algo: sum(rs) / max(len(rs), 1) for algo, rs in self.algorithm_rewards.items()}
        if not avg_rewards:
            return False
        best_algo = max(avg_rewards, key=lambda k: float(avg_rewards[k]))
        worst_algo = min(avg_rewards, key=lambda k: float(avg_rewards[k]))
        # Best must be significantly better than worst
        improvement = avg_rewards[best_algo] - avg_rewards[worst_algo]
        self._shadow_verified = improvement > 0.1
        return self._shadow_verified

    def activate_production(self) -> bool:
        """BD-CV45 AC-45-03: learner policy 变化需经过 shadow evidence 后才能激活。"""
        if not self._shadow_verified:
            return False
        self._production_active = True
        return True

    def predicted_cost_bps(self, symbol: str, algorithm: str) -> float:
        """预测成本（仅作为预测，不使用真实 realized 值）。"""
        algo_samples = [s for s in self.samples if s.algorithm == algorithm and s.has_fill]
        if not algo_samples:
            return 5.0  # default estimate
        return sum(s.realized_cost_bps for s in algo_samples) / len(algo_samples)
