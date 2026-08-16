"""Machine-checkable registry for executable and exchange-sensitive paths."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import plistlib
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


class DuplicateKeyError(ValueError):
    """Raised when YAML attempts to hide a previous mapping value."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _load_yaml_unique(source: str) -> Any:
    return yaml.load(source, Loader=_UniqueKeyLoader)  # noqa: S506 - strict SafeLoader subclass


_PYTHON_MARKERS = (
    'if __name__ == "__main__"',
    "if __name__ == '__main__'",
    "AutonomousEngine(",
    "BinanceRESTClient(",
    "BinanceUsdmAdapter(",
)
_SKIP_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "tests",
}
_REQUIRED_ENTRY_FIELDS = {
    "id",
    "path",
    "kind",
    "owner",
    "capability",
    "status",
    "call_graph",
    "negative_test",
    "expected_rejection",
}
_ALLOWED_STATUS = {"HARD_HOLD", "READ_ONLY", "OFFLINE_ONLY", "DELEGATE_ONLY"}
_ALLOWED_CAPABILITIES = {
    "ACCOUNT_RISK_SETTING_REQUIRED",
    "ACCOUNT_WIDE_CLEANUP_FORBIDDEN",
    "BUILD_AND_RUNTIME_ALIAS_SURFACE",
    "CANCEL_ACCOUNT_ORDERS",
    "CANCEL_OWNED_REQUIRED",
    "CI_AUTOMATION",
    "CONTROL_RESUME_AUTHORITY_REQUIRED",
    "CREATE_PROTECTION_SCOPE_REQUIRED",
    "DATABASE_MIGRATION",
    "DELEGATE_TO_LAUNCHER",
    "DEVELOPER_HOOKS",
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
    "TESTNET_CERTIFICATION_WRITE_HELD",
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
_NETWORK_MODULES = {"aiohttp", "httpx", "requests", "socket", "urllib.request"}
_MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_HTTP_CALL_NAMES = {"post", "put", "patch", "delete"}
_NETWORK_CALL_MARKERS = {
    "Request": "urllib_request",
    "urlopen": "urllib_urlopen",
    "open_connection": "asyncio_open_connection",
    "create_connection": "socket_create_connection",
    "sendall": "socket_sendall",
}
_CONSTRUCTOR_NAMES = {"AutonomousEngine", "BinanceRESTClient", "BinanceUsdmAdapter"}
_ALLOWED_OWNERS = {
    "Control Owner",
    "Engineering Owner",
    "Execution Owner",
    "Market Data Owner",
    "Research Owner",
    "Risk Owner",
    "Runtime Owner",
    "SRE Owner",
    "Safety Owner",
    "Security Owner",
    "Storage Owner",
    "Test Quality Owner",
}
_TERMINAL_CAPABILITIES = {
    "ACCOUNT_RISK_SETTING_REQUIRED",
    "ACCOUNT_WIDE_CLEANUP_FORBIDDEN",
    "CANCEL_OWNED_REQUIRED",
    "CONTROL_RESUME_AUTHORITY_REQUIRED",
    "CREATE_PROTECTION_SCOPE_REQUIRED",
    "DYNAMIC_WRITE_BOUNDARY_REQUIRED",
    "DYNAMIC_READ_BOUNDARY",
    "INCREASE_OR_REDUCE_SCOPE_REQUIRED",
    "TERMINAL_CANCEL_SCOPE_REQUIRED",
    "TERMINAL_CREATE_SCOPE_REQUIRED",
    "TESTNET_CERTIFICATION_WRITE_HELD",
    "UNOWNED_RECOVERY_FORBIDDEN",
    "USER_STREAM_SESSION_WRITE_REQUIRED",
    "LOCAL_HEALTH_READ",
}
_GENERIC_NEGATIVE_TEST = (
    "tests/architecture/test_write_capability_registry.py::test_write_capability_registry_is_complete_and_valid"
)
_REQUIRED_WRITE_PATH_FIELDS = {
    "id",
    "source",
    "occurrences",
    "owner",
    "capability",
    "status",
    "call_graph",
    "negative_test",
    "expected_rejection",
}
_REQUIRED_DECLARATION_FIELDS = {
    "command",
    "owner",
    "capability",
    "status",
    "negative_test",
    "expected_rejection",
}
_REQUIRED_NETWORK_IMPORT_FIELDS = {
    "id",
    "source",
    "occurrences",
    "owner",
    "purpose",
    "status",
    "negative_test",
    "expected_rejection",
}
_ALLOWED_NETWORK_PURPOSES = {
    "ALERT_DELIVERY",
    "EXCHANGE_WEBSOCKET",
    "EXCHANGE_TRANSPORT",
    "FAULT_INJECTION_READ",
    "LOCAL_HEALTH_READ",
    "LOCAL_NETWORK_BIND_CHECK",
}
_ALLOWED_REJECTIONS = {
    "CANONICAL_LAUNCHER_REQUIRED",
    "CONTROL_AUTHORITY_REQUIRED",
    "EXTERNAL_WRITE_NOT_AUTHORIZED",
    "NONCANONICAL_ENTRYPOINT_HELD",
    "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
}
_REQUIRED_BEHAVIORAL_NEGATIVE_TESTS = {
    "tests/architecture/test_entrypoint_negative_contracts.py::test_delegating_entrypoints_reject_write_mode_injection",
    "tests/unit/test_startup_side_effects.py::test_blocked_preflight_runs_before_lock_or_evidence_writes",
    "tests/unit/test_terminal_write_authority.py::test_adapter_does_not_treat_reduce_or_cancel_as_implicitly_authorized",
    "tests/unit/test_terminal_write_authority.py::test_listen_key_session_writes_are_held_before_transport",
    "tests/unit/test_terminal_write_authority.py::test_rest_transport_denies_all_terminal_writes_without_authority",
    "tests/unit/test_terminal_write_authority.py::test_unknown_mutating_endpoint_is_denied_before_transport",
}


def _expected_entry_rejection(status: str, capability: str) -> str:
    if status == "DELEGATE_ONLY":
        return "CANONICAL_LAUNCHER_REQUIRED"
    if status != "HARD_HOLD":
        return "EXTERNAL_WRITE_NOT_AUTHORIZED"
    if capability == "CONTROL_RESUME_AUTHORITY_REQUIRED":
        return "CONTROL_AUTHORITY_REQUIRED"
    if capability.startswith("LEGACY") or capability in {
        "BUILD_AND_RUNTIME_ALIAS_SURFACE",
        "GIT_BRANCH_MUTATION",
        "RUNTIME_ACTIVATION",
    }:
        return "NONCANONICAL_ENTRYPOINT_HELD"
    return "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"


def _is_skipped(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return relative.as_posix() == "beidou_launcher/write_registry.py" or any(
        part in _SKIP_PARTS for part in relative.parts
    )


def _is_governed_source_skipped(path: Path, root: Path) -> bool:
    """Exclude non-source trees while retaining executable runtime artifacts."""

    relative = path.relative_to(root)
    return any(part in _SKIP_PARTS for part in relative.parts)


def _yaml_execution_strings(payload: Any) -> list[str]:
    commands: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in {"command", "entrypoint", "exec", "run", "script", "uses"}:
                if isinstance(value, str):
                    commands.append(value)
                elif isinstance(value, list):
                    commands.append(" ".join(str(item) for item in value))
            commands.extend(_yaml_execution_strings(value))
    elif isinstance(payload, list):
        for item in payload:
            commands.extend(_yaml_execution_strings(item))
    return commands


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
    for declarative_relative in ("docker-compose.yml", ".pre-commit-config.yaml"):
        if (root / declarative_relative).is_file():
            discovered.add(declarative_relative)
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        discovered.update(
            path.relative_to(root).as_posix()
            for pattern in ("*.yml", "*.yaml")
            for path in workflows.glob(pattern)
            if path.is_file()
        )

    for path in root.rglob("*.py"):
        if _is_skipped(path, root):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        relative = path.relative_to(root)
        entry_visitor = _EntrySurfaceVisitor()
        entry_visitor.visit(tree)
        if (
            relative.parts[:2] == ("delivery", "scripts")
            or any(marker in source for marker in _PYTHON_MARKERS)
            or entry_visitor.sensitive
        ):
            discovered.add(relative.as_posix())

    for pattern in ("*.sh", "*.plist"):
        for path in root.rglob(pattern):
            if not _is_skipped(path, root):
                discovered.add(path.relative_to(root).as_posix())
    for pattern in ("*.yml", "*.yaml"):
        for path in root.rglob(pattern):
            if _is_skipped(path, root):
                continue
            with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError, DuplicateKeyError):
                payload = _load_yaml_unique(path.read_text(encoding="utf-8"))
                if _yaml_execution_strings(payload):
                    discovered.add(path.relative_to(root).as_posix())
    for path in root.rglob("*.cron"):
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


def discover_network_imports(root: Path) -> dict[str, int]:
    """Inventory network-capable imports and explicit stream-opening calls."""

    root = root.resolve()
    imports: Counter[str] = Counter()
    for path in root.rglob("*.py"):
        if _is_skipped(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.append(alias.name)
                    aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
            ):
                dynamic_module = _constant_string(node.args[0])
                if dynamic_module:
                    modules.append(dynamic_module)
            for module in modules:
                normalized = "urllib.request" if module.startswith("urllib.request") else module.split(".", 1)[0]
                if normalized in _NETWORK_MODULES:
                    imports[f"{relative}::{normalized}"] += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            parts: list[str] = []
            while isinstance(function, ast.Attribute):
                parts.append(function.attr)
                function = function.value
            if isinstance(function, ast.Name):
                parts.append(function.id)
            dotted = ".".join(reversed(parts))
            root_name, _, remainder = dotted.partition(".")
            resolved_root = aliases.get(root_name, root_name)
            resolved = f"{resolved_root}.{remainder}" if remainder else resolved_root
            if resolved in {"asyncio.open_connection", "socket.socket"}:
                imports[f"{relative}::{resolved}"] += 1
    return dict(sorted(imports.items()))


def discover_governed_source_digests(root: Path) -> dict[str, str]:
    """Hash every production code, executable, and declarative source surface."""

    root = root.resolve()
    governed_suffixes = {".cron", ".json", ".plist", ".py", ".sh", ".sql", ".toml", ".yaml", ".yml"}
    runtime_source_suffixes = {".cron", ".plist", ".py", ".sh", ".sql", ".toml", ".yaml", ".yml"}
    digests: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or _is_governed_source_skipped(path, root):
            continue
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        if relative_text == "config/write-capability-registry.json":
            continue
        try:
            executable = bool(path.stat().st_mode & 0o111)
            has_shebang = path.read_bytes()[:2] == b"#!"
        except OSError:
            continue
        if relative.parts[0] in {".beidou", "evidence"} and not (
            path.suffix.lower() in runtime_source_suffixes or executable or has_shebang
        ):
            continue
        if (
            path.suffix.lower() not in governed_suffixes
            and path.name not in {"Makefile", "Dockerfile"}
            and not path.name.startswith("Dockerfile.")
            and not executable
            and not has_shebang
        ):
            continue
        digests[relative_text] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(digests.items()))


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


class _EntrySurfaceVisitor(ast.NodeVisitor):
    """Conservatively detect runtime constructors and unresolved callable factories."""

    def __init__(self) -> None:
        self.aliases: set[str] = set()
        self.sensitive = False

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _CONSTRUCTOR_NAMES:
                self.aliases.add(alias.asname or alias.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        constructor = ""
        if isinstance(node.value, ast.Name) and node.value.id in self.aliases | _CONSTRUCTOR_NAMES:
            constructor = node.value.id
        elif isinstance(node.value, ast.Attribute) and node.value.attr in _CONSTRUCTOR_NAMES:
            constructor = node.value.attr
        if constructor:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases.add(target.id)
                elif isinstance(target, (ast.Attribute, ast.Subscript)):
                    self.sensitive = True
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if any(
            isinstance(child, (ast.Name, ast.Attribute))
            and self._constructor_name(child) in self.aliases | _CONSTRUCTOR_NAMES
            for child in ast.walk(node.body)
        ):
            self.sensitive = True
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        if any(self._constructor_name(value) in self.aliases | _CONSTRUCTOR_NAMES for value in node.values):
            self.sensitive = True
        self.generic_visit(node)

    @staticmethod
    def _constructor_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def visit_Call(self, node: ast.Call) -> None:
        direct_constructor = isinstance(node.func, ast.Name) and node.func.id in self.aliases | _CONSTRUCTOR_NAMES
        attribute_constructor = isinstance(node.func, ast.Attribute) and node.func.attr in _CONSTRUCTOR_NAMES
        if direct_constructor or attribute_constructor:
            self.sensitive = True
        elif (
            isinstance(node.func, ast.Call) and isinstance(node.func.func, ast.Name) and node.func.func.id == "getattr"
        ):
            target = _constant_string(node.func.args[1]) if len(node.func.args) >= 2 else None
            if target is None or target in _CONSTRUCTOR_NAMES:
                self.sensitive = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
            self.sensitive = True
        self.generic_visit(node)


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
    def _expression_key(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _TerminalWriteCallVisitor._expression_key(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Subscript):
            base = _TerminalWriteCallVisitor._expression_key(node.value)
            index = _constant_string(node.slice)
            return f"{base}[{index or '*'}]" if base else ""
        return ""

    @staticmethod
    def _getattr_target(node: ast.AST) -> str:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
        ):
            return ""
        target = _constant_string(node.args[1])
        if target is None:
            return "DYNAMIC"
        if target.lower() in _HTTP_CALL_NAMES:
            return f"http[{target.upper()}]"
        if target in _NETWORK_CALL_MARKERS:
            return _NETWORK_CALL_MARKERS[target]
        if target in _MUTATING_CALL_NAMES or target in _GENERIC_REQUEST_NAMES or target == "execute_action":
            return target
        return ""

    def _reflective_target(self, node: ast.AST) -> str:
        direct = self._getattr_target(node)
        if direct:
            return direct
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and len(node.args) >= 2
        ):
            return ""
        target = _constant_string(node.args[1])
        return f"reflective[{target or 'DYNAMIC'}]"

    def _reference_target(self, node: ast.AST) -> str:
        key = self._expression_key(node)
        alias = self._lookup_alias(key)
        if alias:
            return alias
        if isinstance(node, ast.Name) and node.id in {"aiohttp", "asyncio", "httpx", "requests", "socket", "urllib"}:
            return f"network_module[{node.id}]"
        if isinstance(node, ast.Subscript):
            container_alias = self._lookup_alias(self._expression_key(node.value))
            if container_alias:
                return f"subscript_alias[{container_alias}]"
        name = self._attribute_name(node)
        if name.lower() in _HTTP_CALL_NAMES:
            return f"http[{name.upper()}]"
        if name in _NETWORK_CALL_MARKERS:
            return _NETWORK_CALL_MARKERS[name]
        if name in _MUTATING_CALL_NAMES or name in _GENERIC_REQUEST_NAMES or name == "execute_action":
            return name
        getattr_target = self._reflective_target(node)
        if getattr_target:
            return getattr_target
        if isinstance(node, ast.Call) and self._attribute_name(node.func) == "partial" and node.args:
            reference = self._reference_target(node.args[0])
            if reference:
                method_value = _constant_string(node.args[1]) if len(node.args) > 1 else None
                method = method_value.upper() if method_value else "DYNAMIC"
                return f"partial[{reference}:{method}]"
        if isinstance(node, ast.Call) and self._attribute_name(node.func) == "vars" and node.args:
            reference = self._reference_target(node.args[0])
            if reference:
                return f"vars[{reference}]"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            container_reference = self._reference_target(node.func.value)
            if node.func.attr in {"get", "pop", "values"} and container_reference:
                return f"container_extract[{container_reference}]"
        if isinstance(node, ast.Lambda):
            return self._reference_target(node.body)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            references = {self._reference_target(item) for item in node.elts}
            references.discard("")
            return references.pop() if len(references) == 1 else ("DYNAMIC" if references else "")
        if isinstance(node, ast.Dict):
            references = {self._reference_target(item) for item in node.values}
            references.discard("")
            return references.pop() if len(references) == 1 else ("DYNAMIC" if references else "")
        return ""

    def _remember_alias(self, target: ast.AST, value: ast.AST) -> None:
        key = self._expression_key(target)
        if not key:
            return
        reference = self._reference_target(value)
        if reference:
            if isinstance(target, ast.Attribute):
                reference = f"attribute_alias[{reference}]"
            elif isinstance(target, ast.Subscript):
                reference = f"subscript_alias[{reference}]"
            self.aliases[-1][key] = reference

    def _lookup_alias(self, name: str) -> str:
        for scope_aliases in reversed(self.aliases):
            if name in scope_aliases:
                return scope_aliases[name]
        return ""

    def _explicit_callable_reference(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            reference = self._lookup_alias(node.id)
            return "" if reference in {"DYNAMIC", "exchange", "import_alias[exchange]"} else reference
        if isinstance(node, ast.Attribute) and (
            node.attr in _MUTATING_CALL_NAMES
            or node.attr in (_GENERIC_REQUEST_NAMES - {"exchange"})
            or node.attr == "execute_action"
        ):
            return node.attr
        return ""

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if (
                alias.name in _MUTATING_CALL_NAMES
                or alias.name in _GENERIC_REQUEST_NAMES
                or alias.name == "execute_action"
            ):
                self.aliases[-1][alias.asname or alias.name] = f"import_alias[{alias.name}]"

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._remember_alias(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._remember_alias(node.target, node.value)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            reference = self._reference_target(node.value)
            if reference:
                scope = ".".join(self.scope) if self.scope else "<module>"
                self.calls[f"{self.relative_path}::{scope}::returned_callable[{reference}]"] += 1
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
        expression_key = self._expression_key(node.func)
        alias_target = self._lookup_alias(expression_key)
        if not alias_target and isinstance(node.func, ast.Subscript):
            container_target = self._lookup_alias(self._expression_key(node.func.value))
            if container_target:
                alias_target = f"subscript_alias[{container_target}]"
        if alias_target:
            marker = (
                alias_target
                if alias_target.startswith(("attribute_alias[", "subscript_alias[", "import_alias[", "partial["))
                else f"alias[{alias_target}]"
            )

        direct_getattr_target = self._reflective_target(node.func)
        if direct_getattr_target:
            marker = f"getattr[{direct_getattr_target}]"

        if call_name == "getattr":
            retrieved_target = self._getattr_target(node)
            if retrieved_target:
                marker = f"getattr[{retrieved_target}]"

        if call_name == "__import__" and node.args:
            imported = _constant_string(node.args[0])
            if imported and imported.split(".", 1)[0] in {
                "aiohttp",
                "asyncio",
                "httpx",
                "requests",
                "socket",
                "urllib",
            }:
                marker = f"dynamic_import[{imported}]"

        partial_target = self._reference_target(node.func) if isinstance(node.func, ast.Call) else ""
        if partial_target.startswith("partial["):
            marker = partial_target

        generic_name = call_name if call_name in _GENERIC_REQUEST_NAMES else ""
        if alias_target and not alias_target.startswith("partial["):
            alias_words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", alias_target))
            alias_generics = alias_words & _GENERIC_REQUEST_NAMES
            if alias_generics:
                generic_name = sorted(alias_generics)[0]
        if generic_name:
            marker = ""
            method_node: ast.AST | None = None
            for keyword in node.keywords:
                if keyword.arg == "method":
                    method_node = keyword.value
                    break
            if method_node is None:
                position = 1 if generic_name.startswith("_api") else 0
                method_node = node.args[position] if len(node.args) > position else None
            method_value = _constant_string(method_node) if method_node is not None else None
            if method_value is not None:
                method = method_value.upper()
                if method in _MUTATING_HTTP_METHODS:
                    marker = f"{generic_name}[{method}]"
            elif method_node is not None:
                marker = f"{generic_name}[DYNAMIC]"

        if call_name == "execute_action":
            action_node = node.args[0] if node.args else None
            if isinstance(action_node, ast.Attribute):
                action = action_node.attr
            elif isinstance(action_node, ast.Constant) and isinstance(action_node.value, str):
                action = action_node.value
            else:
                action = "DYNAMIC"
            marker = f"execute_action[{action}]" if action in {"RESUME", "DYNAMIC"} else ""

        if call_name.lower() in _HTTP_CALL_NAMES:
            marker = f"http[{call_name.upper()}]"

        if call_name in _NETWORK_CALL_MARKERS:
            marker = _NETWORK_CALL_MARKERS[call_name]

        if call_name == "setattr" and len(node.args) >= 3:
            stored_target = self._reference_target(node.args[2])
            if stored_target:
                marker = f"setattr[{stored_target}]"

        callable_arguments: set[str] = set()
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            reference = self._explicit_callable_reference(argument)
            if not reference:
                dynamic_reference = self._reference_target(argument)
                if dynamic_reference.startswith(
                    ("container_extract[", "partial[", "reflective[", "subscript_alias[", "vars[")
                ):
                    reference = dynamic_reference
            if reference:
                callable_arguments.add(reference)
        callable_arguments.discard("")
        if callable_arguments and not marker:
            marker = f"callable_argument[{','.join(sorted(callable_arguments))}]"

        if marker:
            scope = ".".join(self.scope) if self.scope else "<module>"
            self.calls[f"{self.relative_path}::{scope}::{marker}"] += 1
        self.generic_visit(node)

    @staticmethod
    def _is_direct_http_client(node: ast.AST) -> bool:
        if not isinstance(node, ast.Attribute):
            return False
        value = node.value
        if isinstance(value, ast.Name):
            return value.id in {"requests", "httpx", "aiohttp"}
        if isinstance(value, ast.Call):
            constructor = _TerminalWriteCallVisitor._expression_key(value.func)
            return constructor.startswith(("httpx.", "requests.", "aiohttp."))
        return False


def _logical_shell_lines(source: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw_line in source.splitlines():
        stripped = raw_line.rstrip()
        pending = f"{pending} {stripped}".strip() if pending else stripped
        if stripped.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        lines.append(pending)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _curl_method(command: str) -> str | None:
    if not re.search(r"(?:^|\s|[;&|]\s*|\benv\s+)(?:[^\s]+/)?curl\b|\$\([^)]*CURL[^)]*\)", command):
        return None
    method_match = re.search(r"(?:^|\s)(?:-X|--request)(?:=|\s+)([^\s]+)", command)
    if method_match is not None:
        candidate = method_match.group(1).strip("'\"").upper()
        return candidate if candidate in _MUTATING_HTTP_METHODS else "DYNAMIC"
    if re.search(r"(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form)(?:=|\s+)", command):
        return "POST"
    return None


def _discover_shell_write_calls(path: Path, relative: str) -> Counter[str]:
    calls: Counter[str] = Counter()
    source = path.read_text(encoding="utf-8")
    curl_aliases = set(
        re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?(?:[^\s'\"]*/)?curl['\"]?\s*$", source)
    )
    for line in _logical_shell_lines(source):
        for alias in curl_aliases:
            line = re.sub(rf'"?\$(?:\{{{re.escape(alias)}\}}|{re.escape(alias)})"?', "curl", line)
        method = _curl_method(line)
        if method is not None:
            calls[f"{relative}::<shell>::curl[{method}]"] += 1
        if re.search(r"\b(?:python|python3)\b.*\b(?:httpx|requests|aiohttp|urllib\.request|socket)\b", line):
            calls[f"{relative}::<shell>::embedded_network[DYNAMIC]"] += 1
    return calls


def _discover_cron_write_calls(path: Path, relative: str) -> Counter[str]:
    shell_calls = _discover_shell_write_calls(path, relative)
    return Counter({source.replace("::<shell>::", "::<cron>::"): count for source, count in shell_calls.items()})


def _discover_plist_write_calls(path: Path, relative: str) -> Counter[str]:
    calls: Counter[str] = Counter()
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    arguments = payload.get("ProgramArguments", []) if isinstance(payload, dict) else []
    if not isinstance(arguments, list):
        return calls
    command = " ".join(str(argument) for argument in arguments)
    method = _curl_method(command)
    if method is not None:
        calls[f"{relative}::<plist>::curl[{method}]"] += 1
    if re.search(r"\b(?:python|python3)\b.*\b(?:httpx|requests|aiohttp|urllib\.request|socket)\b", command):
        calls[f"{relative}::<plist>::embedded_network[DYNAMIC]"] += 1
    return calls


def _discover_make_write_calls(path: Path) -> Counter[str]:
    calls: Counter[str] = Counter()
    current_target = "UNKNOWN"
    source = path.read_text(encoding="utf-8")
    curl_aliases = set(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(?:[^\s]*/)?curl\s*$", source))
    for line in source.splitlines():
        target_match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s.*)?$", line)
        if target_match:
            current_target = target_match.group(1)
            continue
        if not line.startswith("\t"):
            continue
        for alias in curl_aliases:
            line = line.replace(f"$({alias})", "curl").replace(f"${{{alias}}}", "curl")
        method = _curl_method(line)
        if method is not None:
            calls[f"Makefile::<make:{current_target}>::curl[{method}]"] += 1
        if re.search(r"\b(?:httpx|requests|aiohttp|urllib\.request)\b", line):
            calls[f"Makefile::<make:{current_target}>::embedded_network[DYNAMIC]"] += 1
    return calls


def _discover_yaml_write_calls(path: Path, relative: str) -> Counter[str]:
    calls: Counter[str] = Counter()
    payload = _load_yaml_unique(path.read_text(encoding="utf-8"))
    for command in _yaml_execution_strings(payload):
        method = _curl_method(command)
        if method is not None:
            calls[f"{relative}::<yaml>::curl[{method}]"] += 1
        if re.search(r"\b(?:python|python3)\b.*\b(?:httpx|requests|aiohttp|urllib\.request|socket)\b", command):
            calls[f"{relative}::<yaml>::embedded_network[DYNAMIC]"] += 1
        if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@", command.strip()):
            calls[f"{relative}::<yaml>::remote_action[DYNAMIC]"] += 1
        if re.search(r"(?:^|\s)(?:python\s+-m\s+)?pip\s+install(?:\s|$)", command):
            calls[f"{relative}::<yaml>::package_network[DYNAMIC]"] += 1
    return calls


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
    for path in root.rglob("*.sh"):
        if _is_skipped(path, root):
            continue
        try:
            calls.update(_discover_shell_write_calls(path, path.relative_to(root).as_posix()))
        except (OSError, UnicodeError):
            continue
    for path in root.rglob("*.plist"):
        if _is_skipped(path, root):
            continue
        try:
            calls.update(_discover_plist_write_calls(path, path.relative_to(root).as_posix()))
        except (OSError, plistlib.InvalidFileException):
            continue
    for pattern in ("*.yml", "*.yaml"):
        for path in root.rglob(pattern):
            if _is_skipped(path, root):
                continue
            try:
                calls.update(_discover_yaml_write_calls(path, path.relative_to(root).as_posix()))
            except (OSError, UnicodeError, yaml.YAMLError, DuplicateKeyError):
                continue
    for path in root.rglob("*.cron"):
        if _is_skipped(path, root):
            continue
        try:
            calls.update(_discover_cron_write_calls(path, path.relative_to(root).as_posix()))
        except (OSError, UnicodeError):
            continue
    makefile = root / "Makefile"
    if makefile.is_file():
        with contextlib.suppress(OSError, UnicodeError):
            calls.update(_discover_make_write_calls(makefile))
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
    for path in root.rglob("*.sh"):
        if _is_skipped(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            interpreter = "/bin/zsh" if "zsh" in first_line else "/bin/sh"
            result = subprocess.run(  # noqa: S603 - fixed syntax-check interpreter; no script execution
                [interpreter, "-n", str(path)],
                capture_output=True,
                check=False,
                text=True,
            )
            if result.returncode != 0:
                issues.append(f"WRITE_REGISTRY_SOURCE_SCAN_FAILED:{relative}:ShellSyntaxError")
        except (OSError, UnicodeError, IndexError) as exc:
            issues.append(f"WRITE_REGISTRY_SOURCE_SCAN_FAILED:{relative}:{type(exc).__name__}")
    for path in root.rglob("*.plist"):
        if _is_skipped(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            with path.open("rb") as handle:
                plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as exc:
            issues.append(f"WRITE_REGISTRY_SOURCE_SCAN_FAILED:{relative}:{type(exc).__name__}")
    declarative_paths = [
        path for pattern in ("*.yml", "*.yaml") for path in root.rglob(pattern) if not _is_skipped(path, root)
    ]
    for path in declarative_paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            payload = _load_yaml_unique(path.read_text(encoding="utf-8"))
            if not isinstance(payload, (dict, list)):
                raise ValueError("declarative entrypoint must be a mapping or list")
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            issues.append(f"WRITE_REGISTRY_SOURCE_SCAN_FAILED:{relative}:{type(exc).__name__}")
    return sorted(issues)


def load_registry(path: Path) -> dict[str, Any]:
    """Load a registry without accepting non-object or duplicate entries."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"WRITE_REGISTRY_DUPLICATE_KEY:{key}")
            result[key] = value
        return result

    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("WRITE_REGISTRY_INVALID_ROOT")
    return data


def _negative_test_reference_exists(reference: str, root: Path) -> bool:
    """Require a concrete top-level pytest function, not just a test file."""

    path_text, separator, node_id = reference.partition("::")
    function_name = node_id.partition("[")[0]
    if not separator or not function_name.startswith("test_"):
        return False
    test_path = (root / path_text).resolve()
    if not test_path.is_relative_to(root) or not test_path.is_file():
        return False
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    except (OSError, UnicodeError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name for node in tree.body
    )


def _bound_negative_test(kind: str, identity: str) -> str:
    pytest_identity = "".join(
        character if character.isascii() and (character.isalnum() or character in "_-") else f"u{ord(character):04x}"
        for character in identity
    )
    return (
        "tests/architecture/test_registry_record_contracts.py::"
        f"test_{kind}_record_is_behaviorally_bound[{pytest_identity}]"
    )


def compute_governance_digest(registry: dict[str, Any]) -> str:
    """Bind all security-relevant record semantics to one immutable identity."""

    payload = {
        "declared_entrypoint_records": registry.get("declared_entrypoint_records"),
        "entries": registry.get("entries"),
        "governed_source_digests": registry.get("governed_source_digests"),
        "independent_oracle_findings": registry.get("independent_oracle_findings"),
        "network_imports": registry.get("network_imports"),
        "terminal_write_paths": registry.get("terminal_write_paths"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_registry(registry: dict[str, Any], *, root: Path) -> list[str]:
    """Return stable validation errors; an empty list is the only passing state."""

    root = root.resolve()
    issues: list[str] = []
    if registry.get("governance_digest") != compute_governance_digest(registry):
        issues.append("WRITE_REGISTRY_GOVERNANCE_DIGEST_MISMATCH")
    behavioral_tests = registry.get("behavioral_negative_tests")
    if not isinstance(behavioral_tests, list) or set(behavioral_tests) != _REQUIRED_BEHAVIORAL_NEGATIVE_TESTS:
        issues.append("WRITE_REGISTRY_BEHAVIORAL_NEGATIVE_TESTS_INVALID")
    else:
        for reference in behavioral_tests:
            if not _negative_test_reference_exists(reference, root):
                issues.append(f"WRITE_REGISTRY_BEHAVIORAL_NEGATIVE_TEST_MISSING:{reference}")
    registered_digests = registry.get("governed_source_digests")
    discovered_digests = discover_governed_source_digests(root)
    if not isinstance(registered_digests, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) and len(digest) == 64
        for path, digest in registered_digests.items()
    ):
        issues.append("WRITE_REGISTRY_SOURCE_DIGESTS_INVALID")
    else:
        for path in sorted(discovered_digests.keys() - registered_digests.keys()):
            issues.append(f"WRITE_REGISTRY_SOURCE_DIGEST_UNREGISTERED:{path}")
        for path in sorted(registered_digests.keys() - discovered_digests.keys()):
            issues.append(f"WRITE_REGISTRY_SOURCE_DIGEST_STALE:{path}")
        for path in sorted(discovered_digests.keys() & registered_digests.keys()):
            if discovered_digests[path] != registered_digests[path]:
                issues.append(f"WRITE_REGISTRY_SOURCE_DIGEST_MISMATCH:{path}")
    if registry.get("schema_version") != "1.0":
        issues.append("WRITE_REGISTRY_SCHEMA_VERSION_INVALID")
    issues.extend(discover_source_scan_issues(root))
    declared_entrypoints = registry.get("declared_entrypoints")
    if not isinstance(declared_entrypoints, dict):
        issues.append("WRITE_REGISTRY_DECLARED_ENTRYPOINTS_INVALID")
    elif declared_entrypoints != discover_declared_entrypoints(root):
        issues.append("WRITE_REGISTRY_DECLARED_ENTRYPOINTS_MISMATCH")
    declaration_records = registry.get("declared_entrypoint_records")
    if not isinstance(declaration_records, dict):
        issues.append("WRITE_REGISTRY_DECLARED_RECORDS_INVALID")
    elif isinstance(declared_entrypoints, dict):
        if set(declaration_records) != set(declared_entrypoints):
            issues.append("WRITE_REGISTRY_DECLARED_RECORDS_MISMATCH")
        for declaration, raw_record in declaration_records.items():
            if not isinstance(raw_record, dict):
                issues.append(f"WRITE_REGISTRY_DECLARED_RECORD_INVALID:{declaration}")
                continue
            missing = sorted(_REQUIRED_DECLARATION_FIELDS - raw_record.keys())
            if missing:
                issues.append(f"WRITE_REGISTRY_DECLARED_FIELDS_MISSING:{declaration}:{','.join(missing)}")
                continue
            if raw_record.get("command") != declared_entrypoints.get(declaration):
                issues.append(f"WRITE_REGISTRY_DECLARED_COMMAND_MISMATCH:{declaration}")
            if raw_record.get("owner") not in _ALLOWED_OWNERS:
                issues.append(f"WRITE_REGISTRY_DECLARED_OWNER_INVALID:{declaration}")
            if raw_record.get("capability") not in _ALLOWED_CAPABILITIES:
                issues.append(f"WRITE_REGISTRY_DECLARED_CAPABILITY_INVALID:{declaration}")
            if raw_record.get("status") not in _ALLOWED_STATUS:
                issues.append(f"WRITE_REGISTRY_DECLARED_STATUS_INVALID:{declaration}")
            negative_test = raw_record.get("negative_test")
            if not isinstance(negative_test, str) or not _negative_test_reference_exists(negative_test, root):
                issues.append(f"WRITE_REGISTRY_DECLARED_NEGATIVE_TEST_MISSING:{declaration}")
            elif negative_test != _bound_negative_test("declaration", declaration):
                issues.append(f"WRITE_REGISTRY_DECLARED_NEGATIVE_TEST_UNBOUND:{declaration}")
            if negative_test == _GENERIC_NEGATIVE_TEST:
                issues.append(f"WRITE_REGISTRY_DECLARED_NEGATIVE_TEST_GENERIC:{declaration}")
            if (
                not isinstance(raw_record.get("expected_rejection"), str)
                or not raw_record["expected_rejection"].strip()
            ):
                issues.append(f"WRITE_REGISTRY_DECLARED_REJECTION_EMPTY:{declaration}")

    network_imports = registry.get("network_imports")
    registered_network_imports: dict[str, int] = {}
    network_ids: set[str] = set()
    if not isinstance(network_imports, list):
        issues.append("WRITE_REGISTRY_NETWORK_IMPORTS_INVALID")
    else:
        for index, raw_import in enumerate(network_imports):
            if not isinstance(raw_import, dict):
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_NOT_OBJECT:{index}")
                continue
            missing = sorted(_REQUIRED_NETWORK_IMPORT_FIELDS - raw_import.keys())
            if missing:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_FIELDS_MISSING:{index}:{','.join(missing)}")
                continue
            source = raw_import["source"]
            import_id = raw_import["id"]
            occurrences = raw_import["occurrences"]
            if not isinstance(import_id, str) or not import_id or import_id in network_ids:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_ID_INVALID:{index}")
                continue
            network_ids.add(import_id)
            if not isinstance(source, str) or not source or source in registered_network_imports:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_SOURCE_INVALID:{index}")
                continue
            if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences < 1:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_COUNT_INVALID:{source}")
                continue
            registered_network_imports[source] = occurrences
            if raw_import["owner"] not in _ALLOWED_OWNERS:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_OWNER_INVALID:{source}")
            if raw_import["purpose"] not in _ALLOWED_NETWORK_PURPOSES:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_PURPOSE_INVALID:{source}")
            network_status = raw_import["status"]
            if network_status not in {"HARD_HOLD", "READ_ONLY"}:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_STATUS_INVALID:{source}")
            expected_network_rejection = (
                "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
                if network_status == "HARD_HOLD"
                else "EXTERNAL_WRITE_NOT_AUTHORIZED"
            )
            if raw_import["expected_rejection"] != expected_network_rejection:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_REJECTION_MISMATCH:{source}")
            negative_test = raw_import["negative_test"]
            if not isinstance(negative_test, str) or not _negative_test_reference_exists(negative_test, root):
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_NEGATIVE_TEST_MISSING:{source}")
            elif negative_test != _bound_negative_test("network", import_id):
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_NEGATIVE_TEST_UNBOUND:{source}")
        discovered_network_imports = discover_network_imports(root)
        for source in sorted(discovered_network_imports.keys() - registered_network_imports.keys()):
            issues.append(f"WRITE_REGISTRY_UNREGISTERED_NETWORK_IMPORT:{source}")
        for source in sorted(registered_network_imports.keys() - discovered_network_imports.keys()):
            issues.append(f"WRITE_REGISTRY_STALE_NETWORK_IMPORT:{source}")
        for source in sorted(discovered_network_imports.keys() & registered_network_imports.keys()):
            if discovered_network_imports[source] != registered_network_imports[source]:
                issues.append(f"WRITE_REGISTRY_NETWORK_IMPORT_COUNT_MISMATCH:{source}")

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
        if raw_entry["owner"] not in _ALLOWED_OWNERS:
            issues.append(f"WRITE_REGISTRY_OWNER_INVALID:{entry_id}")
        for field in ("kind", "owner", "capability", "call_graph", "negative_test"):
            if not isinstance(raw_entry[field], str) or not raw_entry[field].strip():
                issues.append(f"WRITE_REGISTRY_FIELD_EMPTY:{entry_id}:{field}")
        if raw_entry["expected_rejection"] not in _ALLOWED_REJECTIONS:
            issues.append(f"WRITE_REGISTRY_REJECTION_INVALID:{entry_id}")
        elif raw_entry["expected_rejection"] != _expected_entry_rejection(
            str(raw_entry["status"]), str(raw_entry["capability"])
        ):
            issues.append(f"WRITE_REGISTRY_REJECTION_MISMATCH:{entry_id}")
        negative_test = raw_entry["negative_test"]
        if isinstance(negative_test, str) and not _negative_test_reference_exists(negative_test, root):
            issues.append(f"WRITE_REGISTRY_NEGATIVE_TEST_MISSING:{entry_id}")
        elif negative_test != _bound_negative_test("entry", entry_id):
            issues.append(f"WRITE_REGISTRY_NEGATIVE_TEST_UNBOUND:{entry_id}")
        if negative_test == _GENERIC_NEGATIVE_TEST:
            issues.append(f"WRITE_REGISTRY_NEGATIVE_TEST_GENERIC:{entry_id}")
        if not isinstance(raw_entry["call_graph"], str) or "->" not in raw_entry["call_graph"]:
            issues.append(f"WRITE_REGISTRY_CALL_GRAPH_INVALID:{entry_id}")

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
        if raw_path["status"] not in {"HARD_HOLD", "READ_ONLY"}:
            issues.append(f"WRITE_REGISTRY_TERMINAL_STATUS_INVALID:{path_id}")
        if raw_path["capability"] not in _TERMINAL_CAPABILITIES:
            issues.append(f"WRITE_REGISTRY_TERMINAL_CAPABILITY_INVALID:{path_id}")
        if raw_path["owner"] not in _ALLOWED_OWNERS:
            issues.append(f"WRITE_REGISTRY_OWNER_INVALID:{path_id}")
        for field in ("owner", "capability", "call_graph", "negative_test"):
            if not isinstance(raw_path[field], str) or not raw_path[field].strip():
                issues.append(f"WRITE_REGISTRY_TERMINAL_FIELD_EMPTY:{path_id}:{field}")
        if raw_path["expected_rejection"] not in _ALLOWED_REJECTIONS:
            issues.append(f"WRITE_REGISTRY_REJECTION_INVALID:{path_id}")
        elif raw_path["status"] == "READ_ONLY":
            expected_terminal_rejection = "EXTERNAL_WRITE_NOT_AUTHORIZED"
            if raw_path["expected_rejection"] != expected_terminal_rejection:
                issues.append(f"WRITE_REGISTRY_REJECTION_MISMATCH:{path_id}")
        elif raw_path["expected_rejection"] != (
            "CONTROL_AUTHORITY_REQUIRED"
            if raw_path["capability"] == "CONTROL_RESUME_AUTHORITY_REQUIRED"
            else "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
        ):
            issues.append(f"WRITE_REGISTRY_REJECTION_MISMATCH:{path_id}")
        negative_test = raw_path["negative_test"]
        if isinstance(negative_test, str) and not _negative_test_reference_exists(negative_test, root):
            issues.append(f"WRITE_REGISTRY_TERMINAL_NEGATIVE_TEST_MISSING:{path_id}")
        elif negative_test != _bound_negative_test("terminal", path_id):
            issues.append(f"WRITE_REGISTRY_TERMINAL_NEGATIVE_TEST_UNBOUND:{path_id}")
        if negative_test == _GENERIC_NEGATIVE_TEST:
            issues.append(f"WRITE_REGISTRY_NEGATIVE_TEST_GENERIC:{path_id}")
        if not isinstance(raw_path["call_graph"], str) or "->" not in raw_path["call_graph"]:
            issues.append(f"WRITE_REGISTRY_CALL_GRAPH_INVALID:{path_id}")

    discovered_calls = discover_terminal_write_calls(root)
    for source in sorted(discovered_calls.keys() - registered_calls.keys()):
        issues.append(f"WRITE_REGISTRY_UNREGISTERED_TERMINAL_PATH:{source}")
    for source in sorted(registered_calls.keys() - discovered_calls.keys()):
        issues.append(f"WRITE_REGISTRY_STALE_TERMINAL_PATH:{source}")
    for source in sorted(discovered_calls.keys() & registered_calls.keys()):
        if discovered_calls[source] != registered_calls[source]:
            issues.append(f"WRITE_REGISTRY_TERMINAL_COUNT_MISMATCH:{source}")
    return sorted(set(issues))


def run_negative_test_gate(registry: dict[str, Any], *, root: Path) -> list[str]:
    """Execute every referenced behavioral negative test as a separate gate."""

    references: set[str] = set()
    for section in (
        registry.get("entries", []),
        registry.get("network_imports", []),
        registry.get("terminal_write_paths", []),
    ):
        if isinstance(section, list):
            references.update(
                item["negative_test"]
                for item in section
                if isinstance(item, dict) and isinstance(item.get("negative_test"), str)
            )
    declaration_records = registry.get("declared_entrypoint_records", {})
    if isinstance(declaration_records, dict):
        references.update(
            item["negative_test"]
            for item in declaration_records.values()
            if isinstance(item, dict) and isinstance(item.get("negative_test"), str)
        )
    behavioral_tests = registry.get("behavioral_negative_tests", [])
    if isinstance(behavioral_tests, list):
        references.update(reference for reference in behavioral_tests if isinstance(reference, str))
    if not references or _GENERIC_NEGATIVE_TEST in references:
        return ["WRITE_REGISTRY_NEGATIVE_TEST_GATE_INVALID"]
    result = subprocess.run(  # noqa: S603 - validated repository-local pytest node IDs
        [sys.executable, "-m", "pytest", *sorted(references), "-q"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return ["WRITE_REGISTRY_NEGATIVE_TEST_GATE_FAILED"]
    return []


def main(argv: list[str] | None = None) -> int:
    """Run a read-only registry validation suitable for CI and local dry-runs."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--run-negative-tests", action="store_true")
    args = parser.parse_args(argv)
    registry_path = args.registry or args.root / "config" / "write-capability-registry.json"
    registry = load_registry(registry_path)
    issues = validate_registry(registry, root=args.root)
    if not issues and args.run_negative_tests:
        issues.extend(run_negative_test_gate(registry, root=args.root))
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
    "run_negative_test_gate",
    "validate_registry",
]
