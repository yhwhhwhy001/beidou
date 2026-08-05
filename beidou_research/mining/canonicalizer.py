"""BF-03: 表达式规范化器。

独立模块，对表达式 AST 进行规范化处理：
- 等价表达式去重（基于 canonical_hash）
- 表达式排序与归一化
- 批量规范化与统计
"""

from __future__ import annotations

import hashlib
from typing import Any


class ExpressionCanonicalizer:
    """表达式规范化器。

    封装 expression_ast 的规范化逻辑，提供批量处理能力。
    """

    def __init__(self) -> None:
        self._seen_hashes: set[str] = set()
        self._total_processed: int = 0
        self._duplicates_removed: int = 0

    def canonicalize(self, expression: Any) -> Any:
        """规范化单个表达式（委托给 expression 自身的 canonicalize 方法）。

        表达式需要实现 canonicalize() → canonical_hash() 接口。
        """
        if hasattr(expression, "canonicalize"):
            return expression.canonicalize()
        return expression

    def compute_hash(self, expression: Any) -> str:
        """计算表达式的规范化哈希。"""
        if hasattr(expression, "canonical_hash"):
            return expression.canonical_hash()
        # 回退到字符串哈希
        content = str(expression)
        return hashlib.sha256(content.encode()).hexdigest()[:20]

    def deduplicate(
        self,
        expressions: list[Any],
    ) -> list[Any]:
        """去除等价表达式。

        Args:
            expressions: 候选表达式列表

        Returns:
            去重后的表达式列表
        """
        unique = []
        for expr in expressions:
            h = self.compute_hash(expr)
            self._total_processed += 1
            if h not in self._seen_hashes:
                self._seen_hashes.add(h)
                unique.append(expr)
            else:
                self._duplicates_removed += 1
        return unique

    def are_equivalent(self, expr1: Any, expr2: Any) -> bool:
        """检查两个表达式是否等价。"""
        h1 = self.compute_hash(expr1)
        h2 = self.compute_hash(expr2)
        return h1 == h2

    @property
    def stats(self) -> dict:
        return {
            "total_processed": self._total_processed,
            "duplicates_removed": self._duplicates_removed,
            "unique_hashes": len(self._seen_hashes),
        }

    def reset(self) -> None:
        self._seen_hashes.clear()
        self._total_processed = 0
        self._duplicates_removed = 0
