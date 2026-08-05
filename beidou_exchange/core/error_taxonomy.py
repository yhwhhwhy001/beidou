"""交易所错误分类体系 — 将各交易所专有错误码归一化为统一语义。"""
from __future__ import annotations
from beidou_shared.errors import ErrorCategory, FaultSeverity, RecoveryAction, DomainError


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
