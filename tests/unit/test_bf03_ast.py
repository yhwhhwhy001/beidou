"""BF-03 测试 — 表达式 AST 与 Primitive Library.

覆盖：类型检查、规格化与常数折叠、确定性哈希、复杂度评分、
max_lookback 推导、序列化往返、安全解析（eval/exec 拒绝）、
数值边界条件（SafeDiv / SignedLog1p / SignedSqrt）。
"""

from __future__ import annotations

import json
import math

import pytest

from beidou_research.mining.expression_ast import (
    Add,
    Clip,
    Constant,
    CsRank,
    Diff,
    EMA,
    Eq,
    ExprType,
    ExpressionError,
    ExpressionParseError,
    ExpressionTypeError,
    Feature,
    Gt,
    Lag,
    Lt,
    Mul,
    Neg,
    PctChange,
    Residualize,
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
    from_dict,
)
from beidou_research.mining.primitive_library import (
    ALLOWED_OPERATORS,
    FeatureDef,
    PrimitiveRegistry,
    parse_expression,
)


# ================================================================
# 测试工具
# ================================================================

def close(t: ExprType = ExprType.PRICE) -> Feature:
    return Feature("close", t)


def volume() -> Feature:
    return Feature("volume", ExprType.VOLUME)


def isnan(v: float) -> bool:
    return isinstance(v, float) and math.isnan(v)


# ================================================================
# 基本构建与求值
# ================================================================

class TestBasicEvaluation:
    """基本表达式构建与求值。"""

    def test_constant_and_feature(self):
        expr = Add(Constant(1.0), close())
        assert expr.output_type() is ExprType.PRICE
        result = expr.evaluate_series({"close": [10.0, 20.0]})
        assert result == [11.0, 21.0]

    def test_lag(self):
        result = Lag(close(), 2).evaluate_series({"close": [1.0, 2.0, 3.0, 4.0]})
        assert isnan(result[0]) and isnan(result[1])
        assert result[2:] == [1.0, 2.0]

    def test_diff(self):
        result = Diff(close(), 1).evaluate_series({"close": [1.0, 2.0, 4.0]})
        assert isnan(result[0])
        assert result[1:] == [1.0, 2.0]

    def test_pct_change(self):
        result = PctChange(close(), 1).evaluate_series({"close": [1.0, 2.0, 4.0]})
        assert isnan(result[0])
        assert abs(result[1] - 1.0) < 1e-12
        assert abs(result[2] - 1.0) < 1e-12

    def test_pct_change_zero_base_is_nan(self):
        result = PctChange(close(), 1).evaluate_series({"close": [1.0, 0.0, 3.0]})
        assert isnan(result[2])

    def test_rolling_mean(self):
        result = RollingMean(close(), 3).evaluate_series(
            {"close": [1.0, 2.0, 4.0, 8.0, 16.0]}
        )
        assert isnan(result[0]) and isnan(result[1])
        assert abs(result[2] - 7.0 / 3.0) < 1e-12
        assert abs(result[3] - 14.0 / 3.0) < 1e-12
        assert abs(result[4] - 28.0 / 3.0) < 1e-12

    def test_rolling_median(self):
        result = RollingMedian(close(), 3).evaluate_series(
            {"close": [5.0, 1.0, 3.0, 10.0, 2.0]}
        )
        assert result[2] == 3.0
        assert result[3] == 3.0
        assert result[4] == 3.0

    def test_ema(self):
        result = EMA(close(), 1).evaluate_series({"close": [1.0, 2.0, 4.0]})
        # span=1 → alpha=1.0，EMA 退化为原序列
        assert result == [1.0, 2.0, 4.0]

    def test_where(self):
        cond = Lt(close(), Constant(3.0))
        expr = Where(cond, Constant(1.0), Constant(0.0))
        assert expr.output_type() is ExprType.SCALAR
        result = expr.evaluate_series({"close": [1.0, 5.0, 3.0]})
        assert result == [1.0, 0.0, 0.0]

    def test_clip(self):
        result = Clip(close(), 2.0, 4.0).evaluate_series({"close": [1.0, 3.0, 9.0]})
        assert result == [2.0, 3.0, 4.0]

    def test_cross_section_rank(self):
        # 两标的截面排名：每时刻小的得 0.0，大的得 1.0
        data = {
            "close": [
                [10.0, 5.0],
                [20.0, 8.0],
            ],
        }
        result = CsRank(close()).evaluate(data)
        assert result == [
            [0.0, 0.0],
            [1.0, 1.0],
        ]

    def test_ts_rank(self):
        result = TsRank(close(), 3).evaluate_series({"close": [1.0, 3.0, 2.0]})
        assert isnan(result[0]) and isnan(result[1])
        assert abs(result[2] - 0.5) < 1e-12  # 2 在 [1,3,2] 中排第 2/3

    def test_zscore(self):
        result = ZScore(close(), 3).evaluate_series({"close": [2.0, 4.0, 6.0]})
        assert isnan(result[0]) and isnan(result[1])
        # mean=4, std(ddof=1)=2, (6-4)/2 = 1.0
        assert abs(result[2] - 1.0) < 1e-12

    def test_constant_series_needs_feature_for_length(self):
        with pytest.raises(ExpressionError, match="序列长度"):
            Constant(1.0).evaluate({})

    def test_residualize_eval_ols_basic(self):
        """Residualize OLS 求值（逐步去相关）。"""
        expr = Residualize(close(), (volume(),))
        # 完美线性相关 → OLS 残差化后接近 0
        result = expr._eval({
            "close": [[1.0, 10.0], [2.0, 20.0], [3.0, 30.0], [4.0, 40.0], [5.0, 50.0]],
            "volume": [[2.0, 20.0], [4.0, 40.0], [6.0, 60.0], [8.0, 80.0], [10.0, 100.0]],
        })
        # 两个 symbol 都与 volume 完美线性相关 → 残差为 0
        for row in result:
            for v in row:
                assert abs(v) < 1e-9, f"OLS should zero out linear dependence: {v}"


# ================================================================
# 类型检查
# ================================================================

class TestTypeChecking:
    """不同量纲的非法运算在构建时拒绝。"""

    def test_mul_price_price_rejected(self):
        with pytest.raises(ExpressionTypeError, match="PRICE"):
            Mul(close(), close())

    def test_mul_scalar_price_allowed(self):
        expr = Mul(close(), Constant(2.0))
        assert expr.output_type() is ExprType.PRICE

    def test_add_different_dims_rejected(self):
        with pytest.raises(ExpressionTypeError):
            Add(close(), volume())

    def test_add_scalar_mixing_allowed(self):
        expr = Add(close(), Constant(1.0))
        assert expr.output_type() is ExprType.PRICE

    def test_sub_different_dims_rejected(self):
        with pytest.raises(ExpressionTypeError):
            Sub(close(), volume())

    def test_where_branches_type_mismatch_rejected(self):
        cond = Eq(close(), Constant(1.0))
        with pytest.raises(ExpressionTypeError, match="分支类型不一致"):
            Where(cond, close(), volume())

    def test_where_condition_must_be_boolean(self):
        with pytest.raises(ExpressionTypeError, match="BOOLEAN"):
            Where(close(), close(), close())

    def test_lag_boolean_rejected(self):
        with pytest.raises(ExpressionTypeError):
            Lag(Constant(True, ExprType.BOOLEAN), 1)

    def test_neg_boolean_rejected(self):
        with pytest.raises(ExpressionTypeError):
            Neg(Constant(True, ExprType.BOOLEAN))

    def test_bad_window_rejected(self):
        with pytest.raises(ExpressionTypeError):
            RollingMean(close(), 0)
        with pytest.raises(ExpressionTypeError):
            Lag(close(), 0)
        with pytest.raises(ExpressionTypeError):
            RollingQuantile(close(), 5, 1.5)

    def test_clip_lower_above_upper_rejected(self):
        with pytest.raises(ExpressionTypeError):
            Clip(close(), 5.0, 1.0)

    def test_pct_change_output_is_rate(self):
        assert PctChange(close(), 1).output_type() is ExprType.RATE
        assert TsRank(close(), 5).output_type() is ExprType.RATE
        assert ZScore(close(), 5).output_type() is ExprType.RATE
        assert CsRank(close()).output_type() is ExprType.RATE

    def test_rolling_std_of_return_is_volatility(self):
        ret = Feature("ret", ExprType.RETURN)
        assert RollingStd(ret, 5).output_type() is ExprType.VOLATILITY


# ================================================================
# 规格化与常数折叠
# ================================================================

class TestCanonicalization:
    """等价表达式产生相同 canonical_hash。"""

    def test_add_zero(self):
        assert Add(close(), Constant(0.0)).canonicalize() == close()
        assert Add(Constant(0.0), close()).canonicalize() == close()

    def test_mul_one(self):
        assert Mul(close(), Constant(1.0)).canonicalize() == close()

    def test_mul_zero(self):
        zero = Mul(close(), Constant(0.0)).canonicalize()
        assert isinstance(zero, Constant) and zero.value == 0.0

    def test_sub_self_is_zero(self):
        zero = Sub(close(), close()).canonicalize()
        assert isinstance(zero, Constant) and zero.value == 0.0

    def test_sub_zero(self):
        assert Sub(close(), Constant(0.0)).canonicalize() == close()

    def test_safe_div_self_is_one(self):
        one = SafeDiv(close(), close()).canonicalize()
        assert isinstance(one, Constant) and one.value == 1.0

    def test_safe_div_zero_zero_is_zero(self):
        zero = SafeDiv(Constant(0.0), Constant(0.0)).canonicalize()
        assert isinstance(zero, Constant) and zero.value == 0.0

    def test_commutative_swap(self):
        other = Feature("open", ExprType.PRICE)
        a = Add(close(), other)
        b = Add(other, close())
        assert a.canonical_hash() == b.canonical_hash()

    def test_associativity(self):
        low = Feature("low", ExprType.PRICE)
        a = Add(Add(close(), Feature("open", ExprType.PRICE)), low)
        b = Add(close(), Add(Feature("open", ExprType.PRICE), low))
        assert a.canonical_hash() == b.canonical_hash()

    def test_mul_commutative(self):
        a = Mul(Mul(close(), Constant(2.0)), Constant(3.0))
        b = Mul(Constant(3.0), Mul(Constant(2.0), close()))
        assert a.canonical_hash() == b.canonical_hash()

    def test_neg_neg(self):
        assert Neg(Neg(close())).canonicalize() == close()

    def test_constant_folding_add(self):
        folded = Add(Constant(1.0), Constant(2.0)).canonicalize()
        assert folded == Constant(3.0)

    def test_constant_folding_sub(self):
        folded = Sub(Constant(5.0), Constant(2.0)).canonicalize()
        assert folded == Constant(3.0)

    def test_constant_folding_mul(self):
        folded = Mul(Constant(2.0), Constant(3.0)).canonicalize()
        assert folded == Constant(6.0)

    def test_constant_folding_mixed(self):
        folded = Add(Constant(1.0), Add(close(), Constant(2.0))).canonicalize()
        assert folded == Add(close(), Constant(3.0))

    def test_constant_folding_safe_div(self):
        folded = SafeDiv(Constant(6.0), Constant(3.0)).canonicalize()
        assert isinstance(folded, Constant) and folded.value == 2.0

    def test_folded_expression_hashes_as_constant(self):
        assert Add(Constant(1.0), Constant(2.0)).canonical_hash() == Constant(3.0).canonical_hash()

    def test_hash_deterministic(self):
        expr = Add(close(), Mul(Constant(2.0), Feature("open", ExprType.PRICE)))
        assert expr.canonical_hash() == expr.canonical_hash()

    def test_different_expressions_different_hash(self):
        other = Feature("open", ExprType.PRICE)
        assert Add(close(), other).canonical_hash() != Sub(close(), other).canonical_hash()

    def test_hash_distinguishes_features(self):
        assert Feature("close", ExprType.PRICE).canonical_hash() != Feature(
            "open", ExprType.PRICE
        ).canonical_hash()


# ================================================================
# 复杂度评分
# ================================================================

class TestComplexityScore:
    """每个节点 1 分 + 算子复杂度权重。"""

    def test_leaf(self):
        assert Feature("close", ExprType.PRICE).complexity_score() == 1
        assert Constant(1.0).complexity_score() == 1

    def test_binary(self):
        assert Add(close(), close()).complexity_score() == 3

    def test_rolling_mean_weight_1(self):
        assert RollingMean(close(), 20).complexity_score() == 3  # 1 + 1 + 1

    def test_ema_weight_2(self):
        assert EMA(close(), 12).complexity_score() == 4  # 1 + 2 + 1

    def test_rolling_quantile_weight_3(self):
        assert RollingQuantile(close(), 10, 0.5).complexity_score() == 5  # 1 + 3 + 1

    def test_residualize_weight_5(self):
        assert Residualize(close(), (volume(),)).complexity_score() == 8  # 1 + 5 + 1 + 1

    def test_nested(self):
        inner = EMA(close(), 5)          # 4
        outer = RollingQuantile(inner, 10, 0.5)  # 4 + 4
        assert outer.complexity_score() == 8


# ================================================================
# max_lookback 推导
# ================================================================

class TestMaxLookback:
    """最大回溯期推导。"""

    def test_leaf_zero(self):
        assert Feature("close", ExprType.PRICE).max_lookback() == 0
        assert Constant(1.0).max_lookback() == 0

    def test_lag(self):
        assert Lag(close(), 3).max_lookback() == 3

    def test_diff(self):
        assert Diff(close(), 5).max_lookback() == 5

    def test_rolling_window(self):
        assert RollingMean(close(), 20).max_lookback() == 19
        assert RollingMedian(close(), 7).max_lookback() == 6
        assert RollingMAD(close(), 10).max_lookback() == 9
        assert RollingStd(close(), 10).max_lookback() == 9
        assert RollingQuantile(close(), 15, 0.5).max_lookback() == 14

    def test_ema_span(self):
        assert EMA(close(), 12).max_lookback() == 12

    def test_nested_combination(self):
        # RollingMean(Lag(close, 2), 10) → 2 + 9 = 11
        assert RollingMean(Lag(close(), 2), 10).max_lookback() == 11

    def test_max_over_branches(self):
        other = Feature("open", ExprType.PRICE)
        expr = Add(RollingMean(close(), 20), Lag(other, 5))
        assert expr.max_lookback() == 19

    def test_cs_rank_no_lookback(self):
        assert CsRank(close()).max_lookback() == 0


# ================================================================
# 序列化
# ================================================================

class TestSerialization:
    """to_dict / from_dict 往返。"""

    def test_roundtrip(self):
        other = Feature("open", ExprType.PRICE)
        expr = Add(
            RollingMean(close(), 20),
            Mul(Constant(2.0), other),
        )
        restored = from_dict(expr.to_dict())
        assert restored == expr

    def test_roundtrip_json(self):
        other = Feature("volume", ExprType.VOLUME)
        expr = Where(
            Lt(close(), EMA(close(), 5)),
            PctChange(close(), 1),
            SafeDiv(other, Lag(other, 1), 1e-8),
        )
        payload = json.dumps(expr.to_dict())
        restored = from_dict(json.loads(payload))
        assert restored == expr

    def test_roundtrip_preserves_hash(self):
        other = Feature("open", ExprType.PRICE)
        expr = Sub(RollingMean(close(), 10), RollingMean(other, 5))
        assert from_dict(expr.to_dict()).canonical_hash() == expr.canonical_hash()

    def test_unknown_op_rejected(self):
        with pytest.raises(ExpressionError, match="未知算子"):
            from_dict({"op": "Eval", "expr": {}})

    def test_from_dict_uses_whitelisted_ops(self):
        # 反序列化只能还原白名单内的算子
        for op in ("Add", "Lag", "RollingMean", "SafeDiv", "Where"):
            assert op in ALLOWED_OPERATORS
        assert "Eval" not in ALLOWED_OPERATORS


# ================================================================
# 安全除法与符号变换边界
# ================================================================

class TestSafeMath:
    """SafeDiv / SignedLog1p / SignedSqrt 边界条件。"""

    def test_safe_div_zero_denominator_no_nan(self):
        result = SafeDiv(close(), Lag(close(), 1)).evaluate_series(
            {"close": [5.0, 10.0, 0.0, 3.0]}
        )
        # t1: 10/5 = 2.0; t2: 0/10 = 0.0; t3: 3/0 → 0.0（不传播 NaN/inf）
        assert result[1] == 2.0
        assert result[2] == 0.0
        assert result[3] == 0.0
        assert not any(isnan(v) for v in result)

    def test_safe_div_small_denominator_clamped(self):
        result = SafeDiv(close(), close(), epsilon=1.0).evaluate_series(
            {"close": [1.0, 2.0, 3.0]}
        )
        # |close| 未必 > 1.0 阈值时返回 0.0
        assert all(v == 0.0 or abs(v - 1.0) < 1e-12 for v in result)

    def test_safe_div_numerator_nan_propagates(self):
        result = SafeDiv(Lag(close(), 1), close()).evaluate_series(
            {"close": [1.0, 2.0, 3.0]}
        )
        # t0: 分子 NaN（Lag 暖机），分母 1 → NaN 传播（分母非零）
        assert isnan(result[0])
        assert result[1] == 0.5

    def test_signed_log1p_zero(self):
        result = SignedLog1p(Constant(0.0)).evaluate_series({"close": [1.0, 1.0]})
        assert result == [0.0, 0.0]

    def test_signed_log1p_negative(self):
        result = SignedLog1p(Constant(-0.5)).evaluate_series({"close": [1.0]})
        assert abs(result[0] + math.log(1.5)) < 1e-12

    def test_signed_log1p_positive(self):
        result = SignedLog1p(Constant(0.5)).evaluate_series({"close": [1.0]})
        assert abs(result[0] - math.log(1.5)) < 1e-12

    def test_signed_sqrt_zero(self):
        result = SignedSqrt(Constant(0.0)).evaluate_series({"close": [1.0]})
        assert result == [0.0]

    def test_signed_sqrt_negative(self):
        result = SignedSqrt(Constant(-9.0)).evaluate_series({"close": [1.0]})
        assert result == [-3.0]

    def test_signed_sqrt_positive(self):
        result = SignedSqrt(Constant(9.0)).evaluate_series({"close": [1.0]})
        assert result == [3.0]

    def test_nan_constant_rejected(self):
        with pytest.raises(ExpressionError, match="有限数"):
            Constant(float("nan"))


# ================================================================
# 安全解析
# ================================================================

class TestSafeParse:
    """parse_expression 的安全性与功能。"""

    @pytest.fixture
    def feature_types(self) -> dict:
        return {
            "close": ExprType.PRICE,
            "open": ExprType.PRICE,
            "volume": ExprType.VOLUME,
        }

    def test_parse_arithmetic(self, feature_types):
        expr = parse_expression("close + 1", feature_types)
        assert isinstance(expr, Add)
        assert expr.output_type() is ExprType.PRICE

    def test_parse_division_is_safe_div(self, feature_types):
        expr = parse_expression("close / open", feature_types)
        assert isinstance(expr, SafeDiv)

    def test_parse_function_calls(self, feature_types):
        expr = parse_expression(
            "where(close > ema(close, 5), 1, 0)", feature_types
        )
        assert isinstance(expr, Where)
        assert isinstance(expr.condition, Gt)
        assert isinstance(expr.condition.b, EMA)

    def test_parse_pct_change_roll(self, feature_types):
        expr = parse_expression("pct_change(close, 1)", feature_types)
        assert isinstance(expr, PctChange)

    def test_parse_safe_div_with_epsilon(self, feature_types):
        expr = parse_expression("safe_div(close, open, 1e-6)", feature_types)
        assert isinstance(expr, SafeDiv)
        assert expr.epsilon == 1e-6

    def test_parse_residualize(self, feature_types):
        expr = parse_expression("residualize(close, open, volume)", feature_types)
        assert isinstance(expr, Residualize)
        assert len(expr.controls) == 2

    def test_parse_eval_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("eval('1')", feature_types)

    def test_parse_exec_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("exec('x = 1')", feature_types)

    def test_parse_import_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("__import__('os')", feature_types)

    def test_parse_attribute_access_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("close.__class__", feature_types)

    def test_parse_subscript_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("close[0]", feature_types)

    def test_parse_lambda_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("lambda x: 1", feature_types)

    def test_parse_boolop_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("close > 1 or close < 0", feature_types)

    def test_parse_string_literal_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("'hello'", feature_types)

    def test_parse_assignment_rejected(self, feature_types):
        with pytest.raises(ExpressionParseError):
            parse_expression("x = 1", feature_types)

    def test_parse_unknown_function_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError, match="未授权"):
            parse_expression("foo(close)", feature_types)

    def test_parse_unknown_feature_rejected(self, feature_types):
        with pytest.raises(ExpressionParseError, match="未注册"):
            parse_expression("close + unknown_feature", feature_types)

    def test_parse_bare_eval_name_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError, match="禁止"):
            parse_expression("eval", feature_types)

    def test_parse_reserved_feature_name_rejected(self):
        # 未提供特征上下文时，危险内建名也不能作为特征
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("eval + 1", None)

    def test_parse_syntax_error(self, feature_types):
        with pytest.raises(ExpressionParseError, match="语法错误"):
            parse_expression("close +", feature_types)

    def test_parse_wrong_arity(self, feature_types):
        with pytest.raises(ExpressionParseError, match="参数"):
            parse_expression("lag(close)", feature_types)

    def test_parse_non_constant_param_rejected(self, feature_types):
        with pytest.raises(ExpressionParseError, match="数值常量"):
            parse_expression("lag(close, open)", feature_types)

    def test_parse_pow_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError):
            parse_expression("close ** 2", feature_types)

    def test_parse_chain_comparison_rejected(self, feature_types):
        with pytest.raises(UnsupportedExpressionError, match="链式"):
            parse_expression("1 < close < 3", feature_types)

    def test_parsed_expr_roundtrip(self, feature_types):
        expr = parse_expression(
            "rolling_mean(close, 20) - rolling_mean(close, 5)", feature_types
        )
        assert from_dict(expr.to_dict()) == expr

    def test_parsed_expr_can_evaluate(self, feature_types):
        expr = parse_expression("close + 1", feature_types)
        result = expr.evaluate_series({"close": [10.0, 20.0]})
        assert result == [11.0, 21.0]


# ================================================================
# PrimitiveRegistry
# ================================================================

class TestPrimitiveRegistry:
    """特征注册与注册表解析。"""

    @pytest.fixture
    def registry(self) -> PrimitiveRegistry:
        reg = PrimitiveRegistry()
        reg.register(FeatureDef(
            name="close", type=ExprType.PRICE,
            description="收盘价", min_window=1, max_window=500,
        ))
        reg.register_feature("volume", ExprType.VOLUME)
        return reg

    def test_register_and_get(self, registry):
        definition = registry.get("close")
        assert definition is not None
        assert definition.type is ExprType.PRICE
        assert definition.description == "收盘价"
        assert registry.has("close")
        assert not registry.has("open")

    def test_duplicate_registration_rejected(self, registry):
        with pytest.raises(ExpressionParseError, match="重复注册"):
            registry.register(FeatureDef(name="close", type=ExprType.PRICE))

    def test_feature_types_mapping(self, registry):
        assert registry.feature_types == {
            "close": ExprType.PRICE,
            "volume": ExprType.VOLUME,
        }

    def test_registry_parse(self, registry):
        expr = registry.parse("close + 1")
        assert isinstance(expr, Add)
        assert expr.output_type() is ExprType.PRICE

    def test_registry_parse_unknown_feature(self, registry):
        with pytest.raises(ExpressionParseError, match="未注册"):
            registry.parse("close + open")

    def test_allowed_operators_is_whitelist(self):
        assert "Add" in ALLOWED_OPERATORS
        assert "RollingQuantile" in ALLOWED_OPERATORS
        assert "Residualize" in ALLOWED_OPERATORS
        # 危险构造不在白名单
        for op in ("Eval", "Exec", "Import", "Getattr"):
            assert op not in ALLOWED_OPERATORS

    def test_feature_def_validation(self):
        with pytest.raises(ExpressionParseError):
            FeatureDef(name="close", type=ExprType.PRICE, min_window=0)
        with pytest.raises(ExpressionParseError):
            FeatureDef(name="close", type=ExprType.PRICE, min_window=5, max_window=3)
