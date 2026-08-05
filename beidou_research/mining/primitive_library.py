"""BF-03: Primitive Library — 算子白名单、特征注册与安全解析。

安全模型：因子表达式由字符串解析，但绝不执行任意 Python 代码。
parse_expression() 使用 Python 标准库 ast 模块把源码解析为受限的
语法树，然后仅允许白名单算子与注册特征，转换为 expression_ast
的类型化 AST。

禁止：
- eval / exec / compile / __import__ 及任何未知函数调用
- 属性访问（obj.attr）、下标（obj[i]）、lambda、推导式
- 字符串/字节/复数/集合等非常量字面量

只允许：算术运算符（+ - * /）、一元负号、比较运算、括号、
白名单函数调用、数字常量、已注册的特征名。
"""

from __future__ import annotations

import ast as py_ast
from dataclasses import dataclass
from typing import Mapping, Sequence

from .expression_ast import (
    Add,
    Clip,
    Constant,
    CsRank,
    Diff,
    EMA,
    Eq,
    Expression,
    ExpressionParseError,
    ExpressionTypeError,
    ExprType,
    Feature,
    Ge,
    Gt,
    Lag,
    Le,
    Lt,
    Mul,
    Ne,
    Neg,
    PctChange,
    Residualize,
    RobustZScore,
    RollingMAD,
    RollingMean,
    RollingMedian,
    RollingQuantile,
    RollingStd,
    SafeDiv,
    SignedLog1p,
    SignedSqrt,
    Sub,
    TsRank,
    UnsupportedExpressionError,
    Where,
    ZScore,
)


# ================================================================
# 算子白名单
# ================================================================

# 只允许 expression_ast 中定义的算子 — 白名单由 AST 节点注册表
# 自动推导，任何新增算子必须先在 expression_ast 中定义。
ALLOWED_OPERATORS: frozenset[str] = frozenset(
    cls.OP_NAME for cls in (
        Constant, Feature, Lag, Diff, PctChange,
        RollingMean, RollingStd, RollingMedian, RollingMAD, RollingQuantile,
        EMA, TsRank, CsRank, ZScore, RobustZScore,
        SafeDiv, SignedLog1p, SignedSqrt, Clip, Residualize, Where,
        Add, Sub, Mul, Neg,
        Lt, Le, Gt, Ge, Eq, Ne,
    )
)


# ================================================================
# 特征注册
# ================================================================

@dataclass(frozen=True, slots=True)
class FeatureDef:
    """可用特征定义。

    Attributes:
        name: 特征名（唯一）
        type: 特征量纲
        description: 特征描述
        min_window: 特征本身需要的最小历史窗口（K 线数）
        max_window: 特征最大可用窗口；None 表示无上限
    """
    name: str
    type: ExprType
    description: str = ""
    min_window: int = 1
    max_window: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ExpressionParseError("特征名必须为非空字符串")
        if not isinstance(self.type, ExprType):
            raise ExpressionTypeError(f"非法特征类型: {self.type!r}")
        if isinstance(self.min_window, bool) or not isinstance(self.min_window, int):
            raise ExpressionParseError("min_window 必须为整数")
        if self.min_window < 1:
            raise ExpressionParseError(f"min_window 必须 >= 1, 收到 {self.min_window}")
        if self.max_window is not None:
            if isinstance(self.max_window, bool) or not isinstance(self.max_window, int):
                raise ExpressionParseError("max_window 必须为整数或 None")
            if self.max_window < self.min_window:
                raise ExpressionParseError(
                    f"max_window ({self.max_window}) 不能小于 min_window ({self.min_window})"
                )


class PrimitiveRegistry:
    """管理可用特征定义，并提供受限解析入口。

    使用示例:
        registry = PrimitiveRegistry()
        registry.register(FeatureDef(name="close", type=ExprType.PRICE))
        expr = registry.parse("pct_change(close, 1) - rolling_mean(close, 20)")
    """

    def __init__(self) -> None:
        self._features: dict[str, FeatureDef] = {}

    def register(self, feature: FeatureDef) -> None:
        """注册特征定义；重复注册抛错。"""
        if feature.name in self._features:
            raise ExpressionParseError(f"特征重复注册: {feature.name}")
        self._features[feature.name] = feature

    def register_feature(
        self,
        name: str,
        type: ExprType,
        description: str = "",
        min_window: int = 1,
        max_window: int | None = None,
    ) -> None:
        """便捷注册：直接用参数注册特征。"""
        self.register(FeatureDef(
            name=name,
            type=type,
            description=description,
            min_window=min_window,
            max_window=max_window,
        ))

    def get(self, name: str) -> FeatureDef | None:
        """按名查找特征定义；不存在返回 None。"""
        return self._features.get(name)

    def has(self, name: str) -> bool:
        """特征是否已注册。"""
        return name in self._features

    @property
    def feature_defs(self) -> tuple[FeatureDef, ...]:
        """全部特征定义（按注册顺序）。"""
        return tuple(self._features.values())

    @property
    def feature_types(self) -> dict[str, ExprType]:
        """特征名 → 类型的映射（供解析器做类型检查）。"""
        return {name: definition.type for name, definition in self._features.items()}

    def parse(self, source: str) -> Expression:
        """以注册特征为上下文安全解析表达式字符串。"""
        return parse_expression(source, feature_types=self.feature_types)


# ================================================================
# 安全解析
# ================================================================

# 禁止作为特征名的保留字 — 即使未注册特征上下文也必须拒绝，
# 防止把危险内建函数名伪装成特征。
# 注意：open/type/id 等同时也是合法特征名（开盘价等），不在
# 黑名单内；它们的函数调用形式仍会被函数白名单拦截。
_FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "__import__", "import",
    "globals", "locals", "vars",
    "input", "getattr", "setattr", "delattr", "hasattr",
    "object", "lambda", "print",
    "breakpoint", "help", "exit", "quit", "copyright", "credits",
})


def _const_number(expr: Expression, what: str) -> float:
    """要求参数为数值常量并返回 float。"""
    if not isinstance(expr, Constant) or expr.dtype is not ExprType.SCALAR:
        raise ExpressionParseError(f"{what} 必须是数值常量, 收到 {expr.to_dict()!r}")
    return float(expr.value)


def _const_int(expr: Expression, what: str) -> int:
    """要求参数为整数值常量。"""
    v = _const_number(expr, what)
    if v != int(v):
        raise ExpressionParseError(f"{what} 必须为整数, 收到 {v}")
    return int(v)


def _parse_node(node: py_ast.AST, features: dict[str, ExprType]) -> Expression:
    """把受限 Python AST 节点转换为类型化表达式。"""

    if isinstance(node, py_ast.Constant):
        if isinstance(node.value, bool):
            return Constant(node.value, ExprType.BOOLEAN)
        if isinstance(node.value, (int, float)) and not isinstance(node.value, complex):
            return Constant(float(node.value), ExprType.SCALAR)
        raise UnsupportedExpressionError(
            f"不支持的字面量: {node.value!r}（只允许数字与布尔常量）"
        )

    if isinstance(node, py_ast.Name):
        name = node.id
        if name in _FORBIDDEN_NAMES:
            raise UnsupportedExpressionError(f"禁止的标识符: {name!r}")
        if features and name not in features:
            raise ExpressionParseError(f"未注册的特征: {name!r}")
        return Feature(name, features.get(name, ExprType.PRICE))

    if isinstance(node, py_ast.BinOp):
        left = _parse_node(node.left, features)
        right = _parse_node(node.right, features)
        if isinstance(node.op, py_ast.Add):
            return Add(left, right)
        if isinstance(node.op, py_ast.Sub):
            return Sub(left, right)
        if isinstance(node.op, py_ast.Mult):
            return Mul(left, right)
        if isinstance(node.op, py_ast.Div):
            # 除号映射为安全除法 — 除以零不传播 NaN
            return SafeDiv(left, right, 1e-10)
        raise UnsupportedExpressionError(
            f"不支持的二元运算符: {type(node.op).__name__}（只允许 + - * /）"
        )

    if isinstance(node, py_ast.UnaryOp):
        operand = _parse_node(node.operand, features)
        if isinstance(node.op, py_ast.USub):
            return Neg(operand)
        if isinstance(node.op, py_ast.UAdd):
            return operand
        raise UnsupportedExpressionError(
            f"不支持的一元运算符: {type(node.op).__name__}（只允许负号）"
        )

    if isinstance(node, py_ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise UnsupportedExpressionError("不支持链式比较（a < b < c）")
        left = _parse_node(node.left, features)
        right = _parse_node(node.comparators[0], features)
        op = node.ops[0]
        if isinstance(op, py_ast.Lt):
            return Lt(left, right)
        if isinstance(op, py_ast.LtE):
            return Le(left, right)
        if isinstance(op, py_ast.Gt):
            return Gt(left, right)
        if isinstance(op, py_ast.GtE):
            return Ge(left, right)
        if isinstance(op, py_ast.Eq):
            return Eq(left, right)
        if isinstance(op, py_ast.NotEq):
            return Ne(left, right)
        raise UnsupportedExpressionError(
            f"不支持的比较运算符: {type(op).__name__}（只允许 == != < <= > >=）"
        )

    if isinstance(node, py_ast.Call):
        if not isinstance(node.func, py_ast.Name):
            raise UnsupportedExpressionError("函数必须是直接命名调用（不支持属性/下标调用）")
        name = node.func.id
        if name in _FORBIDDEN_NAMES:
            raise UnsupportedExpressionError(f"禁止的调用: {name!r}")
        if name not in _FUNCTION_TABLE:
            raise UnsupportedExpressionError(
                f"未授权的函数调用: {name!r}（白名单: {sorted(_FUNCTION_TABLE)}）"
            )
        min_args, max_args, builder = _FUNCTION_TABLE[name]
        if not min_args <= len(node.args) <= max_args:
            if min_args == max_args:
                raise ExpressionParseError(
                    f"函数 {name}() 需要 {min_args} 个参数, 收到 {len(node.args)}"
                )
            raise ExpressionParseError(
                f"函数 {name}() 需要 {min_args}-{max_args} 个参数, 收到 {len(node.args)}"
            )
        if node.keywords:
            raise UnsupportedExpressionError("不支持关键字参数调用")
        parsed = [_parse_node(arg, features) for arg in node.args]
        return builder(parsed)

    # 其余节点一律拒绝
    raise UnsupportedExpressionError(
        f"不支持的表达式构造: {type(node).__name__}"
    )


def _build_lag(args: Sequence[Expression]) -> Expression:
    return Lag(args[0], _const_int(args[1], "lag 的期数 n"))


def _build_diff(args: Sequence[Expression]) -> Expression:
    return Diff(args[0], _const_int(args[1], "diff 的期数 n"))


def _build_pct_change(args: Sequence[Expression]) -> Expression:
    return PctChange(args[0], _const_int(args[1], "pct_change 的期数 n"))


def _build_rolling_mean(args: Sequence[Expression]) -> Expression:
    return RollingMean(args[0], _const_int(args[1], "rolling_mean 的窗口 window"))


def _build_rolling_std(args: Sequence[Expression]) -> Expression:
    return RollingStd(args[0], _const_int(args[1], "rolling_std 的窗口 window"))


def _build_rolling_median(args: Sequence[Expression]) -> Expression:
    return RollingMedian(args[0], _const_int(args[1], "rolling_median 的窗口 window"))


def _build_rolling_mad(args: Sequence[Expression]) -> Expression:
    return RollingMAD(args[0], _const_int(args[1], "rolling_mad 的窗口 window"))


def _build_rolling_quantile(args: Sequence[Expression]) -> Expression:
    window = _const_int(args[1], "rolling_quantile 的窗口 window")
    q = _const_number(args[2], "rolling_quantile 的分位数 q")
    return RollingQuantile(args[0], window, q)


def _build_ema(args: Sequence[Expression]) -> Expression:
    return EMA(args[0], _const_int(args[1], "ema 的 span"))


def _build_ts_rank(args: Sequence[Expression]) -> Expression:
    return TsRank(args[0], _const_int(args[1], "ts_rank 的窗口 window"))


def _build_cs_rank(args: Sequence[Expression]) -> Expression:
    return CsRank(args[0])


def _build_zscore(args: Sequence[Expression]) -> Expression:
    return ZScore(args[0], _const_int(args[1], "zscore 的窗口 window"))


def _build_robust_zscore(args: Sequence[Expression]) -> Expression:
    return RobustZScore(args[0], _const_int(args[1], "robust_zscore 的窗口 window"))


def _build_safe_div(args: Sequence[Expression]) -> Expression:
    if len(args) == 3:
        epsilon = _const_number(args[2], "safe_div 的 epsilon")
    else:
        epsilon = 1e-10
    return SafeDiv(args[0], args[1], epsilon)


def _build_signed_log1p(args: Sequence[Expression]) -> Expression:
    return SignedLog1p(args[0])


def _build_signed_sqrt(args: Sequence[Expression]) -> Expression:
    return SignedSqrt(args[0])


def _build_clip(args: Sequence[Expression]) -> Expression:
    return Clip(args[0], _const_number(args[1], "clip 的下界 lower"),
                _const_number(args[2], "clip 的上界 upper"))


def _build_where(args: Sequence[Expression]) -> Expression:
    return Where(args[0], args[1], args[2])


def _build_residualize(args: Sequence[Expression]) -> Expression:
    return Residualize(args[0], tuple(args[1:]))


# 函数名 → (最小参数数, 最大参数数, 构建器)
_FUNCTION_TABLE: dict[str, tuple[int, int, object]] = {
    "lag": (2, 2, _build_lag),
    "diff": (2, 2, _build_diff),
    "pct_change": (2, 2, _build_pct_change),
    "rolling_mean": (2, 2, _build_rolling_mean),
    "rolling_std": (2, 2, _build_rolling_std),
    "rolling_median": (2, 2, _build_rolling_median),
    "rolling_mad": (2, 2, _build_rolling_mad),
    "rolling_quantile": (3, 3, _build_rolling_quantile),
    "ema": (2, 2, _build_ema),
    "ts_rank": (2, 2, _build_ts_rank),
    "cs_rank": (1, 1, _build_cs_rank),
    "zscore": (2, 2, _build_zscore),
    "robust_zscore": (2, 2, _build_robust_zscore),
    "safe_div": (2, 3, _build_safe_div),
    "safediv": (2, 3, _build_safe_div),
    "signed_log1p": (1, 1, _build_signed_log1p),
    "signed_sqrt": (1, 1, _build_signed_sqrt),
    "clip": (3, 3, _build_clip),
    "where": (3, 3, _build_where),
    "residualize": (1, 100, _build_residualize),
}

# 解析可调用的白名单函数名（ALLOWED_OPERATORS 的解析层子集）
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(_FUNCTION_TABLE)


def parse_expression(
    source: str,
    feature_types: Mapping[str, ExprType] | None = None,
) -> Expression:
    """安全解析受限表达式语法为类型化 AST。

    Args:
        source: 表达式源码，如 "pct_change(close, 1) - rolling_mean(close, 20)"
        feature_types: 特征名 → 类型；未注册的特征名被拒绝。
            不提供时特征默认为 PRICE 类型。

    Raises:
        ExpressionParseError: 语法错误、参数错误、未注册特征
        UnsupportedExpressionError: 非白名单构造（eval/exec/属性访问等）
        ExpressionTypeError: 解析结果类型不合法
    """
    if not isinstance(source, str):
        raise ExpressionParseError(f"表达式源码必须为字符串, 收到 {type(source).__name__}")
    if not source.strip():
        raise ExpressionParseError("表达式源码为空")
    try:
        tree = py_ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ExpressionParseError(
            f"表达式语法错误: {e.msg}（第 {e.lineno} 行）"
        ) from e
    features = dict(feature_types or {})
    return _parse_node(tree.body, features)


__all__ = [
    "ALLOWED_OPERATORS",
    "ALLOWED_FUNCTIONS",
    "FeatureDef",
    "PrimitiveRegistry",
    "parse_expression",
]
