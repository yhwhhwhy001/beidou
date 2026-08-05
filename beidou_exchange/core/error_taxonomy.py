"""交易所错误分类体系 — 将各交易所专有错误码归一化为统一语义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from beidou_shared.errors import ErrorCategory, FaultSeverity, RecoveryAction, DomainError

# 重新导出 — 供 exchange adapter 层统一导入
__all__ = [
    "ErrorCategory",
    "AdapterError",
    "Result",
    "classify_http_error",
    "ErrorNormalizer",
]

T = TypeVar("T")


@dataclass
class AdapterError(Exception):
    """交易所适配器统一错误类型。

    封装原始异常、HTTP 状态码和归一化分类。
    """
    message: str
    http_status: int = 0
    category: ErrorCategory = ErrorCategory.UNKNOWN
    retryable: bool = False
    raw: Any = None

    def __str__(self) -> str:
        return f"[{self.category.value}] HTTP {self.http_status}: {self.message}"


@dataclass
class Result(Generic[T]):
    """Result 模式 — 适配器操作的统一返回类型。

    ok=True 时 data 有效；ok=False 时 error 有效。
    """
    ok: bool
    data: T | None = None
    error: AdapterError | None = None

    @classmethod
    def success(cls, data: T) -> "Result[T]":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, message: str, http_status: int = 0,
                category: ErrorCategory = ErrorCategory.UNKNOWN,
                retryable: bool = False, raw: Any = None) -> "Result[T]":
        return cls(
            ok=False,
            error=AdapterError(
                message=message,
                http_status=http_status,
                category=category,
                retryable=retryable,
                raw=raw,
            ),
        )


def classify_http_error(http_status: int, response_body: str = "") -> tuple[ErrorCategory, bool]:
    """根据 HTTP 状态码分类错误。

    Returns:
        (ErrorCategory, retryable)
    """
    if http_status == 429:
        return (ErrorCategory.RATE_LIMIT, True)
    if http_status == 418:
        return (ErrorCategory.RATE_LIMIT, True)
    if http_status in (401, 403):
        return (ErrorCategory.AUTH_FAILURE, False)
    if http_status >= 500:
        return (ErrorCategory.EXCHANGE_UNAVAILABLE, True)
    if http_status >= 400:
        return (ErrorCategory.ORDER_REJECTED, False)
    return (ErrorCategory.UNKNOWN, False)


class ErrorNormalizer:
    """错误语义归一化器。

    将不同交易所的专有错误码映射到统一的 ErrorCategory。
    未知错误默认归类为 UNKNOWN → P0_CRITICAL → MANUAL。
    """

    _NORMALIZATION_MAP: dict[str, dict[int, tuple[ErrorCategory, FaultSeverity, RecoveryAction]]] = {
        "BINANCE": {
            -1013: (ErrorCategory.RATE_LIMIT, FaultSeverity.P1_MAJOR, RecoveryAction.RETRY),
            -1021: (ErrorCategory.TIMEOUT, FaultSeverity.P1_MAJOR, RecoveryAction.RETRY),
            -2010: (ErrorCategory.INSUFFICIENT_BALANCE, FaultSeverity.P0_CRITICAL, RecoveryAction.NOOP),
            -2011: (ErrorCategory.ORDER_REJECTED, FaultSeverity.P1_MAJOR, RecoveryAction.NOOP),
            -2015: (ErrorCategory.AUTH_FAILURE, FaultSeverity.P0_CRITICAL, RecoveryAction.LOCK),
            -2019: (ErrorCategory.INSUFFICIENT_MARGIN, FaultSeverity.P0_CRITICAL, RecoveryAction.NOOP),
            -2021: (ErrorCategory.ORDER_REJECTED, FaultSeverity.P1_MAJOR, RecoveryAction.NOOP),
            -2022: (ErrorCategory.POSITION_LIMIT, FaultSeverity.P0_CRITICAL, RecoveryAction.DEGRADE),
            -4061: (ErrorCategory.RATE_LIMIT, FaultSeverity.P1_MAJOR, RecoveryAction.RETRY),
        },
    }

    @classmethod
    def normalize(
        cls, venue_id: str, error_code: int, raw_message: str,
        correlation_id: str | None = None,
    ) -> DomainError:
        venue_map = cls._NORMALIZATION_MAP.get(venue_id.upper(), {})
        normalized = venue_map.get(error_code)
        if normalized is None:
            return DomainError(
                category=ErrorCategory.UNKNOWN,
                severity=FaultSeverity.P0_CRITICAL,
                message=f"Unclassified exchange error [{venue_id}:{error_code}]: {raw_message}",
                correlation_id=correlation_id,
                venue_id=venue_id,
                retryable=False,
                recommended_action=RecoveryAction.MANUAL,
                raw_response={"error_code": error_code, "message": raw_message},
            )
        return DomainError(
            category=normalized[0],
            severity=normalized[1],
            message=raw_message,
            correlation_id=correlation_id,
            venue_id=venue_id,
            retryable=normalized[2] == RecoveryAction.RETRY,
            recommended_action=normalized[2],
            raw_response={"error_code": error_code, "message": raw_message},
        )

    @classmethod
    def register_venue_errors(
        cls, venue_id: str,
        mapping: dict[int, tuple[ErrorCategory, FaultSeverity, RecoveryAction]],
    ) -> None:
        venue_id = venue_id.upper()
        if venue_id not in cls._NORMALIZATION_MAP:
            cls._NORMALIZATION_MAP[venue_id] = {}
        cls._NORMALIZATION_MAP[venue_id].update(mapping)
