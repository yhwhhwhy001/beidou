"""Independent, conservative oracle for the write-capability registry.

This verifier intentionally does not import ``beidou_launcher.write_registry``.
It uses a separate AST/lexical implementation so the primary discoverer cannot
prove its own completeness.
"""

from __future__ import annotations

import argparse
import ast
import json
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONSTRUCTORS = {"AutonomousEngine", "BinanceRESTClient", "BinanceUsdmAdapter"}
TERMINAL_NAMES = {
    "create_order",
    "cancel_order",
    "create_algo_order",
    "cancel_algo_order",
    "_create_algo_order",
    "_cancel_algo_order",
    "_cancel_algo_orders",
    "_submit_order_slice",
    "execute_action",
}
REQUEST_NAMES = {"request", "_request", "exchange", "_api", "_api_async", "_api_async_safe"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SKIP_PARTS = {".git", ".venv", "__pycache__", "build", "dist", "docs", "tests"}


@dataclass(frozen=True, slots=True)
class OracleFinding:
    path: str
    kind: str
    line: int
    detail: str


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


class _OracleVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.findings: list[OracleFinding] = []

    def _record(self, node: ast.AST, kind: str, detail: str) -> None:
        finding = OracleFinding(self.path, kind, int(getattr(node, "lineno", 0)), detail)
        if finding not in self.findings:
            self.findings.append(finding)

    def _target(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, "")
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call) and _name(node.func) == "getattr":
            value = _constant_string(node.args[1]) if len(node.args) > 1 else None
            return value or "UNRESOLVED_GETATTR"
        return ""

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
                self.aliases[alias.asname or alias.name] = alias.name

    def visit_Assign(self, node: ast.Assign) -> None:
        target = self._target(node.value)
        if target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
            for assigned in node.targets:
                if isinstance(assigned, ast.Name):
                    self.aliases[assigned.id] = target
                elif isinstance(assigned, (ast.Attribute, ast.Subscript)):
                    self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"stored:{target}")
        elif isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            values = node.value.values if isinstance(node.value, ast.Dict) else node.value.elts
            if any(self._target(value) in TERMINAL_NAMES | REQUEST_NAMES for value in values):
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", "container-dispatch")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self._target(node.func) or _name(node.func)
        if target in CONSTRUCTORS:
            self._record(node, "RUNTIME_CONSTRUCTOR", target)
        if target in TERMINAL_NAMES:
            if target == "execute_action":
                action = _name(node.args[0]) if node.args else ""
                if not action and node.args:
                    action = _constant_string(node.args[0]) or "DYNAMIC"
                if action in {"RESUME", "DYNAMIC"}:
                    self._record(node, "TERMINAL_CALL", f"{target}:{action}")
            else:
                self._record(node, "TERMINAL_CALL", target)
        if target == "UNRESOLVED_GETATTR":
            self._record(node, "UNRESOLVED_WRITE_CALLABLE", target)
        if isinstance(node.func, ast.Subscript):
            self._record(node, "UNRESOLVED_WRITE_CALLABLE", "subscript-dispatch")

        if target in REQUEST_NAMES:
            method_node: ast.AST | None = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "method"),
                None,
            )
            if method_node is None:
                position = 1 if target.startswith("_api") else 0
                method_node = node.args[position] if len(node.args) > position else None
            method_value = _constant_string(method_node) if method_node is not None else None
            method = method_value.upper() if method_value else "DYNAMIC"
            if method in WRITE_METHODS or method == "DYNAMIC":
                self._record(node, "TERMINAL_CALL", f"{target}:{method}")

        if _name(node.func) == "partial" and node.args:
            partial_target = self._target(node.args[0])
            if partial_target in TERMINAL_NAMES | REQUEST_NAMES:
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"partial:{partial_target}")

        if isinstance(node.func, ast.Attribute) and node.func.attr.lower() in {"post", "put", "patch", "delete"}:
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in {"requests", "httpx", "aiohttp"}:
                self._record(node, "DIRECT_HTTP_WRITE", node.func.attr.upper())
            elif isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute):
                if isinstance(base.func.value, ast.Name) and base.func.value.id in {"requests", "httpx", "aiohttp"}:
                    self._record(node, "DIRECT_HTTP_WRITE", node.func.attr.upper())
        self.generic_visit(node)


def scan_repository(root: Path) -> list[OracleFinding]:
    root = root.resolve()
    findings: list[OracleFinding] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if relative.as_posix() in {
            "beidou_launcher/write_registry.py",
            "scripts/verify_write_registry.py",
        }:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
            continue
        visitor = _OracleVisitor(relative.as_posix())
        visitor.visit(tree)
        findings.extend(visitor.findings)

    for path in root.rglob("*.sh"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            if re.search(r"\bcurl\b", line) and re.search(r"(?:-X|--request)(?:=|\s+)", line):
                findings.append(OracleFinding(relative.as_posix(), "SHELL_HTTP_WRITE", line_number, "curl"))
    for path in root.rglob("*.plist"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload.get("ProgramArguments", []) if isinstance(payload, dict) else []
            if isinstance(arguments, list) and arguments and str(arguments[0]).rsplit("/", 1)[-1] == "curl":
                findings.append(OracleFinding(relative.as_posix(), "PLIST_HTTP_WRITE", 0, "curl"))
        except (OSError, plistlib.InvalidFileException) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
    makefile = root / "Makefile"
    if makefile.is_file():
        for line_number, line in enumerate(makefile.read_text(encoding="utf-8").splitlines(), start=1):
            if line.startswith("\t") and re.search(r"\bcurl\b", line):
                findings.append(OracleFinding("Makefile", "MAKE_HTTP_WRITE", line_number, "curl"))
    return sorted(findings, key=lambda item: (item.path, item.line, item.kind, item.detail))


def verify_coverage(root: Path, registry: dict[str, Any]) -> list[str]:
    entries = {item.get("path"): item for item in registry.get("entries", []) if isinstance(item, dict)}
    terminal_paths = [
        item.get("source", "") for item in registry.get("terminal_write_paths", []) if isinstance(item, dict)
    ]
    issues: list[str] = []
    for finding in scan_repository(root):
        entry = entries.get(finding.path)
        has_terminal_record = any(source.startswith(finding.path + "::") for source in terminal_paths)
        if finding.kind == "RUNTIME_CONSTRUCTOR":
            covered = isinstance(entry, dict) and entry.get("status") == "HARD_HOLD"
        else:
            covered = has_terminal_record or (isinstance(entry, dict) and entry.get("status") == "HARD_HOLD")
        if not covered:
            issues.append(f"INDEPENDENT_ORACLE_UNCOVERED:{finding.path}:{finding.line}:{finding.kind}:{finding.detail}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    registry_path = args.registry or args.root / "config" / "write-capability-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    issues = verify_coverage(args.root, registry)
    print(json.dumps({"status": "PASS" if not issues else "FAIL", "issues": issues}, sort_keys=True))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
