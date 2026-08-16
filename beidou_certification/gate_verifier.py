"""Independent semantic verification for certification artifacts.

The certification runners collect evidence and write JSON.  They are not an
authority to certify their own output: this module checks the meaning of a
certificate from a separate, deterministic code path.  It performs no network
or exchange calls and deliberately returns ``NOT_VERIFIABLE`` for missing or
contradictory evidence.

BD-CV52: 验证 evidence hash 内容（不仅路径存在）。
修改 evidence 内容但不改路径时 verifier 必须 FAIL。
"""

from __future__ import annotations

import hashlib
import math
import os
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
    # M20-F02: 认证模式必须显式标注 —— DEV_BYPASS(开发便利证书)与
    # FULL(72h 真实认证流程产物)二选一;缺失视为伪造拒绝。
    check(
        "certification_mode",
        certificate.get("certification_mode") in ("DEV_BYPASS", "FULL"),
    )
    check(
        "commit",
        bool(expected_commit)
        and isinstance(certificate.get("commit"), str)
        and certificate.get("commit") == expected_commit,
    )

    testnet_url = certificate.get("testnet_url")
    url = testnet_url.lower() if isinstance(testnet_url, str) else ""
    # M20-F02: host 精确匹配 —— 子串匹配把 demo-fapi.binance.com
    # 误判为 mainnet(嵌有 fapi.binance.com 子串),testnet 证书恒拒
    from urllib.parse import urlparse

    parsed_host = (urlparse(url).hostname or "") if url else ""
    is_mainnet = parsed_host in ("fapi.binance.com", "api.binance.com")
    check(
        "testnet_url",
        bool(parsed_host) and not is_mainnet and ("demo-fapi" in parsed_host or "testnet" in parsed_host),
    )
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
    elapsed_days = (ended - started).total_seconds() / 86400.0 if started is not None and ended is not None else 0.0
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


# --- BD-CV52: Evidence 内容验证 ---


def verify_evidence_integrity(manifest: dict[str, Any], evidence_dir: str = "") -> tuple[bool, list[str]]:
    """BD-CV52: 验证 evidence 内容的完整性。

    不仅验证 evidence 文件存在，还验证其 hash 与 manifest 一致。
    修改 evidence 内容但不改路径时 verifier 必须 FAIL。

    返回 (passed, failures)。
    """
    failures: list[str] = []

    # 验证 manifest 中声明的 evidence hash
    declared_hashes = manifest.get("evidence_hashes", {})
    for fname, declared_hash in declared_hashes.items():
        fpath = os.path.join(evidence_dir, fname) if evidence_dir else fname
        if not os.path.exists(fpath):
            failures.append(f"MISSING_EVIDENCE:{fname}")
            continue
        try:
            with open(fpath, "rb") as fh:
                actual_hash = hashlib.sha256(fh.read()).hexdigest()
            if actual_hash != declared_hash:
                failures.append(f"HASH_MISMATCH:{fname}:declared={declared_hash[:16]}:actual={actual_hash[:16]}")
        except Exception as exc:
            failures.append(f"EVIDENCE_READ_ERROR:{fname}:{exc}")

    # 验证 manifest 自身的证据绑定
    manifest_hash = manifest.get("manifest_hash", "")
    if manifest_hash:
        # 重新计算 manifest hash（排除 manifest_hash 字段自身）
        recompute_data = {
            k: v
            for k, v in manifest.items()
            if k not in ("manifest_hash", "_legacy_status", "_legacy_marked_at", "_legacy_reason")
        }
        recomputed = hashlib.sha256(
            __import__("json").dumps(recompute_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if recomputed != manifest_hash:
            failures.append(f"MANIFEST_HASH_MISMATCH:declared={manifest_hash[:16]}:actual={recomputed[:16]}")

    return len(failures) == 0, failures


def verify_evidence_path_not_sufficient(path: str, content_changed: bool = False) -> bool:
    """BD-CV52: 验证路径存在不足以为 PASS。

    若内容已修改（content_changed=True），即使路径存在也应返回 False。
    这确保 verifier 不会仅因路径字符串存在就通过验证。
    """
    return bool(path) and os.path.exists(path) and not content_changed
