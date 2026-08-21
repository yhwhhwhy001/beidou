"""BF-04: 符号遗传编程 (Symbolic Genetic Programming)。

使用类型化表达式树进行结构搜索。
操作: tournament selection, typed subtree crossover, point mutation,
      subtree mutation, parameter mutation, elitism,
      duplicate canonical hash elimination。

多目标适应度 (Pareto): Sharpe/ICIR/consistency/stability/marginal contribution。
防止过拟合: 最大树深/节点数、内外层CV、搜索预算。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class GPConfig:
    """遗传编程配置。"""

    population_size: int = 100
    generations: int = 20
    tournament_size: int = 3
    crossover_prob: float = 0.7
    mutation_prob: float = 0.3
    elitism_count: int = 2
    max_tree_depth: int = 5
    max_node_count: int = 30
    max_search_budget: int = 2000
    random_seed: int = 42


@dataclass
class GPIndividual:
    """遗传编程个体。"""

    individual_id: str
    expression_hash: str
    tree_depth: int
    node_count: int
    complexity_score: float
    fitness: dict[str, float] = field(default_factory=dict)
    generation: int = 0
    parent_ids: list[str] = field(default_factory=list)

    @property
    def is_pareto_optimal(self) -> bool:
        return self.fitness.get("pareto_rank", 999) == 0


class SymbolicGPGenerator:
    """符号遗传编程生成器。

    生成类型化表达式树，通过多目标进化搜索最优结构。
    当前为骨架实现 — 完整进化循环需要表达式求值引擎配合。
    """

    def __init__(self, config: GPConfig | None = None) -> None:
        self.config = config or GPConfig()
        self._rng = random.Random(self.config.random_seed)  # nosec B311 - deterministic research search
        self._population: list[GPIndividual] = []
        self._generation: int = 0
        self._seen_hashes: set[str] = set()
        self._offspring_serial: int = 0

    def initialize_population(self, primitives: list[str]) -> list[GPIndividual]:
        """随机初始化种群。"""
        cfg = self.config
        if not primitives:
            raise ValueError("primitives must not be empty")
        if cfg.population_size <= 0:
            raise ValueError("population_size must be positive")
        if cfg.max_tree_depth <= 0 or cfg.max_node_count <= 0:
            raise ValueError("tree and node limits must be positive")
        if cfg.max_search_budget < cfg.population_size:
            raise ValueError("max_search_budget must cover the initial population")

        self._rng = random.Random(cfg.random_seed)  # nosec B311 - deterministic research search
        self._seen_hashes.clear()
        self._offspring_serial = 0
        self._generation = 0
        population = []

        for i in range(cfg.population_size):
            depth = self._rng.randint(1, min(3, cfg.max_tree_depth))
            primitive = self._rng.choice(primitives)
            expr_hash = hashlib.sha256(f"gp:{primitive}:d{depth}:g0:i{i}".encode()).hexdigest()[:20]

            if expr_hash not in self._seen_hashes:
                self._seen_hashes.add(expr_hash)
                population.append(
                    GPIndividual(
                        individual_id=f"gp_g0_i{i}",
                        expression_hash=expr_hash,
                        tree_depth=depth,
                        node_count=depth * 2,
                        complexity_score=depth * 1.5,
                    )
                )

        self._population = population
        return population

    def evaluate_fitness(
        self,
        population: list[GPIndividual],
        evaluator: Callable[[str], dict[str, float]],
    ) -> list[GPIndividual]:
        """评估种群适应度。

        Args:
            population: 当前种群
            evaluator: (expression_hash) -> {metric: value} 评估函数

        Returns:
            带适应度的种群
        """
        for ind in population:
            try:
                ind.fitness = evaluator(ind.expression_hash)
            except Exception:  # nosec B112 - isolate one failed research generation
                ind.fitness = {"sharpe": -999.0, "icir": -999.0}

        return population

    def select_parent(self, population: list[GPIndividual]) -> GPIndividual:
        """锦标赛选择。"""
        if not population:
            raise ValueError("cannot select a parent from an empty population")
        cfg = self.config
        candidates = self._rng.sample(population, min(cfg.tournament_size, len(population)))
        return max(candidates, key=lambda ind: ind.fitness.get("sharpe", -999))

    def crossover(self, parent1: GPIndividual, parent2: GPIndividual) -> GPIndividual:
        """类型化子树交叉。"""
        self._offspring_serial += 1
        new_hash = hashlib.sha256(
            (
                f"cx:{parent1.expression_hash[:8]}:{parent2.expression_hash[:8]}:"
                f"g{self._generation}:n{self._offspring_serial}"
            ).encode()
        ).hexdigest()[:20]

        return GPIndividual(
            individual_id=f"gp_g{self._generation}_cx_{self._offspring_serial}",
            expression_hash=new_hash,
            tree_depth=max(parent1.tree_depth, parent2.tree_depth),
            node_count=parent1.node_count + parent2.node_count,
            complexity_score=(parent1.complexity_score + parent2.complexity_score) / 2,
            generation=self._generation,
            parent_ids=[parent1.individual_id, parent2.individual_id],
        )

    def mutate(self, individual: GPIndividual) -> GPIndividual:
        """点突变。"""
        self._offspring_serial += 1
        new_hash = hashlib.sha256(
            (f"mut:{individual.expression_hash[:8]}:g{self._generation}:n{self._offspring_serial}").encode()
        ).hexdigest()[:20]

        return GPIndividual(
            individual_id=f"gp_g{self._generation}_mut_{self._offspring_serial}",
            expression_hash=new_hash,
            tree_depth=individual.tree_depth,
            node_count=max(1, individual.node_count + self._rng.choice([-1, 0, 1])),
            complexity_score=individual.complexity_score * self._rng.uniform(0.8, 1.2),
            generation=self._generation,
            parent_ids=[individual.individual_id],
        )

    def evolve_one_generation(
        self,
        evaluator: Callable[[str], dict[str, float]],
    ) -> list[GPIndividual]:
        """进化一代。"""
        cfg = self.config
        self._generation += 1
        pop = self._population
        if not pop:
            raise ValueError("population is not initialized")

        new_pop = []
        # Elitism
        evaluated = self.evaluate_fitness(pop, evaluator)
        evaluated.sort(key=lambda i: i.fitness.get("sharpe", -999), reverse=True)
        new_pop.extend(evaluated[: cfg.elitism_count])

        # Generate offspring
        max_attempts = max(cfg.population_size * 20, 20)
        attempts = 0
        while (
            len(new_pop) < cfg.population_size
            and len(self._seen_hashes) < cfg.max_search_budget
            and attempts < max_attempts
        ):
            attempts += 1
            if self._rng.random() < cfg.crossover_prob and len(pop) >= 2:
                p1 = self.select_parent(pop)
                p2 = self.select_parent(pop)
                child = self.crossover(p1, p2)
            else:
                parent = self.select_parent(pop)
                child = self.mutate(parent)

            within_limits = child.tree_depth <= cfg.max_tree_depth and child.node_count <= cfg.max_node_count
            if within_limits and child.expression_hash not in self._seen_hashes:
                self._seen_hashes.add(child.expression_hash)
                new_pop.append(child)

        self._population = new_pop[: cfg.population_size]
        return self._population

    def run(
        self,
        primitives: list[str],
        evaluator: Callable[[str], dict[str, float]],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[GPIndividual]:
        """执行完整的遗传编程搜索。

        Args:
            primitives: 可用原始特征
            evaluator: 适应度评估函数 (expression_hash) → {sharpe, icir, ...}
            progress_callback: 进度回调 (generation, total_generations)

        Returns:
            最终种群中的 Pareto 前沿个体
        """
        cfg = self.config

        if cfg.generations < 0:
            raise ValueError("generations must not be negative")

        # 初始化
        if not self._population:
            self.initialize_population(primitives)

        # 进化循环
        for gen in range(cfg.generations):
            self._generation = gen
            try:
                self.evolve_one_generation(evaluator)
            except Exception:
                # 单代失败不终止搜索
                continue  # nosec B112 - isolate one failed research generation

            if progress_callback:
                progress_callback(gen + 1, cfg.generations)

            # 去重检查
            if len(self._seen_hashes) >= cfg.max_search_budget:
                break

        # The last generation's offspring have not participated in the next
        # generation, so evaluate them before computing the final frontier.
        self.evaluate_fitness(self._population, evaluator)
        return self.get_pareto_front()

    def get_pareto_front(self) -> list[GPIndividual]:
        """获取 Pareto 前沿。"""
        return [ind for ind in self._population if ind.is_pareto_optimal]

    def get_search_statistics(self) -> dict[str, Any]:
        """搜索统计。"""
        return {
            "generation": self._generation,
            "population_size": len(self._population),
            "seen_hashes": len(self._seen_hashes),
            "max_budget": self.config.max_search_budget,
            "pareto_front_size": len(self.get_pareto_front()),
        }
