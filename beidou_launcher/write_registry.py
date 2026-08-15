"""Machine-checkable registry for executable and exchange-sensitive paths."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

_PYTHON_MARKERS = (
    'if __name__ == "__main__"',
    "if __name__ == '__main__'",
    "AutonomousEngine(",
    "BinanceRESTClient(",
    "BinanceUsdmAdapter(",
)
_SKIP_PARTS = {".git", ".venv", "__pycache__", "docs", "tests", "build", "dist"}
_REQUIRED_ENTRY_FIELDS = {
    "id",
    "path",
    "kind",
    "owner",
    "capability",
    "status",
    "call_graph",
    "negative_test",
}
_ALLOWED_STATUS = {"HARD_HOLD", "READ_ONLY", "OFFLINE_ONLY", "DELEGATE_ONLY"}
_ALLOWED_CAPABILITIES = {
    "ACCOUNT_RISK_SETTING_REQUIRED",
    "ACCOUNT_WIDE_CLEANUP_FORBIDDEN",
    "BUILD_AND_RUNTIME_ALIAS_SURFACE",
    "CANCEL_ACCOUNT_ORDERS",
    "CANCEL_OWNED_REQUIRED",
    "CONTROL_RESUME_AUTHORITY_REQUIRED",
    "CREATE_PROTECTION_SCOPE_REQUIRED",
    "DATABASE_MIGRATION",
    "DELEGATE_TO_LAUNCHER",
    "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
    "EVIDENCE_READ_ONLY",
    "EVIDENCE_WINDOW_CONTROL",
    "EXCHANGE_READ_ONLY",
    "GIT_BRANCH_MUTATION",
    "INCREASE_OR_REDUCE_SCOPE_REQUIRED",
    "LEGACY_COMMAND_AND_EVIDENCE_WRITE",
    "LEGACY_EVIDENCE_PRODUCER",
    "LEGACY_EVIDENCE_READ_ONLY",
    "LEGACY_PREFLIGHT_COMMANDS",
    "LEGACY_TRADE_TOOL",
    "LOCAL_ARTIFACT_WRITE",
    "LOCAL_CONFIG_MIGRATION",
    "LOCAL_OBSERVATION",
    "OFFLINE_REPLAY",
    "OFFLINE_RESEARCH",
    "OFFLINE_SIMULATION",
    "PACKAGE_READ_ONLY_VALIDATION",
    "RUNTIME_ACTIVATION",
    "SOURCE_READ_ONLY",
    "TERMINAL_CANCEL_SCOPE_REQUIRED",
    "TERMINAL_CREATE_SCOPE_REQUIRED",
    "TERMINAL_WRITE_BOUNDARY",
    "TERMINAL_WRITE_INTERLOCK",
    "TERMINAL_WRITE_INTERNAL",
    "UNOWNED_RECOVERY_FORBIDDEN",
    "USER_STREAM_SESSION_WRITE_REQUIRED",
    "WRITABLE_RUNTIME_CANDIDATE",
}
_MUTATING_CALL_NAMES = {
    "create_order",
    "cancel_order",
    "create_algo_order",
    "cancel_algo_order",
    "_create_algo_order",
    "_cancel_algo_order",
    "_cancel_algo_orders",
    "_submit_order_slice",
}
_GENERIC_REQUEST_NAMES = {"request", "_request", "exchange", "_api", "_api_async", "_api_async_safe"}
_MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_REQUIRED_WRITE_PATH_FIELDS = {
    "id",
    "source",
    "occurrences",
    "owner",
    "capability",
    "status",
    "call_graph",
    "negative_test",
}


def _is_skipped(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return relative.as_posix() == "beidou_launcher/write_registry.py" or any(
        part in _SKIP_PARTS for part in relative.parts
    )


def discover_sensitive_entry_paths(root: Path) -> set[str]:
    """Discover executable declarations and direct runtime/transport construction."""

    root = root.resolve()
    discovered: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "[project.scripts]" in pyproject.read_text(encoding="utf-8"):
        discovered.add("pyproject.toml")
    makefile = root / "Makefile"
    if makefile.is_file():
        make_source = makefile.read_text(encoding="utf-8")
        if "python -m apps." in make_source or "beidou" in make_source:
            discovered.add("Makefile")

    for path in root.rglob("*.py"):
        if _is_skipped(path, root):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root)
        if relative.parts[:2] == ("delivery", "scripts") or any(marker in source for marker in _PYTHON_MARKERS):
            discovered.add(relative.as_posix())

    for pattern in ("*.sh", "*.plist"):
        for path in root.rglob(pattern):
            if not _is_skipped(path, root):
                discovered.add(path.relative_to(root).as_posix())
    return discovered


def discover_declared_entrypoints(root: Path) -> dict[str, str]:
    """Return exact console-script and Make target declarations."""

    root = root.resolve()
    declarations: dict[str, str] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        scripts = data.get("project", {}).get("scripts", {})
        if isinstance(scripts, dict):
            for name, target in scripts.items():
                declarations[f"console:{name}"] = str(target)

    makefile = root / "Makefile"
    if makefile.is_file():
        current_target = ""
        commands: list[str] = []

        def flush() -> None:
            if current_target and commands:
                declarations[f"make:{current_target}"] = " && ".join(commands)

        for line in makefile.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s.*)?$", line)
            if match:
                flush()
                current_target = match.group(1)
                commands = []
            elif current_target and line.startswith("\t"):
                command = line.strip()
                if command:
                    commands.append(command)
        flush()
    return dict(sorted(declarations.items()))


class _TerminalWriteCallVisitor(ast.NodeVisitor):
    """Collect stable function-qualified terminal-write call sites."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.aliases: list[dict[str, str]] = [{}]
        self.calls: Counter[str] = Counter()

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.aliases.append({})
        self.generic_visit(node)
        self.aliases.pop()
        self.scope.pop()

    @staticmethod
    def _attribute_name(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return ""

    @staticmethod
    def _getattr_target(node: ast.AST) -> str:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            return ""
        target = node.args[1].value
        if target in _MUTATING_CALL_NAMES or target in _GENERIC_REQUEST_NAMES or target == "execute_action":
            return target
        return ""

    def _reference_target(self, node: ast.AST) -> str:
        name = self._attribute_name(node)
        if name in _MUTATING_CALL_NAMES or name in _GENERIC_REQUEST_NAMES or name == "execute_action":
            return name
        return self._getattr_target(node)

    def _remember_alias(self, target: ast.AST, value: ast.AST) -> None:
        if not isinstance(target, ast.Name):
            return
        reference = self._reference_target(value)
        if reference:
            self.aliases[-1][target.id] = reference

    def _lookup_alias(self, name: str) -> str:
        for scope_aliases in reversed(self.aliases):
            if name in scope_aliases:
                return scope_aliases[name]
        return ""

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._remember_alias(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._remember_alias(node.target, node.value)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = self._attribute_name(node.func)

        marker = call_name if call_name in _MUTATING_CALL_NAMES else ""
        alias_target = self._lookup_alias(call_name) if isinstance(node.func, ast.Name) else ""
        if alias_target:
            marker = f"alias[{alias_target}]"
        direct_getattr_target = self._getattr_target(node.func)
        if direct_getattr_target:
            marker = f"getattr[{direct_getattr_target}]"
        if call_name in _GENERIC_REQUEST_NAMES:
            method_node: ast.AST | None = None
            for keyword in node.keywords:
                if keyword.arg == "method":
                    method_node = keyword.value
                    break
            if method_node is None:
                position = 1 if call_name.startswith("_api") else 0
                method_node = node.args[position] if len(node.args) > position else None
            if isinstance(method_node, ast.Constant) and isinstance(method_node.value, str):
                method = method_node.value.upper()
                if method in _MUTATING_HTTP_METHODS:
                    marker = f"{call_name}[{method}]"
            elif method_node is not None:
                marker = f"{call_name}[DYNAMIC]"

        if call_name == "execute_action":
            action_node = node.args[0] if node.args else None
            if isinstance(action_node, ast.Attribute):
                action = action_node.attr
            elif isinstance(action_node, ast.Constant) and isinstance(action_node.value, str):
                action = action_node.value
            else:
                action = "DYNAMIC"
            marker = f"execute_action[{action}]" if action in {"RESUME", "DYNAMIC"} else ""

        if marker:
            scope = ".".join(self.scope) if self.scope else "<module>"
            self.calls[f"{self.relative_path}::{scope}::{marker}"] += 1
        self.generic_visit(node)


def discover_terminal_write_calls(root: Path) -> dict[str, int]:
    """Return every production mutation call grouped by stable source identity."""

    root = root.resolve()
    calls: Counter[str] = Counter()
    for path in root.rglob("*.py"):
        if _is_skipped(path, root):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        visitor = _TerminalWriteCallVisitor(path.relative_to(root).as_posix())
        visitor.visit(tree)
        calls.update(visitor.calls)
    return dict(sorted(calls.items()))


def discover_source_scan_issues(root: Path) -> list[str]:
    """Return production source files that cannot be parsed or read."""

    root = root.resolve()
    issues: list[str] = []
    for path in root.rglob("*.py"):
        if _is_skipped(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            issues.append(f"WRITE_REGISTRY_SOURCE_SCAN_FAILED:{relative}:{type(exc).__name__}")
    return sorted(issues)


def load_registry(path: Path) -> dict[str, Any]:
    """Load a registry without accepting non-object or duplicate entries."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("WRITE_REGISTRY_INVALID_ROOT")
    return data


def _negative_test_reference_exists(reference: str, root: Path) -> bool:
    """Require a concrete top-level pytest function, not just a test file."""

    path_text, separator, node_id = reference.partition("::")
    if not separator or not node_id.startswith("test_"):
        return False
    test_path = (root / path_text).resolve()
    if not test_path.is_relative_to(root) or not test_path.is_file():
        return False
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    except (OSError, UnicodeError, SyntaxError):
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == node_id for node in tree.body)


def validate_registry(registry: dict[str, Any], *, root: Path) -> list[str]:
    """Return stable validation errors; an empty list is the only passing state."""

    issues: list[str] = []
    if registry.get("schema_version") != "1.0":
        issues.append("WRITE_REGISTRY_SCHEMA_VERSION_INVALID")
    root = root.resolve()
    issues.extend(discover_source_scan_issues(root))
    declared_entrypoints = registry.get("declared_entrypoints")
    if not isinstance(declared_entrypoints, dict):
        issues.append("WRITE_REGISTRY_DECLARED_ENTRYPOINTS_INVALID")
    elif declared_entrypoints != discover_declared_entrypoints(root):
        issues.append("WRITE_REGISTRY_DECLARED_ENTRYPOINTS_MISMATCH")
    entries = registry.get("entries")
    if not isinstance(entries, list):
        return ["WRITE_REGISTRY_ENTRIES_INVALID"]

    ids: set[str] = set()
    paths: set[str] = set()
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            issues.append(f"WRITE_REGISTRY_ENTRY_NOT_OBJECT:{index}")
            continue
        missing = sorted(_REQUIRED_ENTRY_FIELDS - raw_entry.keys())
        if missing:
            issues.append(f"WRITE_REGISTRY_FIELDS_MISSING:{index}:{','.join(missing)}")
            continue
        if not isinstance(raw_entry["id"], str) or not isinstance(raw_entry["path"], str):
            issues.append(f"WRITE_REGISTRY_ENTRY_TYPE_INVALID:{index}")
            continue
        entry_id = raw_entry["id"].strip()
        path = raw_entry["path"].strip()
        if entry_id in ids:
            issues.append(f"WRITE_REGISTRY_DUPLICATE_ID:{entry_id}")
        if path in paths:
            issues.append(f"WRITE_REGISTRY_DUPLICATE_PATH:{path}")
        ids.add(entry_id)
        paths.add(path)
        candidate = (root / path).resolve()
        if not path or not candidate.is_relative_to(root):
            issues.append(f"WRITE_REGISTRY_PATH_OUTSIDE_ROOT:{entry_id}")
        elif not candidate.is_file():
            issues.append(f"WRITE_REGISTRY_PATH_MISSING:{path}")
        if raw_entry["status"] not in _ALLOWED_STATUS:
            issues.append(f"WRITE_REGISTRY_STATUS_INVALID:{entry_id}")
        if raw_entry["capability"] not in _ALLOWED_CAPABILITIES:
            issues.append(f"WRITE_REGISTRY_CAPABILITY_INVALID:{entry_id}")
        for field in ("kind", "owner", "capability", "call_graph", "negative_test"):
            if not isinstance(raw_entry[field], str) or not raw_entry[field].strip():
                issues.append(f"WRITE_REGISTRY_FIELD_EMPTY:{entry_id}:{field}")
        negative_test = raw_entry["negative_test"]
        if isinstance(negative_test, str) and not _negative_test_reference_exists(negative_test, root):
            issues.append(f"WRITE_REGISTRY_NEGATIVE_TEST_MISSING:{entry_id}")

    discovered = discover_sensitive_entry_paths(root)
    for path in sorted(discovered - paths):
        issues.append(f"WRITE_REGISTRY_UNREGISTERED_PATH:{path}")
    for path in sorted(paths - discovered):
        issues.append(f"WRITE_REGISTRY_STALE_PATH:{path}")

    candidates = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("capability") == "WRITABLE_RUNTIME_CANDIDATE"
    ]
    if len(candidates) != 1 or candidates[0].get("path") != "beidou_launcher/cli.py":
        issues.append("WRITE_REGISTRY_WRITABLE_CANDIDATE_INVALID")
    elif candidates[0].get("status") != "HARD_HOLD":
        issues.append("WRITE_REGISTRY_WRITABLE_CANDIDATE_NOT_HELD")

    write_paths = registry.get("terminal_write_paths")
    if not isinstance(write_paths, list):
        issues.append("WRITE_REGISTRY_TERMINAL_PATHS_INVALID")
        return sorted(set(issues))
    registered_calls: dict[str, int] = {}
    write_ids: set[str] = set()
    for index, raw_path in enumerate(write_paths):
        if not isinstance(raw_path, dict):
            issues.append(f"WRITE_REGISTRY_TERMINAL_PATH_NOT_OBJECT:{index}")
            continue
        missing = sorted(_REQUIRED_WRITE_PATH_FIELDS - raw_path.keys())
        if missing:
            issues.append(f"WRITE_REGISTRY_TERMINAL_FIELDS_MISSING:{index}:{','.join(missing)}")
            continue
        path_id = raw_path["id"]
        source = raw_path["source"]
        occurrences = raw_path["occurrences"]
        if not isinstance(path_id, str) or not path_id.strip() or path_id in write_ids:
            issues.append(f"WRITE_REGISTRY_TERMINAL_ID_INVALID:{index}")
        else:
            write_ids.add(path_id)
        if not isinstance(source, str) or not source.strip() or source in registered_calls:
            issues.append(f"WRITE_REGISTRY_TERMINAL_SOURCE_INVALID:{index}")
            continue
        if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences < 1:
            issues.append(f"WRITE_REGISTRY_TERMINAL_COUNT_INVALID:{path_id}")
            continue
        registered_calls[source] = occurrences
        if raw_path["status"] != "HARD_HOLD":
            issues.append(f"WRITE_REGISTRY_TERMINAL_NOT_HELD:{path_id}")
        if raw_path["capability"] not in _ALLOWED_CAPABILITIES:
            issues.append(f"WRITE_REGISTRY_TERMINAL_CAPABILITY_INVALID:{path_id}")
        for field in ("owner", "capability", "call_graph", "negative_test"):
            if not isinstance(raw_path[field], str) or not raw_path[field].strip():
                issues.append(f"WRITE_REGISTRY_TERMINAL_FIELD_EMPTY:{path_id}:{field}")
        negative_test = raw_path["negative_test"]
        if isinstance(negative_test, str) and not _negative_test_reference_exists(negative_test, root):
            issues.append(f"WRITE_REGISTRY_TERMINAL_NEGATIVE_TEST_MISSING:{path_id}")

    discovered_calls = discover_terminal_write_calls(root)
    for source in sorted(discovered_calls.keys() - registered_calls.keys()):
        issues.append(f"WRITE_REGISTRY_UNREGISTERED_TERMINAL_PATH:{source}")
    for source in sorted(registered_calls.keys() - discovered_calls.keys()):
        issues.append(f"WRITE_REGISTRY_STALE_TERMINAL_PATH:{source}")
    for source in sorted(discovered_calls.keys() & registered_calls.keys()):
        if discovered_calls[source] != registered_calls[source]:
            issues.append(f"WRITE_REGISTRY_TERMINAL_COUNT_MISMATCH:{source}")
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    """Run a read-only registry validation suitable for CI and local dry-runs."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args(argv)
    registry_path = args.registry or args.root / "config" / "write-capability-registry.json"
    issues = validate_registry(load_registry(registry_path), root=args.root)
    print(json.dumps({"status": "PASS" if not issues else "FAIL", "issues": issues}, sort_keys=True))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "discover_declared_entrypoints",
    "discover_sensitive_entry_paths",
    "discover_source_scan_issues",
    "discover_terminal_write_calls",
    "load_registry",
    "main",
    "validate_registry",
]
