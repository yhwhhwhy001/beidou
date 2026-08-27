"""Independent, conservative oracle for the write-capability registry.

This verifier intentionally does not import ``beidou_launcher.write_registry``.
It uses a separate AST/lexical implementation so the primary discoverer cannot
prove its own completeness.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
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
SKIP_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "tests",
}
GENERATED_OUTPUT_PREFIXES = (("delivery", "packages"),)


def _is_repository_local_only(relative: Path) -> bool:
    """Mirror repository policy for ignored evidence and sensitive local config."""

    parts = relative.parts
    if parts[:1] == (".superpowers",):
        return True
    if parts[:2] == ("artifacts", "evidence"):
        return True
    if parts[:2] == ("config", "policies"):
        return True
    return (
        len(parts) == 2
        and parts[0] == "config"
        and parts[1].startswith("env.")
        and parts[1].endswith(".yaml")
        and parts[1] != "env.template.yaml"
    )


def _is_generated_output(relative: Path) -> bool:
    """Exclude generated delivery packages from independent source inventory."""

    return any(relative.parts[: len(prefix)] == prefix for prefix in GENERATED_OUTPUT_PREFIXES)


def _is_skipped(relative: Path) -> bool:
    return (
        _is_generated_output(relative)
        or any(part in SKIP_PARTS for part in relative.parts)
        or _is_repository_local_only(relative)
    )


@dataclass(frozen=True, slots=True)
class OracleFinding:
    path: str
    kind: str
    line: int
    detail: str


def scan_source_digests(root: Path) -> dict[str, str]:
    """Independently enumerate production code and declarative source hashes."""

    root = root.resolve()
    governed_suffixes = {".cron", ".json", ".plist", ".py", ".sh", ".sql", ".toml", ".yaml", ".yml"}
    runtime_source_suffixes = {".cron", ".plist", ".py", ".sh", ".sql", ".toml", ".yaml", ".yml"}
    digests: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        relative_text = relative.as_posix()
        if relative_text == "config/write-capability-registry.json":
            continue
        try:
            data = path.read_bytes()
            executable = bool(path.stat().st_mode & 0o111)
        except OSError:
            continue
        if relative.parts[0] in {".beidou", "evidence"} and not (
            path.suffix.lower() in runtime_source_suffixes or executable or data.startswith(b"#!")
        ):
            continue
        if (
            path.suffix.lower() not in governed_suffixes
            and path.name not in {"Makefile", "Dockerfile"}
            and not path.name.startswith("Dockerfile.")
            and not executable
            and not data.startswith(b"#!")
        ):
            continue
        digests[relative_text] = hashlib.sha256(data).hexdigest()
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
            alias = self.aliases.get(node.id, "")
            if alias:
                return alias
            module = self.module_aliases.get(node.id, "").split(".", 1)[0]
            return f"NETWORK_MODULE:{module}" if module in {"aiohttp", "httpx", "requests", "urllib"} else ""
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call) and _name(node.func) == "getattr":
            value = _constant_string(node.args[1]) if len(node.args) > 1 else None
            return value or "UNRESOLVED_GETATTR"
        if isinstance(node, ast.Call) and _name(node.func) == "vars" and node.args:
            base = self._target(node.args[0])
            return f"NETWORK_CONTAINER:{base}" if base.startswith("NETWORK_MODULE:") else ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "pop"}:
            base = self._target(node.func.value)
            if base.startswith("NETWORK_CONTAINER:"):
                value = _constant_string(node.args[0]) if node.args else None
                return f"NETWORK_METHOD:{(value or 'DYNAMIC').upper()}"
        if isinstance(node, ast.Subscript):
            base = self._target(node.value)
            if base.startswith("NETWORK_CONTAINER:"):
                value = _constant_string(node.slice)
                return f"NETWORK_METHOD:{(value or 'DYNAMIC').upper()}"
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
        elif target.startswith(("NETWORK_CONTAINER:", "NETWORK_METHOD:")):
            for assigned in node.targets:
                if isinstance(assigned, ast.Name):
                    self.aliases[assigned.id] = target
                else:
                    self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"stored:{target}")
        elif isinstance(node.value, ast.Call) and isinstance(node.value.func, (ast.Name, ast.Attribute)):
            constructor_name = _name(node.value.func)
            constructor_base = node.value.func.value if isinstance(node.value.func, ast.Attribute) else None
            base_name = constructor_base.id if isinstance(constructor_base, ast.Name) else ""
            is_network_constructor = target.startswith("NETWORK_CLIENT:") or (
                constructor_name in {"Client", "AsyncClient", "ClientSession"} and base_name in self.module_aliases
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
            if target in CONSTRUCTORS | TERMINAL_NAMES | REQUEST_NAMES or target.startswith("NETWORK_METHOD:"):
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
            if argument_target in CONSTRUCTORS | TERMINAL_NAMES | (
                REQUEST_NAMES - {"exchange"}
            ) or argument_target.startswith("NETWORK_METHOD:"):
                self._record(node, "UNRESOLVED_WRITE_CALLABLE", f"callback:{argument_target}")

        if isinstance(node.func, ast.Attribute) and node.func.attr.lower() in {"post", "put", "patch", "delete"}:
            base = node.func.value
            if isinstance(base, ast.Name) and (
                base.id in self.network_clients
                or self.module_aliases.get(base.id, "").split(".", 1)[0] in {"httpx", "requests", "aiohttp"}
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
        if _is_skipped(relative):
            continue
        if relative.as_posix() in {
            "beidou_launcher/write_registry.py",
            "scripts/verify_write_registry.py",
        }:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
            continue
        visitor = _OracleVisitor(relative.as_posix())
        visitor.visit(tree)
        findings.extend(visitor.findings)
        has_network_import = bool(visitor.module_aliases) or any(
            target.startswith(("NETWORK_CLIENT:", "NETWORK_METHOD:", "URLLIB:")) for target in visitor.aliases.values()
        )
        if has_network_import:
            imported_roots = {module.split(".", 1)[0] for module in visitor.module_aliases.values()}
            lexical_tokens = {"__getattribute__", "__import__", "vars("}
            if imported_roots & {"aiohttp", "httpx", "requests", "urllib"}:
                lexical_tokens.update({"getattr(", ".post", ".put", ".patch", ".delete", "Request", "urlopen"})
            if "asyncio" in imported_roots:
                lexical_tokens.add("open_connection")
            if "socket" in imported_roots:
                lexical_tokens.update({"create_connection", "sendall"})
            for line_number, line in enumerate(source.splitlines(), start=1):
                if any(token in line for token in lexical_tokens):
                    findings.append(
                        OracleFinding(relative.as_posix(), "LEXICAL_NETWORK_SURFACE", line_number, "network")
                    )
        for line_number, line in enumerate(source.splitlines(), start=1):
            if "lambda" in line and any(name in line for name in TERMINAL_NAMES | REQUEST_NAMES | {"getattr"}):
                findings.append(OracleFinding(relative.as_posix(), "LEXICAL_CALLABLE_SURFACE", line_number, "lambda"))

    for path in root.rglob("*.sh"):
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
            continue
        logical_source = re.sub(r"\\\r?\n", " ", source)
        curl_aliases = set(
            re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]?(?:[^\s'\"]*/)?curl['\"]?\s*$", source)
        )
        for line_number, line in enumerate(logical_source.splitlines(), start=1):
            for alias in curl_aliases:
                line = re.sub(rf'"?\$(?:\{{{re.escape(alias)}\}}|{re.escape(alias)})"?', "curl", line)
            curl_command = re.search(r"(?:\bcurl\b|\$\([^)]*CURL[^)]*\))", line)
            write_option = re.search(
                r"(?:-X|--request)(?:=|\s+)|(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form)(?:=|\s+)",
                line,
            )
            if curl_command and write_option:
                findings.append(OracleFinding(relative.as_posix(), "SHELL_HTTP_WRITE", line_number, "curl"))
    for path in root.rglob("*.plist"):
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            arguments = payload.get("ProgramArguments", []) if isinstance(payload, dict) else []
            command = " ".join(str(argument) for argument in arguments) if isinstance(arguments, list) else ""
            if re.search(r"\bcurl\b", command) and re.search(r"(?:-d|--data|-F|--form|-X|--request)", command):
                findings.append(OracleFinding(relative.as_posix(), "PLIST_HTTP_WRITE", 0, "curl"))
        except (OSError, plistlib.InvalidFileException) as exc:
            findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
    makefile = root / "Makefile"
    if makefile.is_file():
        make_source = makefile.read_text(encoding="utf-8")
        curl_aliases = set(
            re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(?:[^\s]*/)?curl\s*$", make_source)
        )
        for line_number, line in enumerate(make_source.splitlines(), start=1):
            if not line.startswith("\t"):
                continue
            for alias in curl_aliases:
                line = line.replace(f"$({alias})", "curl").replace(f"${{{alias}}}", "curl")
            if re.search(r"(?:\bcurl\b|\$\([^)]*CURL[^)]*\))", line):
                findings.append(OracleFinding("Makefile", "MAKE_HTTP_WRITE", line_number, "curl"))
            if re.search(r"\b(?:httpx|requests|aiohttp|urllib\.request)\b", line):
                findings.append(OracleFinding("Makefile", "MAKE_EMBEDDED_NETWORK", line_number, "python"))
    for pattern in ("*.yml", "*.yaml"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if _is_skipped(relative):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                findings.append(OracleFinding(relative.as_posix(), "UNREADABLE_SOURCE", 0, type(exc).__name__))
                continue
            for line_number, line in enumerate(source.splitlines(), start=1):
                if re.search(
                    r"\b(?:run|command|entrypoint|script):.*\bcurl\b.*(?:-d|--data|-F|--form|-X|--request)", line
                ):
                    findings.append(OracleFinding(relative.as_posix(), "YAML_HTTP_WRITE", line_number, "curl"))
                if re.search(r"\buses:\s*[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@", line):
                    findings.append(OracleFinding(relative.as_posix(), "YAML_REMOTE_ACTION", line_number, "uses"))
                if re.search(r"\b(?:python\s+-m\s+)?pip\s+install(?:\s|$)", line):
                    findings.append(OracleFinding(relative.as_posix(), "YAML_PACKAGE_NETWORK", line_number, "pip"))
    for path in root.rglob("*.cron"):
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            if re.search(r"\bcurl\b.*(?:-d|--data|-F|--form|-X|--request)", line):
                findings.append(OracleFinding(relative.as_posix(), "CRON_HTTP_WRITE", line_number, "curl"))
    return sorted(findings, key=lambda item: (item.path, item.line, item.kind, item.detail))


def _qualified_scopes_at_line(path: Path, line: int) -> set[str]:
    """Return independently parsed function scopes containing a source line."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return set()
    matches: set[str] = set()

    def walk(nodes: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                walk(node.body, (*parents, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno)
                if node.lineno <= line <= end:
                    matches.add(".".join((*parents, node.name)))
                walk(node.body, (*parents, node.name))

    walk(tree.body)
    return matches


def expected_governance_ids(
    finding: OracleFinding,
    *,
    root: Path,
    registry: dict[str, Any],
) -> set[str]:
    """Derive the only record identities allowed to govern a finding."""

    entries = {
        record["id"]
        for record in registry.get("entries", [])
        if isinstance(record, dict) and record.get("path") == finding.path and isinstance(record.get("id"), str)
    }
    networks = {
        record["id"]
        for record in registry.get("network_imports", [])
        if isinstance(record, dict)
        and str(record.get("source", "")).split("::", 1)[0] == finding.path
        and isinstance(record.get("id"), str)
    }
    scopes = _qualified_scopes_at_line(root / finding.path, finding.line)
    terminals = {
        record["id"]
        for record in registry.get("terminal_write_paths", [])
        if isinstance(record, dict)
        and str(record.get("source", "")).split("::", 1)[0] == finding.path
        and len(str(record.get("source", "")).split("::")) >= 3
        and str(record.get("source", "")).split("::")[1] in scopes
        and isinstance(record.get("id"), str)
    }
    terminal_kinds = {"DIRECT_URLLIB_REQUEST", "NETWORK_TRANSPORT_CALL", "TERMINAL_CALL"}
    entry_kinds = {
        "LEXICAL_CALLABLE_SURFACE",
        "RUNTIME_CONSTRUCTOR",
        "UNRESOLVED_WRITE_CALLABLE",
        "YAML_PACKAGE_NETWORK",
        "YAML_REMOTE_ACTION",
    }
    if finding.kind in terminal_kinds:
        return terminals or entries
    if finding.kind in entry_kinds:
        return entries
    if finding.kind in {"NETWORK_STREAM_CALL", "LEXICAL_NETWORK_SURFACE"}:
        return terminals or networks or entries
    return terminals or entries or networks


def verify_coverage(root: Path, registry: dict[str, Any]) -> list[str]:
    actual = {f"{finding.path}:{finding.line}:{finding.kind}:{finding.detail}" for finding in scan_repository(root)}
    governance_payload = {
        "declared_entrypoint_records": registry.get("declared_entrypoint_records"),
        "entries": registry.get("entries"),
        "governed_source_digests": registry.get("governed_source_digests"),
        "independent_oracle_findings": registry.get("independent_oracle_findings"),
        "network_imports": registry.get("network_imports"),
        "terminal_write_paths": registry.get("terminal_write_paths"),
    }
    governance_digest = hashlib.sha256(
        json.dumps(
            governance_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    governance_issues = []
    if registry.get("governance_digest") != governance_digest:
        governance_issues.append("INDEPENDENT_ORACLE_GOVERNANCE_DIGEST_MISMATCH")
    registered_digests = registry.get("governed_source_digests")
    actual_digests = scan_source_digests(root)
    if not isinstance(registered_digests, dict):
        return [*governance_issues, "INDEPENDENT_ORACLE_SOURCE_DIGESTS_INVALID"]
    digest_issues: list[str] = []
    for path in sorted(actual_digests.keys() - registered_digests.keys()):
        digest_issues.append(f"INDEPENDENT_ORACLE_SOURCE_UNREGISTERED:{path}")
    for path in sorted(registered_digests.keys() - actual_digests.keys()):
        digest_issues.append(f"INDEPENDENT_ORACLE_SOURCE_STALE:{path}")
    for path in sorted(actual_digests.keys() & registered_digests.keys()):
        if actual_digests[path] != registered_digests[path]:
            digest_issues.append(f"INDEPENDENT_ORACLE_SOURCE_MISMATCH:{path}")
    declared = registry.get("independent_oracle_findings")
    required = {"identity", "governance_id", "owner", "status", "negative_test"}
    if not isinstance(declared, list) or not all(
        isinstance(item, dict) and required <= item.keys() for item in declared
    ):
        return [*governance_issues, *digest_issues, "INDEPENDENT_ORACLE_DECLARATIONS_INVALID"]
    identities = [item["identity"] for item in declared]
    if not all(isinstance(identity, str) and identity for identity in identities):
        return [*governance_issues, *digest_issues, "INDEPENDENT_ORACLE_DECLARATIONS_INVALID"]
    expected = set(identities)
    issues = [*governance_issues, *digest_issues]
    issues.extend(f"INDEPENDENT_ORACLE_UNREGISTERED:{identity}" for identity in sorted(actual - expected))
    issues.extend(f"INDEPENDENT_ORACLE_STALE:{identity}" for identity in sorted(expected - actual))
    if len(expected) != len(identities):
        issues.append("INDEPENDENT_ORACLE_DUPLICATE_DECLARATION")
    governed_records = {
        item.get("id"): item
        for section in ("entries", "terminal_write_paths", "network_imports")
        for item in registry.get(section, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for declaration in declared:
        identity = declaration["identity"]
        governance_id = declaration["governance_id"]
        record = governed_records.get(governance_id)
        if record is None:
            issues.append(f"INDEPENDENT_ORACLE_GOVERNANCE_MISSING:{identity}")
            continue
        identity_parts = identity.split(":", 3)
        if len(identity_parts) != 4 or not identity_parts[1].isdigit():
            issues.append(f"INDEPENDENT_ORACLE_IDENTITY_INVALID:{identity}")
            continue
        finding = OracleFinding(
            path=identity_parts[0],
            line=int(identity_parts[1]),
            kind=identity_parts[2],
            detail=identity_parts[3],
        )
        allowed_governance = expected_governance_ids(finding, root=root, registry=registry)
        if governance_id not in allowed_governance:
            issues.append(f"INDEPENDENT_ORACLE_GOVERNANCE_SCOPE_MISMATCH:{identity}")
        finding_path = identity.split(":", 1)[0]
        record_path = record.get("path") or str(record.get("source", "")).split("::", 1)[0]
        if finding_path != record_path:
            issues.append(f"INDEPENDENT_ORACLE_GOVERNANCE_PATH_MISMATCH:{identity}")
        for field in ("owner", "status", "negative_test"):
            if declaration.get(field) != record.get(field):
                issues.append(f"INDEPENDENT_ORACLE_GOVERNANCE_{field.upper()}_MISMATCH:{identity}")
        if declaration.get("status") not in {"HARD_HOLD", "READ_ONLY"}:
            issues.append(f"INDEPENDENT_ORACLE_GOVERNANCE_STATUS_INVALID:{identity}")
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
