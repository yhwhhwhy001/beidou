#!/usr/bin/env python3
"""硬编码扫描器 — 检测固定账户余额、固定 PnL、固定健康 PASS、直接审批、吞异常。

用法: python scripts/scan_hardcoded.py beidou_*
"""

from __future__ import annotations

import ast
import os
import re
import sys
from typing import NamedTuple


class HardcodedFinding(NamedTuple):
    file: str
    line: int
    category: str
    message: str


# 硬编码模式
HARDCODED_PATTERNS = [
    # Fixed account balance: account_balance = 1000, balance = 10000.0
    (r'\baccount_balance\s*=\s*\d{3,}', "fixed_account_balance",
     "硬编码账户余额: account_balance 固定值"),
    (r'\btotalWalletBalance.*\bdefault.*\d{3,}', "fixed_account_balance",
     "硬编码账户余额: totalWalletBalance 默认值"),

    # Fixed PnL: pnl = 0.0, is_win = True
    (r'\bp(_?)nl\s*=\s*0\.0', "fixed_pnl",
     "固定 PnL=0.0"),
    (r'\bis_win\s*=\s*True', "fixed_pnl",
     "固定 is_win=True （永远标记为盈利）"),

    # Fixed health PASS
    (r'\bhealthy\s*=\s*True\b', "fixed_health",
     "固定健康状态: healthy = True"),
    (r'\bstatus.*=.*"PASS".*#.*fixed', "fixed_health",
     "固定健康状态: status = 'PASS' (硬编码)"),

    # Direct approval: self._approval.sign() without checks
    (r'self\._approval\.sign\(', "direct_approval",
     "直接审批调用: self._approval.sign() — 可能存在绕过风险检查"),

    # Swallowed exceptions: except Exception: pass
    (r'except\s+Exception\s*:\s*pass', "swallowed_exception",
     "吞异常: except Exception: pass"),

    # except Exception as e: pass
    (r'except\s+Exception\s+as\s+\w+\s*:\s*pass', "swallowed_exception",
     "吞异常: except Exception as e: pass"),

    # Direct mainnet references in config/defaults
    (r'https://fapi\.binance\.com', "mainnet_url",
     "Mainnet URL 硬编码: fapi.binance.com"),
    (r'https://api\.binance\.com', "mainnet_url",
     "Mainnet URL 硬编码: api.binance.com"),

    # Skip in test files
    (r'@pytest\.mark\.skip\s*$', "skip_without_reason",
     "@pytest.mark.skip 无理由"),
]

# Allowlist: (file_suffix, category) pairs that are verified safe
_PATTERN_ALLOWLIST: set[tuple[str, str]] = {
    ("beidou_core/engine.py", "direct_approval"),
}


class HardcodedASTScanner(ast.NodeVisitor):
    """AST-based scanner for additional hardcoded patterns."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.findings: list[HardcodedFinding] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        # Check for direct constant assignments that look suspicious
        for target in node.targets:
            if isinstance(target, ast.Name):
                # account_balance = <constant number like 1000, 10000>
                if target.id in ("account_balance", "init_equity", "balance"):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                        if node.value.value > 0:
                            self.findings.append(HardcodedFinding(
                                self.filepath, node.lineno, "fixed_account_balance",
                                f"硬编码: {target.id} = {node.value.value} (固定数值)"
                            ))

                # pnl = 0.0
                if target.id in ("pnl", "realized_pnl"):
                    if isinstance(node.value, ast.Constant) and node.value.value == 0.0:
                        self.findings.append(HardcodedFinding(
                            self.filepath, node.lineno, "fixed_pnl",
                            f"固定 PnL: {target.id} = 0.0"
                        ))

                # is_win = True
                if target.id == "is_win":
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        self.findings.append(HardcodedFinding(
                            self.filepath, node.lineno, "fixed_pnl",
                            "固定 is_win = True"
                        ))

        self.generic_visit(node)


def scan_file(filepath: str) -> list[HardcodedFinding]:
    """扫描单个 Python 文件。"""
    findings: list[HardcodedFinding] = []

    try:
        with open(filepath) as f:
            source = f.read()
    except (UnicodeDecodeError, PermissionError, IsADirectoryError):
        return findings

    # Regex-based scan
    lines = source.split("\n")
    for pattern, category, message in HARDCODED_PATTERNS:
        for match in re.finditer(pattern, source, re.MULTILINE):
            line_no = source[:match.start()].count("\n") + 1
            # Skip comments (lines starting with # or inside docstrings)
            if line_no <= len(lines):
                stripped = lines[line_no - 1].strip()
                if stripped.startswith("#"):
                    continue
            # Skip if this is in a test file for certain patterns
            if "test" in filepath.lower() and category in ("direct_approval",):
                continue
            # Skip allowlisted (file_suffix, category) pairs
            if any(filepath.endswith(suffix) and cat == category for suffix, cat in _PATTERN_ALLOWLIST):
                continue
            findings.append(HardcodedFinding(
                filepath, line_no, category, f"{message}: '{match.group()[:60]}'"
            ))

    # AST-based scan (skip test files for certain checks)
    if not filepath.endswith("__init__.py"):
        try:
            tree = ast.parse(source)
            scanner = HardcodedASTScanner(filepath)
            scanner.visit(tree)
            findings.extend(scanner.findings)
        except SyntaxError:
            pass

    # Deduplicate by (file, line, category)
    seen = set()
    deduped: list[HardcodedFinding] = []
    for f in findings:
        key = (f.file, f.line, f.category)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    return deduped


def scan_directories(package_dirs: list[str]) -> list[HardcodedFinding]:
    """递归扫描包目录。"""
    all_findings: list[HardcodedFinding] = []
    exclude_dirs = {"__pycache__", ".git", ".venv", ".pytest_cache",
                    ".mypy_cache", "evidence", "tests", "templates",
                    "node_modules", "dist", "build", "runbooks", "docs"}

    for pkg_dir in package_dirs:
        if not os.path.isdir(pkg_dir):
            continue
        for root, dirs, files in os.walk(pkg_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for fname in files:
                if fname.endswith(".py"):
                    fpath = os.path.join(root, fname)
                    all_findings.extend(scan_file(fpath))

    return all_findings


def main() -> int:
    if len(sys.argv) < 2:
        # Default: all beidou_ packages
        import glob
        package_dirs = sorted(glob.glob("beidou_*"))
    else:
        package_dirs = sys.argv[1:]

    findings = scan_directories(package_dirs)

    # Categorize
    categories: dict[str, list[HardcodedFinding]] = {}
    for f in findings:
        categories.setdefault(f.category, []).append(f)

    errors = [f for f in findings
              if f.category in ("fixed_account_balance", "fixed_pnl",
                                "fixed_health", "mainnet_url", "swallowed_exception")]

    if findings:
        print(f"\n=== Hardcoded Value Scan: {len(findings)} issues ({len(errors)} blocking) ===")
        for cat, cat_findings in sorted(categories.items()):
            blocking = "❌" if cat in ("fixed_account_balance", "fixed_pnl",
                                       "fixed_health", "mainnet_url") else "⚠️"
            print(f"\n  [{cat}] {len(cat_findings)} findings:")
            for f in sorted(cat_findings, key=lambda x: (x.file, x.line)):
                print(f"    {blocking} {f.file}:{f.line}: {f.message}")
    else:
        print("✅ Hardcoded scan: no issues found")
        return 0

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
