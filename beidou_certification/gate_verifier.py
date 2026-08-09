"""Independent semantic verification for certification artifacts.

The certification runners collect evidence and write JSON.  They are not an
authority to certify their own output: this module checks the meaning of a
certificate from a separate, deterministic code path.  It performs no network
or exchange calls and deliberately returns ``NOT_VERIFIABLE`` for missing or
contradictory evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class GateVerification:
    """Result of an independent certificate check."""

    gate: str
    status: str
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "PASS" and not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "passed": self.passed,
            "checks": dict(self.checks),
            "failures": list(self.failures),
        }


def _timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _result(gate: str, checks: dict[str, bool], failures: list[str]) -> GateVerification:
    return GateVerification(
        gate=gate,
        status="PASS" if not failures else "NOT_VERIFIABLE",
        checks=checks,
        failures=failures,
    )


def verify_g5_certificate(
    certificate: dict[str, Any],
    *,
    expected_commit: str,
    expected_scenarios: Iterable[str],
    now: datetime | None = None,
    max_notional_usdt: float = 20.0,
) -> GateVerification:
    """Verify the non-negotiable semantics of a real G5 Testnet certificate."""

    expected = set(expected_scenarios)
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        checks[name] = bool(condition)
        if not condition:
            failures.append(name)

    check("gate", certificate.get("gate") == "G5")
    check("status", certificate.get("status") == "PASS")
    check(
        "commit",
        bool(expected_commit)
        and isinstance(certificate.get("commit"), str)
        and certificate.get("commit") == expected_commit,
    )

    testnet_url = certificate.get("testnet_url")
    url = testnet_url.lower() if isinstance(testnet_url, str) else ""
    is_mainnet = any(host in url for host in ("fapi.binance.com", "api.binance.com"))
    check("testnet_url", bool(url) and not is_mainnet and ("demo-fapi" in url or "testnet" in url))
    check("mainnet_prohibited", certificate.get("mainnet_prohibited") is True)
    check("simulation", certificate.get("is_simulated") is False)
    check("evidence_hash", isinstance(certificate.get("evidence_hash"), str) and bool(certificate.get("evidence_hash")))

    reference_now = now.astimezone(timezone.utc) if now and now.tzinfo else (now or datetime.now(timezone.utc))
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    started = _timestamp(certificate.get("started_at"))
    ended = _timestamp(certificate.get("ended_at"))
    check("started_at", started is not None)
    check("ended_at", ended is not None)
    check("time_order", started is not None and ended is not None and started <= ended)
    check("ended_at_not_future", ended is not None and ended <= reference_now)

    summary = certificate.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    check("summary_warn", summary.get("warn") == 0)
    check("summary_fail", summary.get("fail") == 0)

    scenarios = certificate.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, dict) else {}
    actual = set(scenarios)
    check("scenario_set", actual == expected)
    for scenario in sorted(expected):
        entry = scenarios.get(scenario)
        status = entry.get("status") if isinstance(entry, dict) else None
        check(f"scenario_status:{scenario}", status == "PASS")
    check("summary_total", summary.get("total") == len(expected))
    check("summary_pass", summary.get("pass") == len(expected))

    account_access = certificate.get("account_access")
    account_access = account_access if isinstance(account_access, dict) else {}
    check("withdraw_permission", account_access.get("can_withdraw") is False)

    blockers = certificate.get("blockers", [])
    p0_failures = certificate.get("p0_failures", [])
    check("p0_blockers", not bool(blockers) and not bool(p0_failures))
    p0_count = summary.get("p0", summary.get("p0_incidents", 0))
    check("summary_p0", p0_count == 0)

    notional = certificate.get("max_notional_usdt", certificate.get("max_notional"))
    parsed_notional = _number(notional)
    check("max_notional", parsed_notional is not None and 0 <= parsed_notional <= max_notional_usdt)

    return _result("G5", checks, failures)


def verify_g7_certificate(
    certificate: dict[str, Any],
    *,
    expected_commit: str,
    expected_g5_hash: str,
    now: datetime | None = None,
    minimum_days: float = 30.0,
    minimum_samples: int = 200,
) -> GateVerification:
    """Verify a real, bound, minimum-window G7 unattended certificate."""

    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        checks[name] = bool(condition)
        if not condition:
            failures.append(name)

    check("gate", certificate.get("gate") == "G7")
    check("status", certificate.get("status") == "PASS")
    check(
        "commit",
        bool(expected_commit)
        and isinstance(certificate.get("commit"), str)
        and certificate.get("commit") == expected_commit,
    )
    check(
        "g5_binding",
        bool(expected_g5_hash)
        and isinstance(certificate.get("g5_certificate_hash"), str)
        and certificate.get("g5_certificate_hash") == expected_g5_hash,
    )
    check("evidence_hash", isinstance(certificate.get("evidence_hash"), str) and bool(certificate.get("evidence_hash")))
    check("mainnet_prohibited", certificate.get("mainnet_prohibited") is True)
    check("simulation", certificate.get("is_simulated") is False)

    disclaimer = certificate.get("disclaimer", "")
    disclaimer_text = disclaimer.lower() if isinstance(disclaimer, str) else ""
    check("disclaimer", "simulat" not in disclaimer_text and "fast-forward" not in disclaimer_text)

    reference_now = now.astimezone(timezone.utc) if now and now.tzinfo else (now or datetime.now(timezone.utc))
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    started = _timestamp(certificate.get("started_at"))
    ended = _timestamp(certificate.get("ended_at"))
    check("started_at", started is not None)
    check("ended_at", ended is not None)
    check("time_order", started is not None and ended is not None and started <= ended)
    check("ended_at_not_future", ended is not None and ended <= reference_now)
    elapsed_days = (
        (ended - started).total_seconds() / 86400.0
        if started is not None and ended is not None
        else 0.0
    )
    check("minimum_duration", elapsed_days >= minimum_days)

    summary = certificate.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    samples = _number(summary.get("total_sli_samples"))
    reports = _number(summary.get("daily_reports"))
    pass_rate = summary.get("sli_pass_rate")
    if isinstance(pass_rate, str) and pass_rate.endswith("%"):
        pass_rate = pass_rate[:-1]
    parsed_pass_rate = _number(pass_rate)
    check("minimum_samples", samples is not None and samples >= minimum_samples)
    check("minimum_reports", reports is not None and reports >= minimum_days)
    check("sli_pass_rate", parsed_pass_rate is not None and parsed_pass_rate >= 100.0)
    check("p0_incidents", summary.get("p0_incidents") == 0)
    check("resets", summary.get("resets") == 0)
    check("active_incidents", summary.get("active_incidents", 0) == 0)

    return _result("G7", checks, failures)
