"""BF-03: 表达式 AST。

因子挖掘的安全、类型化表达式系统。所有表达式节点均为 frozen
dataclass，在构造时执行类型检查；不同量纲的非法运算（如
Mul(PRICE, PRICE)）在构建时即被拒绝。

设计要点：
- 类型系统：ExprType 区分价格/收益/成交量/比率等量纲
- 规格化：canonicalize() 把表达式折叠为规范形态（去零/去恒等、
  常数折叠、交换律排序、结合律展平）
- 确定性哈希：canonical_hash() 基于规范化后的结构计算，
  等价的表达式产生相同 hash
- 复杂度评分：complexity_score() 每个节点 1 分 + 算子权重
- 最大回溯期：max_lookback() 推导表达式需要的最长历史窗口
- 序列化：to_dict() / from_dict() 支持 JSON 往返
- 求值：evaluate() 支持跨截面矩阵求值（symbols × time），
  不依赖 numpy

与 Python 的 ast 模块（安全解析）配合使用，禁止执行任意
Python 字符串 — 见 primitive_library.parse_expression。
"""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

# ================================================================
# 类型系统
# ================================================================


class ExprType(str, Enum):
    """表达式类型 — 每个表达式输出的量纲。

    类型检查依据：相同量纲才能相加/相减，乘法要求至少一侧为
    SCALAR（PRICE × PRICE 等不同量纲乘法在构建时拒绝）。
    """

    PRICE = "PRICE"
    RETURN = "RETURN"
    VOLUME = "VOLUME"
    RATE = "RATE"
    DEPTH = "DEPTH"
    VOLATILITY = "VOLATILITY"
    SCALAR = "SCALAR"
    BOOLEAN = "BOOLEAN"

    @property
    def is_numeric(self) -> bool:
        """是否数值类型（BOOLEAN 之外的类型）。"""
        return self is not ExprType.BOOLEAN


# ================================================================
# 异常
# ================================================================


class ExpressionError(ValueError):
    """表达式系统错误基类。"""


class ExpressionTypeError(ExpressionError):
    """表达式类型不匹配 — 非法量纲运算。"""


class ExpressionParseError(ExpressionError):
    """表达式解析错误（语法错误、非法构造）。"""


class UnsupportedExpressionError(ExpressionParseError):
    """不支持的表达式构造 — 安全解析的拒绝路径。"""


# ================================================================
# 基类与工具
# ================================================================


def _num(v: float | None) -> float:
    """None → NaN，其余转 float。"""
    return float("nan") if v is None else float(v)


def _is_nan(v: float) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _parse_expr_type(value: str) -> ExprType:
    """从序列化字符串还原 ExprType。"""
    try:
        return ExprType(value)
    except ValueError as e:
        raise ExpressionError(f"未知表达式类型: {value!r}") from e


def _stable_dumps(d: dict) -> str:
    """确定性 JSON 序列化（排序键、紧凑分隔符）。"""
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def _canon_key(node: "Expression") -> str:
    """节点排序键 — 用于交换律排序的确定性全序。"""
    return _stable_dumps(node.to_dict())


def _rolling_apply(row: Sequence[float], window: int, fn) -> list[float]:
    """按完整窗口（含当前值）应用滚动函数。

    窗口不满（i < window - 1）或窗口内有效值不足 window 个时
    返回 NaN。NaN 视为无效值。
    """
    out: list[float] = []
    for i in range(len(row)):
        if i < window - 1:
            out.append(float("nan"))
            continue
        win = row[i - window + 1 : i + 1]
        valid = [v for v in win if not _is_nan(v)]
        if len(valid) < window:
            out.append(float("nan"))
            continue
        out.append(fn(valid))
    return out


def _median(vals: Sequence[float]) -> float:
    """排序取中位（简化实现）。"""
    n = len(vals)
    if n == 0:
        return float("nan")
    s = sorted(vals)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _std(vals: Sequence[float]) -> float:
    """样本标准差（ddof=1）；样本数 < 2 时返回 NaN。"""
    n = len(vals)
    if n < 2:
        return float("nan")
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return math.sqrt(var)


def _quantile(vals: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 'linear' 方法一致）。"""
    s = sorted(vals)
    n = len(s)
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _rank_of(vals: Sequence[float], x: float) -> float:
    """x 在有效值序列中的归一化排名 [0, 1]（严格小于的比例）。"""
    if len(vals) < 2:
        return float("nan")
    lesser = sum(1 for v in vals if v < x)
    return lesser / (len(vals) - 1)


class Expression(ABC):
    """表达式节点基类。

    所有子类均为 frozen dataclass，构造时执行类型检查。
    """

    OP_NAME: str = ""  # 序列化/白名单操作名
    COMPLEXITY_WEIGHT: int = 0  # 算子复杂度权重

    # ---- 类型系统 ----

    @abstractmethod
    def output_type(self) -> ExprType:
        """表达式输出的类型。"""

    @abstractmethod
    def validate_types(self) -> None:
        """类型检查；非法量纲运算抛出 ExpressionTypeError。"""

    # ---- 规格化 ----

    @abstractmethod
    def canonicalize(self) -> "Expression":
        """返回规范化后的等价表达式。"""

    def canonical_hash(self) -> str:
        """确定性哈希：基于规范化后的结构，等价的表达式 hash 相同。

        hash 由规范化结构的 to_dict（排序键 JSON）计算，不含变量名、
        内存地址等实现细节；特征名属于语义内容，参与哈希。
        """
        canon = self.canonicalize()
        return hashlib.sha256(_stable_dumps(canon.to_dict()).encode("utf-8")).hexdigest()

    # ---- 复杂度与回溯期 ----

    @property
    def children(self) -> tuple["Expression", ...]:
        """子表达式（叶子节点默认无子节点）。"""
        return ()

    def complexity_score(self) -> int:
        """复杂度评分：每个节点 1 分 + 算子复杂度权重之和。"""
        return 1 + self.COMPLEXITY_WEIGHT + sum(child.complexity_score() for child in self.children)

    def max_lookback(self) -> int:
        """最大回溯期：所有 Lag/Diff/Rolling* 窗口的最大值。"""
        return max((c.max_lookback() for c in self.children), default=0)

    # ---- 序列化 ----

    @abstractmethod
    def to_dict(self) -> dict:
        """序列化为 JSON 可序列化 dict。"""

    @classmethod
    def from_dict(cls, data: dict) -> "Expression":
        """按 op 字段反序列化表达式（分派到具体节点）。"""
        if not isinstance(data, dict) or "op" not in data:
            raise ExpressionError(f"非法表达式字典: {data!r}")
        node_cls = NODE_BY_OP.get(data["op"])
        if node_cls is None:
            raise ExpressionError(f"未知算子: {data['op']!r}")
        return node_cls._rebuild(data)

    @classmethod
    @abstractmethod
    def _rebuild(cls, data: dict) -> "Expression":
        """具体节点的反序列化实现。"""

    # ---- 求值 ----

    def evaluate(
        self,
        data: Mapping[str, Sequence[Sequence[float | None]]],
    ) -> list[list[float]]:
        """跨截面求值：特征名 → (symbols × time) 矩阵。

        返回 (symbols × time) 的 float 矩阵，缺失值为 NaN。
        """
        return self._eval(data)

    def evaluate_series(
        self,
        data: Mapping[str, Sequence[float | None]],
    ) -> list[float]:
        """单标的求值便利方法：特征名 → 时间序列。"""
        nested: dict[str, list[list[float | None]]] = {name: [list(series)] for name, series in data.items()}
        return list(self._eval(nested)[0])

    @abstractmethod
    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        """内部求值实现。"""


# ================================================================
# 叶子节点
# ================================================================


@dataclass(frozen=True, slots=True)
class Constant(Expression):
    OP_NAME = "Constant"
    """数值常量。

    dtype 默认 SCALAR；可为布尔常量（dtype=BOOLEAN）用作
    Where 的条件。拒绝 NaN/inf 常量。
    """
    value: float | bool
    dtype: ExprType = ExprType.SCALAR

    def __post_init__(self) -> None:
        if not isinstance(self.dtype, ExprType):
            raise ExpressionTypeError(f"非法 dtype: {self.dtype!r}")
        if isinstance(self.value, bool):
            if self.dtype is not ExprType.BOOLEAN:
                raise ExpressionTypeError("布尔常量 dtype 必须为 BOOLEAN")
            return
        if self.dtype is ExprType.BOOLEAN:
            raise ExpressionTypeError("数值常量不能是 BOOLEAN 类型")
        v = float(self.value)
        if not math.isfinite(v):
            raise ExpressionError(f"常量必须为有限数: {self.value!r}")
        if v == 0.0:
            v = 0.0  # 规范化 -0.0 → 0.0
        object.__setattr__(self, "value", v)

    def output_type(self) -> ExprType:
        return self.dtype

    def validate_types(self) -> None:
        if not isinstance(self.dtype, ExprType):
            raise ExpressionTypeError(f"非法 dtype: {self.dtype!r}")
        if isinstance(self.value, bool):
            if self.dtype is not ExprType.BOOLEAN:
                raise ExpressionTypeError("布尔常量 dtype 必须为 BOOLEAN")
            return
        if self.dtype is ExprType.BOOLEAN:
            raise ExpressionTypeError("数值常量不能是 BOOLEAN 类型")
        if not math.isfinite(float(self.value)):
            raise ExpressionError(f"常量必须为有限数: {self.value!r}")

    def canonicalize(self) -> Expression:
        return self

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "value": self.value, "dtype": self.dtype.value}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Constant(data["value"], _parse_expr_type(data["dtype"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        if not data:
            raise ExpressionError("常量求值需要至少一个特征来推断序列长度")
        first = next(iter(data.values()))
        n_rows = len(first)
        n_cols = len(first[0]) if n_rows else 0
        value = float(self.value)
        return [[value] * n_cols for _ in range(n_rows)]


@dataclass(frozen=True, slots=True)
class Feature(Expression):
    OP_NAME = "Feature"
    """命名特征引用 — 数据中已计算好的原始特征。"""
    name: str
    type: ExprType

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ExpressionError("特征名必须为非空字符串")
        if not isinstance(self.type, ExprType):
            raise ExpressionTypeError(f"非法特征类型: {self.type!r}")
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.type

    def validate_types(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ExpressionError("特征名必须为非空字符串")
        if not isinstance(self.type, ExprType):
            raise ExpressionTypeError(f"非法特征类型: {self.type!r}")

    def canonicalize(self) -> Expression:
        return self

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "name": self.name, "type": self.type.value}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Feature(data["name"], _parse_expr_type(data["type"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        rows = data.get(self.name)
        if rows is None:
            raise ExpressionError(f"求值上下文缺少特征: {self.name!r}")
        return [[_num(v) for v in row] for row in rows]


# ================================================================
# 时序算子
# ================================================================


def _validate_int_param(name: str, value: object) -> None:
    """窗口/期数等整数参数的验证（排除 bool）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExpressionTypeError(f"{name} 必须为整数, 收到 {value!r}")


@dataclass(frozen=True, slots=True)
class Lag(Expression):
    OP_NAME = "Lag"
    """n 期滞后。"""
    expr: Expression
    n: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("Lag.n", self.n)
        if self.n < 1:
            raise ExpressionTypeError(f"Lag.n 必须 >= 1, 收到 {self.n}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("Lag 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return Lag(self.expr.canonicalize(), self.n)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return self.n + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "n": self.n}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Lag(from_dict(data["expr"]), data["n"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [
            [float("nan")] * self.n + list(row[: -self.n]) if self.n else list(row) for row in self.expr._eval(data)
        ]


@dataclass(frozen=True, slots=True)
class Diff(Expression):
    OP_NAME = "Diff"
    """n 期差分: x(t) - x(t-n)。"""
    expr: Expression
    n: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("Diff.n", self.n)
        if self.n < 1:
            raise ExpressionTypeError(f"Diff.n 必须 >= 1, 收到 {self.n}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("Diff 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return Diff(self.expr.canonicalize(), self.n)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return self.n + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "n": self.n}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Diff(from_dict(data["expr"]), data["n"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        rows: list[list[float]] = []
        for row in self.expr._eval(data):
            out: list[float] = []
            for i in range(len(row)):
                if i < self.n:
                    out.append(float("nan"))
                else:
                    out.append(row[i] - row[i - self.n])
            rows.append(out)
        return rows


@dataclass(frozen=True, slots=True)
class PctChange(Expression):
    OP_NAME = "PctChange"
    """n 期百分比变化: x(t)/x(t-n) - 1。

    基准为 0 或缺失时返回 NaN（百分比变化在零基下无定义）。
    """
    expr: Expression
    n: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        # 百分比变化无量纲
        return ExprType.RATE

    def validate_types(self) -> None:
        _validate_int_param("PctChange.n", self.n)
        if self.n < 1:
            raise ExpressionTypeError(f"PctChange.n 必须 >= 1, 收到 {self.n}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("PctChange 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return PctChange(self.expr.canonicalize(), self.n)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return self.n + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "n": self.n}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return PctChange(from_dict(data["expr"]), data["n"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        rows: list[list[float]] = []
        for row in self.expr._eval(data):
            out: list[float] = []
            for i in range(len(row)):
                if i < self.n:
                    out.append(float("nan"))
                    continue
                prev = row[i - self.n]
                if _is_nan(prev) or prev == 0.0:
                    out.append(float("nan"))
                else:
                    out.append(row[i] / prev - 1.0)
            rows.append(out)
        return rows


@dataclass(frozen=True, slots=True)
class RollingMean(Expression):
    OP_NAME = "RollingMean"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """滚动均值（完整窗口，含当前值）。"""
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("RollingMean.window", self.window)
        if self.window < 1:
            raise ExpressionTypeError(f"RollingMean.window 必须 >= 1, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RollingMean 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RollingMean(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RollingMean(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [_rolling_apply(row, self.window, lambda vals: sum(vals) / len(vals)) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class RollingStd(Expression):
    OP_NAME = "RollingStd"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """滚动标准差（样本标准差 ddof=1）。"""
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        # RETURN 的波动率语义上归为 VOLATILITY
        return ExprType.VOLATILITY if self.expr.output_type() is ExprType.RETURN else self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("RollingStd.window", self.window)
        if self.window < 2:
            raise ExpressionTypeError(f"RollingStd.window 必须 >= 2, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RollingStd 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RollingStd(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RollingStd(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [_rolling_apply(row, self.window, _std) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class RollingMedian(Expression):
    OP_NAME = "RollingMedian"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """滚动中位数（排序取中位，简化实现）。"""
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("RollingMedian.window", self.window)
        if self.window < 1:
            raise ExpressionTypeError(f"RollingMedian.window 必须 >= 1, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RollingMedian 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RollingMedian(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RollingMedian(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [_rolling_apply(row, self.window, _median) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class RollingMAD(Expression):
    OP_NAME = "RollingMAD"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """滚动 Median Absolute Deviation: median(|x - median|)。"""
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.VOLATILITY if self.expr.output_type() is ExprType.RETURN else self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("RollingMAD.window", self.window)
        if self.window < 1:
            raise ExpressionTypeError(f"RollingMAD.window 必须 >= 1, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RollingMAD 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RollingMAD(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RollingMAD(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        def _mad(vals: Sequence[float]) -> float:
            med = _median(vals)
            return _median([abs(v - med) for v in vals])

        return [_rolling_apply(row, self.window, _mad) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class RollingQuantile(Expression):
    OP_NAME = "RollingQuantile"
    COMPLEXITY_WEIGHT = 3  # 算子复杂度权重
    """滚动分位数（线性插值，q ∈ [0, 1]）。"""
    expr: Expression
    window: int
    q: float

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("RollingQuantile.window", self.window)
        if self.window < 1:
            raise ExpressionTypeError(f"RollingQuantile.window 必须 >= 1, 收到 {self.window}")
        if isinstance(self.q, bool) or not isinstance(self.q, (int, float)):
            raise ExpressionTypeError(f"RollingQuantile.q 必须为数值, 收到 {self.q!r}")
        q = float(self.q)
        if not 0.0 <= q <= 1.0:
            raise ExpressionTypeError(f"RollingQuantile.q 必须位于 [0, 1], 收到 {q}")
        object.__setattr__(self, "q", q)
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RollingQuantile 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RollingQuantile(self.expr.canonicalize(), self.window, self.q)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window, "q": self.q}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RollingQuantile(from_dict(data["expr"]), data["window"], data["q"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [_rolling_apply(row, self.window, lambda vals: _quantile(vals, self.q)) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class EMA(Expression):
    OP_NAME = "EMA"
    COMPLEXITY_WEIGHT = 2  # 算子复杂度权重
    """指数移动平均: y[t] = α·x[t] + (1-α)·y[t-1], α = 2/(span+1)。

    首值直接取 x[0]；缺失值按 NaN 传播。
    """
    expr: Expression
    span: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        _validate_int_param("EMA.span", self.span)
        if self.span < 1:
            raise ExpressionTypeError(f"EMA.span 必须 >= 1, 收到 {self.span}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("EMA 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return EMA(self.expr.canonicalize(), self.span)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return self.span + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "span": self.span}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return EMA(from_dict(data["expr"]), data["span"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        alpha = 2.0 / (self.span + 1)
        rows: list[list[float]] = []
        for row in self.expr._eval(data):
            out: list[float] = []
            prev = float("nan")
            for v in row:
                prev = v if _is_nan(prev) else alpha * v + (1.0 - alpha) * prev
                out.append(prev)
            rows.append(out)
        return rows


@dataclass(frozen=True, slots=True)
class TsRank(Expression):
    OP_NAME = "TsRank"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """时序排名：当前值在滚动窗口内的归一化排名 [0, 1]。"""
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        # 归一化排名无量纲
        return ExprType.RATE

    def validate_types(self) -> None:
        _validate_int_param("TsRank.window", self.window)
        if self.window < 2:
            raise ExpressionTypeError(f"TsRank.window 必须 >= 2, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("TsRank 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return TsRank(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return TsRank(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        def _rank(vals: Sequence[float]) -> float:
            return _rank_of(vals, vals[-1])

        return [_rolling_apply(row, self.window, _rank) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class CsRank(Expression):
    OP_NAME = "CsRank"
    """截面排名：每个时刻跨标的的归一化排名 [0, 1]。

    有效标的小于 2 时返回 NaN。跨截面求值在 evaluate() 的
    矩阵上下文中完成。
    """
    expr: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.RATE

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("CsRank 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return CsRank(self.expr.canonicalize())

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        # 截面排名不增加时间回溯
        return self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return CsRank(from_dict(data["expr"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        rows = self.expr._eval(data)
        n_rows = len(rows)
        n_cols = len(rows[0]) if n_rows else 0
        out: list[list[float]] = [[float("nan")] * n_cols for _ in range(n_rows)]
        for t in range(n_cols):
            valid = [(rows[s][t], s) for s in range(n_rows) if not _is_nan(rows[s][t])]
            if len(valid) < 2:
                continue
            for v, s in valid:
                out[s][t] = _rank_of([w for w, _ in valid], v)
        return out


@dataclass(frozen=True, slots=True)
class ZScore(Expression):
    OP_NAME = "ZScore"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """滚动 z-score: (x - mean) / std（ddof=1）。

    窗口内 std 为 0（常数序列）时返回 NaN。
    """
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        # z-score 无量纲
        return ExprType.RATE

    def validate_types(self) -> None:
        _validate_int_param("ZScore.window", self.window)
        if self.window < 2:
            raise ExpressionTypeError(f"ZScore.window 必须 >= 2, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("ZScore 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return ZScore(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return ZScore(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        def _zs(vals: Sequence[float]) -> float:
            m = sum(vals) / len(vals)
            sd = _std(vals)
            if _is_nan(sd) or sd == 0.0:
                return float("nan")
            return (vals[-1] - m) / sd

        return [_rolling_apply(row, self.window, _zs) for row in self.expr._eval(data)]


@dataclass(frozen=True, slots=True)
class RobustZScore(Expression):
    OP_NAME = "RobustZScore"
    COMPLEXITY_WEIGHT = 1  # 算子复杂度权重
    """稳健 z-score: (x - median) / MAD。

    MAD 为 0（半常数序列）时返回 NaN。
    """
    expr: Expression
    window: int

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.RATE

    def validate_types(self) -> None:
        _validate_int_param("RobustZScore.window", self.window)
        if self.window < 2:
            raise ExpressionTypeError(f"RobustZScore.window 必须 >= 2, 收到 {self.window}")
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("RobustZScore 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return RobustZScore(self.expr.canonicalize(), self.window)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def max_lookback(self) -> int:
        return (self.window - 1) + self.expr.max_lookback()

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict(), "window": self.window}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return RobustZScore(from_dict(data["expr"]), data["window"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        def _rzs(vals: Sequence[float]) -> float:
            med = _median(vals)
            mad = _median([abs(v - med) for v in vals])
            if _is_nan(mad) or mad <= 1e-12:
                return float("nan")
            return (vals[-1] - med) / mad

        return [_rolling_apply(row, self.window, _rzs) for row in self.expr._eval(data)]


# ================================================================
# 数学变换
# ================================================================


@dataclass(frozen=True, slots=True)
class SafeDiv(Expression):
    OP_NAME = "SafeDiv"
    """安全除法: |b| > epsilon 时返回 a/b，否则返回 0.0。

    除数为 0 或 NaN 时不产生 NaN/inf（除以零不传播 NaN）。
    """
    a: Expression
    b: Expression
    epsilon: float = 1e-10

    def __post_init__(self) -> None:
        if isinstance(self.epsilon, bool) or not isinstance(self.epsilon, (int, float)):
            raise ExpressionTypeError(f"SafeDiv.epsilon 必须为数值, 收到 {self.epsilon!r}")
        eps = float(self.epsilon)
        if eps <= 0.0:
            raise ExpressionTypeError(f"SafeDiv.epsilon 必须 > 0, 收到 {eps}")
        object.__setattr__(self, "epsilon", eps)
        self.validate_types()

    def output_type(self) -> ExprType:
        ta, tb = self.a.output_type(), self.b.output_type()
        if ta is ExprType.SCALAR and tb is ExprType.SCALAR:
            return ExprType.SCALAR
        if tb is ExprType.SCALAR:
            return ta
        if ta is tb:
            # 相同量纲相除 → 无量纲比率
            return ExprType.RATE
        return ExprType.RATE

    def validate_types(self) -> None:
        for side, expr in (("SafeDiv.a", self.a), ("SafeDiv.b", self.b)):
            if not expr.output_type().is_numeric:
                raise ExpressionTypeError(f"{side} 必须为数值类型")

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        # 常数折叠
        if (
            isinstance(a, Constant)
            and isinstance(b, Constant)
            and a.dtype is not ExprType.BOOLEAN
            and b.dtype is not ExprType.BOOLEAN
        ):
            den = float(b.value)
            if abs(den) > self.epsilon:
                return Constant(float(a.value) / den, self.output_type())
            return Constant(0.0, self.output_type())
        # SafeDiv(x, x) → 1.0（x 非零常量已由常数折叠处理）
        if a == b and not isinstance(a, Constant):
            return Constant(1.0, self.output_type())
        return SafeDiv(a, b, self.epsilon)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {
            "op": self.OP_NAME,
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
            "epsilon": self.epsilon,
        }

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return SafeDiv(from_dict(data["a"]), from_dict(data["b"]), data["epsilon"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        ra = self.a._eval(data)
        rb = self.b._eval(data)
        eps = self.epsilon
        out: list[list[float]] = []
        for s in range(len(ra)):
            row: list[float] = []
            for t in range(len(ra[s])):
                den = rb[s][t]
                if not _is_nan(den) and abs(den) > eps:
                    row.append(ra[s][t] / den)
                else:
                    row.append(0.0)
            out.append(row)
        return out


@dataclass(frozen=True, slots=True)
class SignedLog1p(Expression):
    OP_NAME = "SignedLog1p"
    """符号 log1p: sign(x) · log(1 + |x|)。

    奇函数、零处连续（0 → 0），适合收益等可正可负的序列。
    """
    expr: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        # 收益的对称压缩变换仍为收益量纲
        if self.expr.output_type() is ExprType.RETURN:
            return ExprType.RETURN
        return ExprType.RATE

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("SignedLog1p 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return SignedLog1p(self.expr.canonicalize())

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return SignedLog1p(from_dict(data["expr"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        out: list[list[float]] = []
        for row in self.expr._eval(data):
            out.append([math.copysign(math.log1p(abs(v)), v) if v != 0.0 else 0.0 for v in row])
        return out


@dataclass(frozen=True, slots=True)
class SignedSqrt(Expression):
    OP_NAME = "SignedSqrt"
    """符号开方: sign(x) · sqrt(|x|)。

    奇函数、零处连续（0 → 0），用于收益的方差稳定化。
    """
    expr: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        if self.expr.output_type() is ExprType.RETURN:
            return ExprType.RETURN
        return ExprType.RATE

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("SignedSqrt 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return SignedSqrt(self.expr.canonicalize())

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return SignedSqrt(from_dict(data["expr"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        out: list[list[float]] = []
        for row in self.expr._eval(data):
            out.append([math.copysign(math.sqrt(abs(v)), v) if v != 0.0 else 0.0 for v in row])
        return out


@dataclass(frozen=True, slots=True)
class Clip(Expression):
    OP_NAME = "Clip"
    """区间截断: min(max(x, lower), upper)。"""
    expr: Expression
    lower: float
    upper: float

    def __post_init__(self) -> None:
        for name, v in (("lower", self.lower), ("upper", self.upper)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ExpressionTypeError(f"Clip.{name} 必须为数值, 收到 {v!r}")
            object.__setattr__(self, name, float(v))
        if self.lower > self.upper:
            raise ExpressionTypeError(f"Clip.lower ({self.lower}) 不能大于 upper ({self.upper})")
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("Clip 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        return Clip(self.expr.canonicalize(), self.lower, self.upper)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def to_dict(self) -> dict:
        return {
            "op": self.OP_NAME,
            "expr": self.expr.to_dict(),
            "lower": self.lower,
            "upper": self.upper,
        }

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Clip(from_dict(data["expr"]), data["lower"], data["upper"])

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        out: list[list[float]] = []
        for row in self.expr._eval(data):
            out.append([v if _is_nan(v) else min(max(v, self.lower), self.upper) for v in row])
        return out


@dataclass(frozen=True, slots=True)
class Residualize(Expression):
    OP_NAME = "Residualize"
    COMPLEXITY_WEIGHT = 5  # 算子复杂度权重
    """对控制变量做逐步 OLS 残差化。

    该算子在表达式级别提供确定性的逐控制变量近似；生产晋级仍须
    通过研究评估器的样本外、成本后和容量门禁，不能仅凭表达式可求值
    视为可交易 Alpha 证据。
    """
    expr: Expression
    controls: tuple[Expression, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.controls, tuple) or not all(isinstance(c, Expression) for c in self.controls):
            raise ExpressionTypeError("Residualize.controls 必须为表达式元组")
        object.__setattr__(self, "controls", tuple(self.controls))
        self.validate_types()

    def output_type(self) -> ExprType:
        # 残差与 x 同量纲
        return self.expr.output_type()

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("Residualize 的 x 必须为数值类型")
        for c in self.controls:
            if not c.output_type().is_numeric:
                raise ExpressionTypeError("Residualize 的控制变量必须为数值类型")

    def canonicalize(self) -> Expression:
        return Residualize(
            self.expr.canonicalize(),
            tuple(c.canonicalize() for c in self.controls),
        )

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr, *self.controls)

    def to_dict(self) -> dict:
        return {
            "op": self.OP_NAME,
            "expr": self.expr.to_dict(),
            "controls": [c.to_dict() for c in self.controls],
        }

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Residualize(
            from_dict(data["expr"]),
            tuple(from_dict(c) for c in data["controls"]),
        )

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        """OLS 残差化：对每个控制变量逐步去相关。

        逐步 OLS: residual = y - (alpha + beta * x) for each control。
        这不是真正的多元回归，但提供了有效的去相关近似。
        """
        y = self.expr._eval(data)
        if not self.controls:
            return y

        # 评估所有控制变量
        control_vals = [c._eval(data) for c in self.controls]

        n_timesteps = len(y)
        n_symbols = len(y[0]) if y else 0
        if n_timesteps == 0 or n_symbols == 0:
            return y

        result = [[y[t][s] for s in range(n_symbols)] for t in range(n_timesteps)]

        for cv in control_vals:
            if len(cv) != n_timesteps or len(cv[0]) != n_symbols:
                continue
            for s in range(n_symbols):
                # 提取当前 symbol 的 y 和 x
                y_col = [result[t][s] for t in range(n_timesteps)]
                x_col = [cv[t][s] for t in range(n_timesteps)]

                # 过滤 None/NaN
                valid = [
                    (yt, xt)
                    for yt, xt in zip(y_col, x_col, strict=False)
                    if yt is not None
                    and xt is not None
                    and not (isinstance(yt, float) and (math.isnan(yt) or math.isinf(yt)))
                    and not (isinstance(xt, float) and (math.isnan(xt) or math.isinf(xt)))
                ]
                if len(valid) < 3:
                    continue

                yv = [p[0] for p in valid]
                xv = [p[1] for p in valid]
                n_v = len(yv)

                mean_y = sum(yv) / n_v
                mean_x = sum(xv) / n_v
                cov = sum((yv[i] - mean_y) * (xv[i] - mean_x) for i in range(n_v))
                var_x = sum((xi - mean_x) ** 2 for xi in xv)
                if abs(var_x) < 1e-15:
                    continue
                beta = cov / var_x
                alpha = mean_y - beta * mean_x

                # 更新：residual = y - (alpha + beta * x)
                for t in range(n_timesteps):
                    if y_col[t] is not None and x_col[t] is not None:
                        result[t][s] = y_col[t] - (alpha + beta * x_col[t])

        return result


@dataclass(frozen=True, slots=True)
class Where(Expression):
    OP_NAME = "Where"
    """条件选择: condition 为真取 a，否则取 b。"""
    condition: Expression
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.a.output_type()

    def validate_types(self) -> None:
        if self.condition.output_type() is not ExprType.BOOLEAN:
            raise ExpressionTypeError(f"Where.condition 必须为 BOOLEAN 类型, 收到 {self.condition.output_type()}")
        if self.a.output_type() is not self.b.output_type():
            raise ExpressionTypeError(f"Where 两个分支类型不一致: {self.a.output_type()} vs {self.b.output_type()}")

    def canonicalize(self) -> Expression:
        return Where(
            self.condition.canonicalize(),
            self.a.canonicalize(),
            self.b.canonicalize(),
        )

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.condition, self.a, self.b)

    def to_dict(self) -> dict:
        return {
            "op": self.OP_NAME,
            "condition": self.condition.to_dict(),
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
        }

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Where(
            from_dict(data["condition"]),
            from_dict(data["a"]),
            from_dict(data["b"]),
        )

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        cond = self.condition._eval(data)
        ra = self.a._eval(data)
        rb = self.b._eval(data)
        out: list[list[float]] = []
        for s in range(len(ra)):
            row: list[float] = []
            for t in range(len(ra[s])):
                c = cond[s][t]
                if _is_nan(c):
                    row.append(float("nan"))
                elif c:
                    row.append(ra[s][t])
                else:
                    row.append(rb[s][t])
            out.append(row)
        return out


# ================================================================
# 算术与比较算子
# ================================================================


def _validate_mixing(name: str, a: Expression, b: Expression) -> ExprType:
    """加减与比较的混合类型规则。

    允许: 类型相同，或任意一侧为 SCALAR（标量广播）。
    拒绝: 两个不同的非标量量纲。
    """
    ta, tb = a.output_type(), b.output_type()
    if ta is tb:
        return ta
    if ta is ExprType.SCALAR:
        return tb
    if tb is ExprType.SCALAR:
        return ta
    raise ExpressionTypeError(f"{name} 非法量纲组合: {ta.value} {name} {tb.value}")


@dataclass(frozen=True, slots=True)
class Add(Expression):
    OP_NAME = "Add"
    """加法（交换、结合，支持常数折叠）。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return _validate_mixing("+", self.a, self.b)

    def validate_types(self) -> None:
        _validate_mixing("+", self.a, self.b)
        if not self.a.output_type().is_numeric or not self.b.output_type().is_numeric:
            raise ExpressionTypeError("Add 只接受数值类型操作数")

    def canonicalize(self) -> Expression:
        # 展平嵌套加法 → 按 dtype 折叠常数 → 交换律排序 → 右结合重建
        parts: list[Expression] = []
        for child in (self.a, self.b):
            canon = child.canonicalize()
            if isinstance(canon, Add):
                parts.extend(_flatten_add(canon))
            else:
                parts.append(canon)
        rest: list[Expression] = []
        sums: dict[ExprType, float] = {}
        for p in parts:
            if isinstance(p, Constant) and p.dtype.is_numeric:
                sums[p.dtype] = sums.get(p.dtype, 0.0) + float(p.value)
            else:
                rest.append(p)
        for dtype, total in sums.items():
            if total == 0.0:
                total = 0.0  # 规范化 -0.0
            if total != 0.0:
                rest.append(Constant(total, dtype))
            elif not rest:
                rest.append(Constant(0.0, dtype))
        if not rest:
            return Constant(0.0, ExprType.SCALAR)
        rest.sort(key=_canon_key)
        node = rest[0]
        for nxt in rest[1:]:
            node = Add(nxt, node)
        return node

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Add(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: x + y)


def _flatten_add(node: Add) -> list[Expression]:
    parts: list[Expression] = []
    for child in (node.a, node.b):
        if isinstance(child, Add):
            parts.extend(_flatten_add(child))
        else:
            parts.append(child)
    return parts


@dataclass(frozen=True, slots=True)
class Sub(Expression):
    OP_NAME = "Sub"
    """减法。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return _validate_mixing("-", self.a, self.b)

    def validate_types(self) -> None:
        _validate_mixing("-", self.a, self.b)
        if not self.a.output_type().is_numeric or not self.b.output_type().is_numeric:
            raise ExpressionTypeError("Sub 只接受数值类型操作数")

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        # a - 0 → a
        if isinstance(b, Constant) and b.dtype.is_numeric and float(b.value) == 0.0:
            return a
        # 0 - b → -b
        if isinstance(a, Constant) and a.dtype.is_numeric and float(a.value) == 0.0:
            return Neg(b)
        # x - x → 0
        if a == b:
            return Constant(0.0, a.output_type())
        # 常数折叠
        if isinstance(a, Constant) and isinstance(b, Constant) and a.dtype is b.dtype and a.dtype.is_numeric:
            return Constant(float(a.value) - float(b.value), a.dtype)
        return Sub(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Sub(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: x - y)


@dataclass(frozen=True, slots=True)
class Mul(Expression):
    OP_NAME = "Mul"
    """乘法。至少一侧必须为 SCALAR；不同量纲乘法被拒绝。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        ta, tb = self.a.output_type(), self.b.output_type()
        if ta is ExprType.SCALAR:
            return tb
        return ta

    def validate_types(self) -> None:
        ta, tb = self.a.output_type(), self.b.output_type()
        if not ta.is_numeric or not tb.is_numeric:
            raise ExpressionTypeError("Mul 只接受数值类型操作数")
        if ta is not ExprType.SCALAR and tb is not ExprType.SCALAR:
            raise ExpressionTypeError(f"非法乘法量纲组合: {ta.value} * {tb.value}（至少一侧必须为 SCALAR）")

    def canonicalize(self) -> Expression:
        parts: list[Expression] = []
        for child in (self.a, self.b):
            canon = child.canonicalize()
            if isinstance(canon, Mul):
                parts.extend(_flatten_mul(canon))
            else:
                parts.append(canon)
        rest: list[Expression] = []
        prods: dict[ExprType, float] = {}
        zero: ExprType | None = None
        for p in parts:
            if isinstance(p, Constant) and p.dtype.is_numeric:
                v = float(p.value)
                if v == 0.0:
                    zero = p.dtype  # a * 0 → 0
                prods[p.dtype] = prods.get(p.dtype, 1.0) * v
            else:
                rest.append(p)
        if zero is not None:
            return Constant(0.0, zero)
        for dtype, prod in prods.items():
            if prod != 1.0:
                rest.append(Constant(prod, dtype))
        if not rest:
            return Constant(1.0, ExprType.SCALAR)
        rest.sort(key=_canon_key)
        node = rest[0]
        for nxt in rest[1:]:
            node = Mul(nxt, node)
        return node

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Mul(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: x * y)


def _flatten_mul(node: Mul) -> list[Expression]:
    parts: list[Expression] = []
    for child in (node.a, node.b):
        if isinstance(child, Mul):
            parts.extend(_flatten_mul(child))
        else:
            parts.append(child)
    return parts


@dataclass(frozen=True, slots=True)
class Neg(Expression):
    OP_NAME = "Neg"
    """一元取负。"""
    expr: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return self.expr.output_type()

    def validate_types(self) -> None:
        if not self.expr.output_type().is_numeric:
            raise ExpressionTypeError("Neg 只接受数值类型表达式")

    def canonicalize(self) -> Expression:
        x = self.expr.canonicalize()
        if isinstance(x, Constant) and x.dtype.is_numeric:
            return Constant(-float(x.value), x.dtype)
        if isinstance(x, Neg):
            return x.expr  # -(-x) → x
        return Neg(x)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.expr,)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "expr": self.expr.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Neg(from_dict(data["expr"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return [[-v for v in row] for row in self.expr._eval(data)]


def _binary_eval(
    ra: list[list[float]],
    rb: list[list[float]],
    op,
) -> list[list[float]]:
    """逐单元格二元运算（NaN 自然传播）。"""
    return [[op(ra[s][t], rb[s][t]) for t in range(len(ra[s]))] for s in range(len(ra))]


@dataclass(frozen=True, slots=True)
class Lt(Expression):
    OP_NAME = "Lt"
    """小于比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing("<", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) < float(b.value), ExprType.BOOLEAN)
        return Lt(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Lt(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        # NaN 参与比较恒为 False
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x < y else 0.0)


@dataclass(frozen=True, slots=True)
class Le(Expression):
    OP_NAME = "Le"
    """小于等于比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing("<=", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) <= float(b.value), ExprType.BOOLEAN)
        return Le(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Le(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x <= y else 0.0)


@dataclass(frozen=True, slots=True)
class Gt(Expression):
    OP_NAME = "Gt"
    """大于比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing(">", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) > float(b.value), ExprType.BOOLEAN)
        return Gt(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Gt(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x > y else 0.0)


@dataclass(frozen=True, slots=True)
class Ge(Expression):
    OP_NAME = "Ge"
    """大于等于比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing(">=", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) >= float(b.value), ExprType.BOOLEAN)
        return Ge(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Ge(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x >= y else 0.0)


@dataclass(frozen=True, slots=True)
class Eq(Expression):
    OP_NAME = "Eq"
    """相等比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing("==", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        # x == x → True
        if a == b:
            return Constant(True, ExprType.BOOLEAN)
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) == float(b.value), ExprType.BOOLEAN)
        # 交换律排序
        if _canon_key(b) < _canon_key(a):
            a, b = b, a
        return Eq(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Eq(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        # NaN != NaN，因此 NaN 恒为 0.0
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x == y else 0.0)


@dataclass(frozen=True, slots=True)
class Ne(Expression):
    OP_NAME = "Ne"
    """不等比较 → BOOLEAN。"""
    a: Expression
    b: Expression

    def __post_init__(self) -> None:
        self.validate_types()

    def output_type(self) -> ExprType:
        return ExprType.BOOLEAN

    def validate_types(self) -> None:
        _validate_mixing("!=", self.a, self.b)

    def canonicalize(self) -> Expression:
        a = self.a.canonicalize()
        b = self.b.canonicalize()
        # x != x → False
        if a == b:
            return Constant(False, ExprType.BOOLEAN)
        if isinstance(a, Constant) and isinstance(b, Constant):
            return Constant(float(a.value) != float(b.value), ExprType.BOOLEAN)
        if _canon_key(b) < _canon_key(a):
            a, b = b, a
        return Ne(a, b)

    @property
    def children(self) -> tuple[Expression, ...]:
        return (self.a, self.b)

    def to_dict(self) -> dict:
        return {"op": self.OP_NAME, "a": self.a.to_dict(), "b": self.b.to_dict()}

    @classmethod
    def _rebuild(cls, data: dict) -> Expression:
        return Ne(from_dict(data["a"]), from_dict(data["b"]))

    def _eval(self, data: Mapping[str, Sequence[Sequence[float | None]]]) -> list[list[float]]:
        return _binary_eval(self.a._eval(data), self.b._eval(data), lambda x, y: 1.0 if x != y else 0.0)


# ================================================================
# 节点注册表
# ================================================================

ALL_NODE_TYPES: tuple[type[Expression], ...] = (
    Constant,
    Feature,
    Lag,
    Diff,
    PctChange,
    RollingMean,
    RollingStd,
    RollingMedian,
    RollingMAD,
    RollingQuantile,
    EMA,
    TsRank,
    CsRank,
    ZScore,
    RobustZScore,
    SafeDiv,
    SignedLog1p,
    SignedSqrt,
    Clip,
    Residualize,
    Where,
    Add,
    Sub,
    Mul,
    Neg,
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    Ne,
)

NODE_BY_OP: dict[str, type[Expression]] = {cls.OP_NAME: cls for cls in ALL_NODE_TYPES}


def from_dict(data: dict) -> Expression:
    """模块级反序列化入口。"""
    return Expression.from_dict(data)


__all__ = [
    "EMA",
    "NODE_BY_OP",
    "Add",
    "Clip",
    "Constant",
    "CsRank",
    "Diff",
    "Eq",
    "ExprType",
    "Expression",
    "ExpressionError",
    "ExpressionParseError",
    "ExpressionTypeError",
    "Feature",
    "Ge",
    "Gt",
    "Lag",
    "Le",
    "Lt",
    "Mul",
    "Ne",
    "Neg",
    "PctChange",
    "Residualize",
    "RobustZScore",
    "RollingMAD",
    "RollingMean",
    "RollingMedian",
    "RollingQuantile",
    "RollingStd",
    "SafeDiv",
    "SignedLog1p",
    "SignedSqrt",
    "Sub",
    "TsRank",
    "UnsupportedExpressionError",
    "Where",
    "ZScore",
    "from_dict",
]
