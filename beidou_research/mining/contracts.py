"""BF-01: 点时数据与标签合同。

PredictionKey、LabelRecord、PredictionRecord、DatasetManifest —
因子研究与策略评估的核心数据合同。所有因子预测必须可追溯到
原始事件范围、数据可用时间和代码版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import (
    DataQualityTier,
    FactorId,
    InstrumentId,
    SchemaVersion,
    VenueId,
)


# ================================================================
# 时间粒度类型
# ================================================================

class TimeframeGranularity(str, Enum):
    """时间粒度枚举。不同粒度数据不得交叉读取。"""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"


class HorizonUnit(str, Enum):
    """标签持有期单位。"""
    BAR = "bar"           # 按 K 线数量
    HOUR = "hour"         # 按自然小时
    DAY = "day"           # 按自然天


class PriceType(str, Enum):
    """价格类型 — 用于标签计算的 entry/exit 价格。"""
    MID = "mid"
    MARK = "mark"
    CLOSE = "close"
    VWAP = "vwap"


class ReturnType(str, Enum):
    """收益类型。"""
    LOG = "log"
    SIMPLE = "simple"
    RESIDUAL = "residual"


class LabelQuality(str, Enum):
    """标签质量状态。"""
    VALID = "VALID"
    STALE = "STALE"               # 数据可用时间超过阈值
    OVERLAPPING = "OVERLAPPING"    # 与另一个标签时间重叠
    FUTURE_LEAK = "FUTURE_LEAK"   # 使用了未来数据
    MISSING_PRICE = "MISSING_PRICE"
    UNKNOWN = "UNKNOWN"


# ================================================================
# 核心合同
# ================================================================

@dataclass(frozen=True, slots=True)
class PredictionKey:
    """因子预测的唯一标识键。

    每个预测必须绑定到具体的 venue/symbol/timeframe/time/horizon/factor_version。
    多品种、多周期的数据不得交叉配对。

    Attributes:
        venue: 交易所标识
        symbol: 交易品种
        timeframe: K 线粒度（如 "1h", "5m"）
        prediction_time: 预测生成时刻
        data_available_time: 预测所用数据的最晚可用时间（≤ prediction_time）
        horizon: 预测持有期
        horizon_unit: 持有期单位
        factor_id: 因子标识
        factor_version: 因子代码版本
    """
    venue: VenueId
    symbol: InstrumentId
    timeframe: str
    prediction_time: datetime
    data_available_time: datetime
    horizon: int
    horizon_unit: HorizonUnit
    factor_id: FactorId
    factor_version: SchemaVersion

    def __post_init__(self) -> None:
        """确保 data_available_time ≤ prediction_time（点时约束）。"""
        if self.data_available_time > self.prediction_time:
            raise ValueError(
                f"data_available_time ({self.data_available_time}) must be <= "
                f"prediction_time ({self.prediction_time})"
            )

    def timeframe_matches(self, other: PredictionKey) -> bool:
        """不同 timeframe 的预测不得交叉配对。"""
        return self.timeframe == other.timeframe

    def symbol_matches(self, other: PredictionKey) -> bool:
        """不同 symbol 的预测不得交叉配对。"""
        return self.venue == other.venue and self.symbol == other.symbol

    def to_sort_key(self) -> tuple:
        """用于排序和分组的复合键。"""
        return (
            self.venue,
            self.symbol,
            self.timeframe,
            self.prediction_time,
            self.horizon,
            self.factor_id,
            self.factor_version,
        )


@dataclass(frozen=True, slots=True)
class LabelRecord:
    """标签记录 — 因子预测对应的真实标签。

    每个标签记录预测的目标值（forward return 等），必须明确：
    - 标签起止时间
    - 数据可用时间（标签值何时可知）
    - 成本模型版本
    - 标签质量状态

    Attributes:
        label_id: 标签唯一标识
        prediction_key: 关联的预测键
        label_start_time: 标签起始时间（通常 = prediction_time）
        label_end_time: 标签结束时间（start + horizon）
        label_available_time: 标签值变为可知的时间（≥ label_end_time）
        entry_price_type: 入场价格类型
        exit_price_type: 出场价格类型
        cost_model_version: 成本模型版本
        label_value: 标签值（如 forward log return）
        gross_return: 未扣成本的原始收益
        expected_cost_bps: 预期往返成本（bps）
        quality_status: 标签质量
        label_spec_hash: 标签规格的确定性哈希
        revision: 修订号（0 = 初始写入，>0 = 修订）
        metadata: 附加信息
    """
    label_id: str
    prediction_key: PredictionKey
    label_start_time: datetime
    label_end_time: datetime
    label_available_time: datetime
    entry_price_type: PriceType
    exit_price_type: PriceType
    cost_model_version: str
    label_value: float
    gross_return: float = 0.0
    expected_cost_bps: float = 0.0
    quality_status: LabelQuality = LabelQuality.VALID
    label_spec_hash: str = ""
    revision: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证标签时间顺序。"""
        if self.label_start_time >= self.label_end_time:
            raise ValueError(
                f"label_start_time ({self.label_start_time}) must be < "
                f"label_end_time ({self.label_end_time})"
            )
        if self.label_available_time < self.label_end_time:
            raise ValueError(
                f"label_available_time ({self.label_available_time}) must be >= "
                f"label_end_time ({self.label_end_time})"
            )

    def overlaps_with(self, other: LabelRecord) -> bool:
        """检测两个标签是否存在时间重叠。

        同一 symbol + timeframe 的标签，如果时间区间有交集则
        存在信息重叠，在构造 fold 时必须 purge。
        """
        if not self.prediction_key.symbol_matches(other.prediction_key):
            return False
        return (
            self.label_start_time < other.label_end_time
            and other.label_start_time < self.label_end_time
        )

    def is_valid_for_evaluation(self) -> bool:
        """检查标签是否可用于因子评估。"""
        return self.quality_status == LabelQuality.VALID

    def cost_adjusted_return(self) -> float:
        """返回扣除成本后的净收益。"""
        cost_decimal = self.expected_cost_bps / 10000.0
        return self.gross_return - cost_decimal


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """因子预测记录 — prediction_key + 预测值 + 版本追溯。

    Attributes:
        prediction_key: 预测键
        prediction_value: 预测值（因子输出）
        feature_manifest_hash: 输入特征清单哈希
        dataset_manifest_hash: 数据集清单哈希
        code_hash: 因子代码哈希
        parameter_hash: 因子参数哈希
        factor_expression_hash: 因子表达式规范哈希
        dq_tier: 输入数据的数据质量
        revision: 修订号
    """
    prediction_key: PredictionKey
    prediction_value: float
    feature_manifest_hash: str = ""
    dataset_manifest_hash: str = ""
    code_hash: str = ""
    parameter_hash: str = ""
    factor_expression_hash: str = ""
    dq_tier: DataQualityTier = DataQualityTier.UNKNOWN if hasattr(DataQualityTier, "UNKNOWN") else DataQualityTier.PASS
    revision: int = 0

    def can_be_evaluated(self) -> bool:
        """UNKNOWN 数据质量的预测不能用于评估。"""
        return self.dq_tier not in (
            getattr(DataQualityTier, "FAIL", "FAIL"),
        )

    def to_lineage(self) -> dict:
        """输出完整的溯源信息。"""
        return {
            "prediction_key": {
                "venue": self.prediction_key.venue,
                "symbol": self.prediction_key.symbol,
                "timeframe": self.prediction_key.timeframe,
                "prediction_time": self.prediction_key.prediction_time.isoformat(),
                "horizon": self.prediction_key.horizon,
                "factor_id": self.prediction_key.factor_id,
                "factor_version": self.prediction_key.factor_version,
            },
            "prediction_value": self.prediction_value,
            "feature_manifest_hash": self.feature_manifest_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "code_hash": self.code_hash,
            "parameter_hash": self.parameter_hash,
            "factor_expression_hash": self.factor_expression_hash,
            "dq_tier": str(self.dq_tier),
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """数据集清单 — 描述用于因子研究的冻结数据集。

    Attributes:
        manifest_id: 清单唯一标识
        manifest_hash: 清单内容的 SHA-256
        venue: 交易所
        symbols: 品种列表
        timeframe: K 线粒度
        start_time: 数据起始时间
        end_time: 数据结束时间
        feature_names: 包含的特征名称列表
        feature_versions: 特征版本映射
        source_description: 数据来源描述
        created_at: 清单创建时间
        is_frozen: 数据集是否已冻结（冻结后不可修改）
    """
    manifest_id: str
    manifest_hash: str
    venue: VenueId
    symbols: tuple[InstrumentId, ...]
    timeframe: str
    start_time: datetime
    end_time: datetime
    feature_names: tuple[str, ...]
    feature_versions: tuple[SchemaVersion, ...]
    source_description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_frozen: bool = True

    def contains(self, prediction_key: PredictionKey) -> bool:
        """检查预测键是否在此数据集范围内。"""
        return (
            prediction_key.venue == self.venue
            and prediction_key.symbol in self.symbols
            and prediction_key.timeframe == self.timeframe
            and self.start_time <= prediction_key.prediction_time <= self.end_time
        )


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """标签规格 — 描述如何构造标签。

    Attributes:
        label_id: 标签规格标识
        horizon_bars: 持有期（K 线数）
        horizon_unit: 持有期单位
        price_type: 标签使用的价格类型
        return_type: 收益计算方式
        cost_adjusted: 是否扣成本
        neutralization: 中性化处理列表
        embargo_bars: 禁运期（K 线数），防止标签重叠
        min_data_quality: 最低数据质量要求
    """
    label_id: str
    horizon_bars: int
    horizon_unit: HorizonUnit = HorizonUnit.BAR
    price_type: PriceType = PriceType.MID
    return_type: ReturnType = ReturnType.LOG
    cost_adjusted: bool = True
    neutralization: tuple[str, ...] = ()
    embargo_bars: int = 0
    min_data_quality: DataQualityTier = DataQualityTier.PASS

    def to_hash(self) -> str:
        """生成标签规格的确定性哈希。"""
        import hashlib
        import json
        content = json.dumps({
            "horizon_bars": self.horizon_bars,
            "horizon_unit": self.horizon_unit.value,
            "price_type": self.price_type.value,
            "return_type": self.return_type.value,
            "cost_adjusted": self.cost_adjusted,
            "neutralization": list(self.neutralization),
            "embargo_bars": self.embargo_bars,
            "min_data_quality": str(self.min_data_quality),
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]
