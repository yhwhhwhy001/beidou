#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

PATTERNS = [
    ("default_secret", re.compile(r"beidou-(?:testnet-)?default-key|default[_-]?secret", re.I)),
    ("mainnet_url", re.compile(r"https?://(?:fapi|api)\.binance\.com", re.I)),
    ("swallowed_exception", re.compile(r"except\s+Exception(?:\s+as\s+\w+)?\s*:\s*(?:pass|continue)")),
]
NETWORK = re.compile(r"\b(?:urllib|requests|httpx|aiohttp)\b|/fapi/")
ALLOWED_NETWORK = (
    "beidou_exchange/",
    "tests/",
    # Alert webhooks and local backup URI quoting are not exchange order paths.
    "beidou_core/alerts.py",
    "beidou_infra/backup.py",
    # This daemon polls the local health endpoint only; it is not an exchange
    # transport and has no order/write endpoint.
    "scripts/monitor_daemon.py",
    # Database URL redaction/normalization parses strings only; it never opens
    # a network connection or bypasses the exchange adapter.
    "beidou_shared/config/__init__.py",
)
SELF_SCAN_FILES = {
    "delivery/scripts/check_forbidden_patterns.py",
    "scripts/scan_test_quality.py",
    "scripts/scan_hardcoded.py",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    p.add_argument("--strategy", action="store_true")
    a = p.parse_args()
    root = Path(a.repo)
    findings = []
    for f in root.rglob("*.py"):
        try:
            rel_path = f.relative_to(root)
        except ValueError:
            continue
        rel = rel_path.as_posix()
        # ``rglob`` returns paths without a leading slash, so substring checks
        # such as ``"/.venv/"`` never excluded the local virtualenv.  Exclude
        # directory components explicitly to keep the gate scoped to source.
        if any(part in {".git", ".venv", "__pycache__"} for part in rel_path.parts):
            continue
        try:
            s = f.read_text(encoding="utf-8")
        except Exception as e:
            findings.append((rel, 0, "read_error", str(e)))
            continue
        try:
            compile(s, rel, "exec")
        except SyntaxError as e:
            findings.append((rel, e.lineno or 0, "syntax_error", e.msg))
            continue
        # The scanners necessarily contain their own pattern strings and
        # examples; do not report the detector as a production violation.
        patterns = () if rel in SELF_SCAN_FILES else PATTERNS
        for name, pat in patterns:
            # Mainnet URLs are intentionally present in guard tests to prove
            # they are rejected.  They are not production defaults.
            if name == "mainnet_url" and "tests" in rel_path.parts:
                continue
            for m in pat.finditer(s):
                findings.append((rel, s[: m.start()].count("\n") + 1, name, m.group(0)[:80]))
        if (
            NETWORK.search(s)
            and rel not in SELF_SCAN_FILES
            and not any(rel.startswith(x) or "/" + x in rel for x in ALLOWED_NETWORK)
        ):
            findings.append((rel, 1, "network_bypass", "network/endpoint outside exchange adapter"))
    if findings:
        for rel, lineno, rule, detail in findings:
            print(f"❌ {rel}:{lineno}  [{rule}]  {detail}", flush=True)  # noqa: T201
        print(f"\n{len(findings)} forbidden pattern(s) found.  Delivery gate FAILED.")  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
