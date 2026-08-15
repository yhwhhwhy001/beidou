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


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class _OracleVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.module_aliases: dict[str, str] = {}
        self.network_clients: set[str] = set()
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
        module_root = str(node.module or "").split(".", 1)[0]
        for alias in node.names:
            if alias.name in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
                self.aliases[alias.asname or alias.name] = alias.name
            if module_root in {"httpx", "requests", "aiohttp"}:
                imported_name = alias.asname or alias.name
                if alias.name.lower() in {"post", "put", "patch", "delete"}:
                    self.aliases[imported_name] = f"NETWORK_METHOD:{alias.name.upper()}"
                elif alias.name in {"Client", "AsyncClient", "ClientSession"}:
                    self.aliases[imported_name] = f"NETWORK_CLIENT:{module_root}"
            if node.module == "urllib" and alias.name == "request":
                self.module_aliases[alias.asname or alias.name] = "urllib.request"
            if node.module == "urllib.request" and alias.name in {"Request", "urlopen"}:
                self.aliases[alias.asname or alias.name] = f"URLLIB:{alias.name}"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module_root = alias.name.split(".", 1)[0]
            if module_root in {"aiohttp", "asyncio", "httpx", "requests", "socket", "urllib"}:
                self.module_aliases[alias.asname or module_root] = alias.name

    def visit_Assign(self, node: ast.Assign) -> None:
        target = self._target(node.value)
        if target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
            for assigned in node.targets:
                if isinstance(assigned, ast.Name):
                    self.aliases[assigned.id] = target
                elif isinstance(assigned, (ast.Attribute, ast.Subscript)):
                    self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"stored:{target}")
        elif isinstance(node.value, ast.Call) and isinstance(node.value.func, (ast.Name, ast.Attribute)):
            constructor_name = _name(node.value.func)
            constructor_base = node.value.func.value if isinstance(node.value.func, ast.Attribute) else None
            base_name = constructor_base.id if isinstance(constructor_base, ast.Name) else ""
            is_network_constructor = target.startswith("NETWORK_CLIENT:") or (
                constructor_name in {"Client", "AsyncClient", "ClientSession"}
                and base_name in self.module_aliases
            )
            if is_network_constructor:
                for assigned in node.targets:
                    if isinstance(assigned, ast.Name):
                        self.network_clients.add(assigned.id)
        elif isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            values = node.value.values if isinstance(node.value, ast.Dict) else node.value.elts
            if any(self._target(value) in TERMINAL_NAMES | REQUEST_NAMES for value in values):
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", "container-dispatch")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            target = self._target(node.value)
            if target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"returned:{target}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self._target(node.func) or _name(node.func)
        if target.startswith("NETWORK_METHOD:"):
            self._record(node, "DIRECT_HTTP_WRITE", target.partition(":")[2])
        if target == "URLLIB:Request":
            method_node = next((keyword.value for keyword in node.keywords if keyword.arg == "method"), None)
            method_value = _constant_string(method_node) if method_node is not None else None
            has_data = any(keyword.arg == "data" for keyword in node.keywords) or len(node.args) >= 2
            method = method_value.upper() if method_value else ("POST" if has_data else "DYNAMIC")
            if method in WRITE_METHODS or method == "DYNAMIC":
                self._record(node, "DIRECT_URLLIB_REQUEST", method)
        if target == "URLLIB:urlopen":
            self._record(node, "NETWORK_TRANSPORT_CALL", "urllib.urlopen")
        dotted = _dotted_name(node.func)
        dotted_root = dotted.split(".", 1)[0]
        imported_module = self.module_aliases.get(dotted_root, "")
        resolved_dotted = dotted.replace(dotted_root, imported_module, 1) if imported_module else dotted
        if resolved_dotted in {"asyncio.open_connection", "socket.socket"}:
            self._record(node, "NETWORK_STREAM_CALL", resolved_dotted)
        if dotted.endswith(".Request") and imported_module.startswith("urllib"):
            method_node = next((keyword.value for keyword in node.keywords if keyword.arg == "method"), None)
            method_value = _constant_string(method_node) if method_node is not None else None
            has_data = any(keyword.arg == "data" for keyword in node.keywords) or len(node.args) >= 2
            method = method_value.upper() if method_value else ("POST" if has_data else "DYNAMIC")
            if method in WRITE_METHODS or method == "DYNAMIC":
                self._record(node, "DIRECT_URLLIB_REQUEST", method)
        if dotted.endswith(".urlopen") and imported_module.startswith("urllib"):
            self._record(node, "NETWORK_TRANSPORT_CALL", "urllib.urlopen")
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
            request_method_node: ast.AST | None = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "method"),
                None,
            )
            if request_method_node is None:
                position = 1 if target.startswith("_api") else 0
                request_method_node = node.args[position] if len(node.args) > position else None
            method_value = _constant_string(request_method_node) if request_method_node is not None else None
            method = method_value.upper() if method_value else "DYNAMIC"
            if method in WRITE_METHODS or method == "DYNAMIC":
                self._record(node, "TERMINAL_CALL", f"{target}:{method}")

        if _name(node.func) == "partial" and node.args:
            partial_target = self._target(node.args[0])
            if partial_target in TERMINAL_NAMES | REQUEST_NAMES:
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"partial:{partial_target}")

        if _name(node.func) == "setattr" and len(node.args) >= 3:
            stored_target = self._target(node.args[2])
            if stored_target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"setattr:{stored_target}")

        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            argument_target = self._target(argument)
            if argument_target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES:
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"callback:{argument_target}")

        if isinstance(node.func, ast.Attribute) and node.func.attr.lower() in {"post", "put", "patch", "delete"}:
            base = node.func.value
            if isinstance(base, ast.Name) and (
                base.id in self.network_clients or self.module_aliases.get(base.id, "").split(".", 1)[0] in {"httpx", "requests", "aiohttp"}
            ):
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
        logical_source = re.sub(r"\\\r?\n", " ", source)
        for line_number, line in enumerate(logical_source.splitlines(), start=1):
            curl_command = re.search(r"(?:\bcurl\b|\$\([^)]*CURL[^)]*\))", line)
            write_option = re.search(
                r"(?:-X|--request)(?:=|\s+)|(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form)(?:=|\s+)",
                line,
            )
            if curl_command and write_option:
                findings.append(OracleFinding(relative.as_posix(), "SHELL_HTTP_WRITE", line_number, "curl"))
    for path in root.rglob("*.plist"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload.get("ProgramArguments", []) if isinstance(payload, dict) else []
            if isinstance(arguments, list) and "curl" in [
                str(argument).rsplit("/", 1)[-1] for argument in arguments
            ]:
                findings.append(OracleFinding(relative.as_posix(), "PLIST_HTTP_WRITE", 0, "curl"))
        except (OSError, plistlib.InvalidFileException) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
    makefile = root / "Makefile"
    if makefile.is_file():
        for line_number, line in enumerate(makefile.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.startswith("\t"):
                continue
            if re.search(r"(?:\bcurl\b|\$\([^)]*CURL[^)]*\))", line):
                findings.append(OracleFinding("Makefile", "MAKE_HTTP_WRITE", line_number, "curl"))
            if re.search(r"\b(?:httpx|requests|aiohttp|urllib\.request)\b", line):
                findings.append(OracleFinding("Makefile", "MAKE_EMBEDDED_NETWORK", line_number, "python"))
    return sorted(findings, key=lambda item: (item.path, item.line, item.kind, item.detail))


def verify_coverage(root: Path, registry: dict[str, Any]) -> list[str]:
    actual = {
        f"{finding.path}:{finding.line}:{finding.kind}:{finding.detail}" for finding in scan_repository(root)
    }
    declared = registry.get("independent_oracle_findings")
    if not isinstance(declared, list) or not all(isinstance(item, str) and item for item in declared):
        return ["INDEPENDENT_ORACLE_DECLARATIONS_INVALID"]
    expected = set(declared)
    issues = [f"INDEPENDENT_ORACLE_UNREGISTERED:{identity}" for identity in sorted(actual - expected)]
    issues.extend(f"INDEPENDENT_ORACLE_STALE:{identity}" for identity in sorted(expected - actual))
    if len(expected) != len(declared):
        issues.append("INDEPENDENT_ORACLE_DUPLICATE_DECLARATION")
    return issues


def load_registry(path: Path) -> dict[str, Any]:
    """Load the oracle input without accepting duplicate JSON keys."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"INDEPENDENT_ORACLE_DUPLICATE_KEY:{key}")
            result[key] = value
        return result

    registry = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(registry, dict):
        raise ValueError("INDEPENDENT_ORACLE_INVALID_ROOT")
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    registry_path = args.registry or args.root / "config" / "write-capability-registry.json"
    try:
        registry = load_registry(registry_path)
        issues = verify_coverage(args.root, registry)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        issues = [str(exc) or type(exc).__name__]
    print(json.dumps({"status": "PASS" if not issues else "FAIL", "issues": issues}, sort_keys=True))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
