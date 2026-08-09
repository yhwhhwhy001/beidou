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
    (r"\baccount_balance\s*=\s*\d{3,}", "fixed_account_balance", "硬编码账户余额: account_balance 固定值"),
    (r"\btotalWalletBalance.*\bdefault.*\d{3,}", "fixed_account_balance", "硬编码账户余额: totalWalletBalance 默认值"),
    # Fixed PnL: pnl = 0.0, is_win = True
    (r"\bp(_?)nl\s*=\s*0\.0", "fixed_pnl", "固定 PnL=0.0"),
    (r"\bis_win\s*=\s*True", "fixed_pnl", "固定 is_win=True （永远标记为盈利）"),
    # Fixed health PASS
    (r"\bhealthy\s*=\s*True\b", "fixed_health", "固定健康状态: healthy = True"),
    (r'\bstatus.*=.*"PASS".*#.*fixed', "fixed_health", "固定健康状态: status = 'PASS' (硬编码)"),
    # Direct approval: self._approval.sign() without checks
    (r"self\._approval\.sign\(", "direct_approval", "直接审批调用: self._approval.sign() — 可能存在绕过风险检查"),
    # Swallowed exceptions: except Exception: pass
    (r"except\s+Exception\s*:\s*pass", "swallowed_exception", "吞异常: except Exception: pass"),
    # except Exception as e: pass
    (r"except\s+Exception\s+as\s+\w+\s*:\s*pass", "swallowed_exception", "吞异常: except Exception as e: pass"),
    # Direct mainnet references in config/defaults
    (r"https://fapi\.binance\.com", "mainnet_url", "Mainnet URL 硬编码: fapi.binance.com"),
    (r"https://api\.binance\.com", "mainnet_url", "Mainnet URL 硬编码: api.binance.com"),
    # Skip in test files
    (r"@pytest\.mark\.skip\s*$", "skip_without_reason", "@pytest.mark.skip 无理由"),
]

# BD-P1-16: Allowlist — 仅保留已验证安全的条目。
# 禁止 Allowlist 已知 P0 问题。
# engine.py 的直接审批为 nearline-tick 内联签名（非 Gate 证书），
# 纳入 BD-P2-18 资本阶梯后进行独立 Gate Runner 替换。
_PATTERN_ALLOWLIST: set[tuple[str, str]] = set()

# 新增检测模式
_EXTRA_PATTERNS = [
    # 数据库密码明文
    (r":\/\/\w+:[\w!@#$%^&*()]+\@", "hardcoded_db_password", "数据库连接字符串包含明文密码"),
    # S3 桶名硬编码
    (r'bucket\s*=\s*"[a-z][a-z0-9-]+"', "hardcoded_s3_bucket", "S3 桶名硬编码"),
    # 绝对文件路径 (排除注释)
    (r'["\']/tmp/', "hardcoded_tmp_path", "硬编码 /tmp/ 路径"),
    (r'["\']/var/run/', "hardcoded_var_path", "硬编码 /var/run/ 路径"),
    # localhost + 端口模式
    (r"localhost:\d{4,5}", "hardcoded_localhost", "硬编码 localhost:PORT"),
    # 明文 API 密钥（在 YAML 中）
    (r"api_key_ref:\s*[A-Za-z0-9]{32,}", "plaintext_api_key", "api_key_ref 包含明文密钥值"),
    (r"api_secret_ref:\s*[A-Za-z0-9]{32,}", "plaintext_api_secret", "api_secret_ref 包含明文密钥值"),
]


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
                            self.findings.append(
                                HardcodedFinding(
                                    self.filepath,
                                    node.lineno,
                                    "fixed_account_balance",
                                    f"硬编码: {target.id} = {node.value.value} (固定数值)",
                                )
                            )

                # pnl = 0.0
                if target.id in ("pnl", "realized_pnl"):
                    if isinstance(node.value, ast.Constant) and node.value.value == 0.0:
                        self.findings.append(
                            HardcodedFinding(self.filepath, node.lineno, "fixed_pnl", f"固定 PnL: {target.id} = 0.0")
                        )

                # is_win = True
                if target.id == "is_win":
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        self.findings.append(
                            HardcodedFinding(self.filepath, node.lineno, "fixed_pnl", "固定 is_win = True")
                        )

        self.generic_visit(node)


def scan_file(filepath: str) -> list[HardcodedFinding]:
    """扫描单个 Python 文件。"""
    findings: list[HardcodedFinding] = []

    try:
        with open(filepath) as f:
            source = f.read()
    except (UnicodeDecodeError, PermissionError, IsADirectoryError) as e:
        findings.append(HardcodedFinding(filepath, 0, "syntax_error", f"Read error: {e}"))
        return findings

    # Regex-based scan
    lines = source.split("\n")
    all_patterns = HARDCODED_PATTERNS + _EXTRA_PATTERNS
    for pattern, category, message in all_patterns:
        for match in re.finditer(pattern, source, re.MULTILINE):
            line_no = source[: match.start()].count("\n") + 1
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
            findings.append(HardcodedFinding(filepath, line_no, category, f"{message}: '{match.group()[:60]}'"))

    # AST-based scan (skip test files for certain checks)
    if not filepath.endswith("__init__.py"):
        try:
            tree = ast.parse(source)
            scanner = HardcodedASTScanner(filepath)
            scanner.visit(tree)
            findings.extend(scanner.findings)
        except SyntaxError as e:
            findings.append(HardcodedFinding(filepath, e.lineno or 0, "syntax_error", f"SyntaxError: {e}"))

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
    exclude_dirs = {
        "__pycache__",
        ".git",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        "evidence",
        "tests",
        "templates",
        "node_modules",
        "dist",
        "build",
        "runbooks",
        "docs",
    }

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

    _BLOCKING_CATEGORIES = (
        "fixed_account_balance",
        "fixed_pnl",
        "fixed_health",
        "mainnet_url",
        "swallowed_exception",
        "syntax_error",
        "hardcoded_db_password",
        "plaintext_api_key",
        "plaintext_api_secret",
    )
    errors = [f for f in findings if f.category in _BLOCKING_CATEGORIES]

    if findings:
        for cat, cat_findings in sorted(categories.items()):
            for f in sorted(cat_findings, key=lambda x: (x.file, x.line)):
                severity = "ERROR" if f in errors else "WARN"
                print(f"{severity} [{cat}] {f.file}:{f.line}: {f.message}")
    else:
        return 0

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
