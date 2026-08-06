#!/usr/bin/env python3
"""测试质量扫描器 — 拒绝永真断言、无断言测试、广泛 skip、mock 生产路径。

用法: python scripts/scan_test_quality.py tests/
"""

from __future__ import annotations

import ast
import os
import sys
from typing import NamedTuple


class Finding(NamedTuple):
    file: str
    line: int
    severity: str
    message: str


class TestQualityScanner(ast.NodeVisitor):
    """AST-based scanner for low-quality test patterns."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.findings: list[Finding] = []
        self._current_function: str | None = None
        self._has_assert: bool = False
        self._assert_count: int = 0
        self._try_except_pass_count: int = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Check for test functions
        is_test = node.name.startswith("test_")
        if is_test:
            self._current_function = node.name
            self._has_assert = False
            self._assert_count = 0

            # BD-T15 fix: Check for @pytest.mark.skip without reason (was dead code in visit_ImportFrom)
            if hasattr(node, "decorator_list"):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        if isinstance(dec.func, ast.Attribute) and dec.func.attr == "skip":
                            has_reason = any(k.arg == "reason" for k in dec.keywords) if dec.keywords else False
                            if not has_reason:
                                self.findings.append(
                                    Finding(self.filepath, node.lineno, "WARNING", "@pytest.mark.skip without reason")
                                )

        self.generic_visit(node)

        if is_test:
            # Check if this test uses alternative assertion mechanisms
            has_valid_test = False
            for child in ast.walk(node):
                # pytest.raises() context manager
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Attribute) and child.func.attr == "raises":
                        has_valid_test = True
                        break
                # raise AssertionError is a valid assertion
                if isinstance(child, ast.Raise):
                    if (
                        isinstance(child.exc, ast.Call)
                        and isinstance(child.exc.func, ast.Name)
                        and child.exc.func.id in ("AssertionError", "ArchitectureViolation")
                    ):
                        has_valid_test = True
                        break
                # with pytest.raises(...): is also valid
                if isinstance(child, ast.With):
                    for item in child.items:
                        if isinstance(item.context_expr, ast.Call):
                            if (
                                isinstance(item.context_expr.func, ast.Attribute)
                                and item.context_expr.func.attr == "raises"
                            ):
                                has_valid_test = True
                                break

            if not self._has_assert and not has_valid_test:
                self.findings.append(
                    Finding(
                        self.filepath,
                        node.lineno,
                        "ERROR",
                        f"Test function '{node.name}' has no assertions — not a valid test",
                    )
                )
            self._current_function = None

    def visit_Assert(self, node: ast.Assert) -> None:
        self._has_assert = True
        self._assert_count += 1

        # Check for vacuous assertions
        if isinstance(node.test, ast.Constant):
            self.findings.append(
                Finding(
                    self.filepath,
                    node.lineno,
                    "ERROR",
                    f"Vacuous assertion: 'assert {ast.unparse(node.test)}' is always True/False",
                )
            )

        # Check for assert len(x) >= 0 (always true)
        if isinstance(node.test, ast.Compare):
            if (
                isinstance(node.test.left, ast.Call)
                and isinstance(node.test.left.func, ast.Name)
                and node.test.left.func.id == "len"
            ):
                for op, comp in zip(node.test.ops, node.test.comparators):
                    if isinstance(op, ast.GtE) and isinstance(comp, ast.Constant) and comp.value == 0:
                        self.findings.append(
                            Finding(
                                self.filepath,
                                node.lineno,
                                "ERROR",
                                "Vacuous assertion: 'assert len(x) >= 0' is always True",
                            )
                        )
                    if isinstance(op, ast.Gt) and isinstance(comp, ast.Constant) and comp.value == -1:
                        self.findings.append(
                            Finding(
                                self.filepath,
                                node.lineno,
                                "ERROR",
                                "Vacuous assertion: 'assert len(x) > -1' is always True",
                            )
                        )

        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        # Check for except Exception: pass (swallowed exceptions)
        for handler in node.handlers:
            if handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"):
                if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                    self.findings.append(
                        Finding(
                            self.filepath,
                            handler.lineno,
                            "ERROR",
                            "'except Exception: pass' — swallowed exception, not allowed",
                        )
                    )
        self.generic_visit(node)

    # BD-T15: Mock production path check
    def visit_Call(self, node: ast.Call) -> None:
        """检测 mock.patch 替换生产路径的模式。"""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "patch" or node.func.attr == "patch_object":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if any(
                            p in arg.value
                            for p in ("beidou_core", "beidou_safety", "beidou_exchange", "beidou_strategy")
                        ):
                            self.findings.append(
                                Finding(
                                    self.filepath,
                                    node.lineno,
                                    "WARNING",
                                    f"mock.patch replaces production path: {arg.value}",
                                )
                            )
        self.generic_visit(node)


def scan_file(filepath: str) -> list[Finding]:
    """扫描单个测试文件。"""
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source)
        scanner = TestQualityScanner(filepath)
        scanner.visit(tree)
        return scanner.findings
    except SyntaxError as e:
        return [Finding(filepath, e.lineno or 0, "ERROR", f"Syntax error: {e}")]
    except Exception as e:
        return [Finding(filepath, 0, "ERROR", f"Failed to parse: {e}")]


def scan_directory(test_dir: str) -> list[Finding]:
    """递归扫描测试目录。"""
    all_findings: list[Finding] = []
    for root, dirs, files in os.walk(test_dir):
        # Skip __pycache__
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if fname.startswith("test_") and fname.endswith(".py"):
                fpath = os.path.join(root, fname)
                all_findings.extend(scan_file(fpath))
    return all_findings


def main() -> int:
    if len(sys.argv) < 2:
        test_dir = "tests"
    else:
        test_dir = sys.argv[1]

    if not os.path.isdir(test_dir):
        print(f"ERROR: {test_dir} is not a directory")
        return 1

    findings = scan_directory(test_dir)
    errors = [f for f in findings if f.severity == "ERROR"]
    warnings = [f for f in findings if f.severity == "WARNING"]

    if findings:
        print(f"\n=== Test Quality Scan: {len(findings)} issues ({len(errors)} errors, {len(warnings)} warnings) ===")
        for f in sorted(findings, key=lambda x: (x.file, x.line)):
            prefix = "❌" if f.severity == "ERROR" else "⚠️"
            print(f"  {prefix} {f.file}:{f.line}: {f.message}")
    else:
        print("✅ Test quality scan: no issues found")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
