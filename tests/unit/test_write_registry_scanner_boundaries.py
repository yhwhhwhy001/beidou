"""Boundary tests for the read-only executable-surface registry scanner."""

from __future__ import annotations

import json
import plistlib
from copy import deepcopy
from pathlib import Path

import pytest

from beidou_launcher import write_registry as registry


def test_unique_yaml_loader_and_nested_execution_strings() -> None:
    with pytest.raises(registry.DuplicateKeyError):
        registry._load_yaml_unique("a: 1\na: 2\n")

    payload = {
        "command": ["python", "-m", "demo"],
        "nested": [{"uses": "curl -X DELETE https://example.invalid"}],
        "ignored": {"value": "not-an-execution-key"},
    }
    commands = registry._yaml_execution_strings(payload)
    assert "python -m demo" in commands
    assert "curl -X DELETE https://example.invalid" in commands


def test_entrypoint_and_network_discovery_cover_declarative_and_dynamic_surfaces(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project.scripts]\nbeidou-demo = 'apps.demo:main'\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "all:\n\tpython -m apps.demo\n\nwrite:\n\tcurl -X POST https://example.invalid\n",
        encoding="utf-8",
    )
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("run: python -m pytest\n", encoding="utf-8")
    (tmp_path / "surface.py").write_text(
        "import httpx\nfrom urllib.request import urlopen\nimport importlib\n"
        "sock = __import__('socket')\nmod = importlib.import_module('requests')\n"
        "async def f():\n    await asyncio.open_connection('127.0.0.1', 1)\n",
        encoding="utf-8",
    )
    assert "pyproject.toml" in registry.discover_sensitive_entry_paths(tmp_path)
    assert "Makefile" in registry.discover_sensitive_entry_paths(tmp_path)
    assert ".github/workflows/ci.yml" in registry.discover_sensitive_entry_paths(tmp_path)
    declarations = registry.discover_declared_entrypoints(tmp_path)
    assert declarations["console:beidou-demo"] == "apps.demo:main"
    assert declarations["make:write"].startswith("curl")
    imports = registry.discover_network_imports(tmp_path)
    assert any(key.endswith("::httpx") for key in imports)
    assert any(key.endswith("::urllib.request") for key in imports)
    assert any(key.endswith("::asyncio.open_connection") for key in imports)


def test_source_digests_and_entry_surface_visitor_cover_aliases_and_skips(tmp_path: Path) -> None:
    (tmp_path / "runtime.py").write_text(
        "from beidou_core.engine import AutonomousEngine as Engine\n"
        "factory = Engine\n\nclass Demo:\n    def run(self):\n        return factory([])\n"
        "mapping = {'engine': Engine}\ncallback = lambda: Engine([])\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ignored.py").write_text("broken(", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "data.json").write_text("{}", encoding="utf-8")
    discovered = registry.discover_sensitive_entry_paths(tmp_path)
    assert "runtime.py" in discovered
    digests = registry.discover_governed_source_digests(tmp_path)
    assert "runtime.py" in digests
    assert "docs/ignored.py" not in digests
    assert "config/data.json" in digests

    tree = registry.ast.parse(
        "from beidou_exchange import create_order\n"
        "def f(x):\n"
        "    request = x.request\n"
        "    request('POST', '/order')\n"
        "    return getattr(x, 'cancel_order')\n"
        "create_order(x)\n"
    )
    visitor = registry._TerminalWriteCallVisitor("runtime.py")
    visitor.visit(tree)
    assert visitor.calls
    assert any("create_order" in key or "request" in key or "cancel_order" in key for key in visitor.calls)


def test_shell_plist_make_yaml_and_cron_write_discovery(tmp_path: Path) -> None:
    shell = tmp_path / "run.sh"
    shell.write_text(
        "#!/bin/sh\n"
        "CURL=curl\n"
        "$CURL -X DELETE https://example.invalid \\\n+--header x\n"
        "python -m demo --transport httpx\n",
        encoding="utf-8",
    )
    plist = tmp_path / "run.plist"
    with plist.open("wb") as handle:
        plistlib.dump({"ProgramArguments": ["curl", "-d", "x", "https://example.invalid"]}, handle)
    makefile = tmp_path / "Makefile"
    makefile.write_text("CURL := /usr/bin/curl\nwrite:\n\t$(CURL) -X PUT https://example.invalid\n", encoding="utf-8")
    yaml_path = tmp_path / "workflow.yaml"
    yaml_path.write_text(
        "steps:\n"
        "  - run: 'curl -X PATCH https://example.invalid'\n"
        "  - command: 'python -m pip install demo'\n"
        "  - uses: 'owner/action@v1'\n",
        encoding="utf-8",
    )
    cron = tmp_path / "nightly.cron"
    cron.write_text("0 * * * * curl -X POST https://example.invalid\n", encoding="utf-8")

    assert registry._curl_method("curl -X DELETE url") == "DELETE"
    assert registry._curl_method("curl --data body url") == "POST"
    assert registry._curl_method("curl -X GET url") == "DYNAMIC"
    assert registry._curl_method("echo no-network") is None
    assert registry._discover_shell_write_calls(shell, "run.sh")
    assert registry._discover_plist_write_calls(plist, "run.plist")
    assert registry._discover_make_write_calls(makefile)
    assert registry._discover_yaml_write_calls(yaml_path, "workflow.yaml")
    assert registry._discover_cron_write_calls(cron, "nightly.cron")
    calls = registry.discover_terminal_write_calls(tmp_path)
    assert any("curl[DELETE]" in key for key in calls)
    assert any("remote_action" in key or "package_network" in key for key in calls)


def test_source_scan_and_registry_load_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "broken.sh").write_text("#!/bin/sh\nif then\n", encoding="utf-8")
    (tmp_path / "broken.plist").write_bytes(b"not plist")
    (tmp_path / "broken.yaml").write_text("a: 1\na: 2\n", encoding="utf-8")
    issues = registry.discover_source_scan_issues(tmp_path)
    assert any("broken.py" in issue for issue in issues)
    assert any("broken.sh" in issue for issue in issues)
    assert any("broken.plist" in issue for issue in issues)
    assert any("broken.yaml" in issue for issue in issues)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="WRITE_REGISTRY_INVALID_ROOT"):
        registry.load_registry(invalid)
    invalid.write_text('{"entries": [], "entries": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="WRITE_REGISTRY_DUPLICATE_KEY"):
        registry.load_registry(invalid)


def test_negative_reference_binding_and_governance_digest(tmp_path: Path) -> None:
    test_file = tmp_path / "test_contract.py"
    test_file.write_text("def test_exists():\n    pass\n\nasync def test_async():\n    pass\n", encoding="utf-8")
    assert registry._negative_test_reference_exists("test_contract.py::test_exists", tmp_path)
    assert registry._negative_test_reference_exists("test_contract.py::test_async[param]", tmp_path)
    assert registry._negative_test_reference_exists("test_contract.py", tmp_path) is False
    assert registry._negative_test_reference_exists("../outside.py::test_exists", tmp_path) is False
    assert registry._negative_test_reference_exists("test_contract.py::missing", tmp_path) is False
    bound = registry._bound_negative_test("entry", "id with spaces/中文")
    assert "test_entry_record_is_behaviorally_bound" in bound
    data = {"entries": [], "network_imports": [], "terminal_write_paths": []}
    assert len(registry.compute_governance_digest(data)) == 64


def test_validate_registry_reports_structural_failures_without_throwing(tmp_path: Path) -> None:
    root = tmp_path
    registry_payload = {
        "schema_version": "bad",
        "governance_digest": "bad",
        "behavioral_negative_tests": [],
        "governed_source_digests": {},
        "declared_entrypoints": [],
        "declared_entrypoint_records": [],
        "network_imports": [{"id": "dup", "source": "x", "occurrences": 0}],
        "entries": [None, {"id": "x"}],
        "terminal_write_paths": [None, {"id": "x"}],
    }
    issues = registry.validate_registry(registry_payload, root=root)
    assert "WRITE_REGISTRY_SCHEMA_VERSION_INVALID" in issues
    assert "WRITE_REGISTRY_BEHAVIORAL_NEGATIVE_TESTS_INVALID" in issues
    assert any(issue.startswith("WRITE_REGISTRY_ENTRY_NOT_OBJECT") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_TERMINAL_PATH_NOT_OBJECT") for issue in issues)


def test_negative_test_gate_rejects_empty_or_generic_reference() -> None:
    assert registry.run_negative_test_gate({"entries": []}, root=Path.cwd()) == [
        "WRITE_REGISTRY_NEGATIVE_TEST_GATE_INVALID"
    ]
    assert registry.run_negative_test_gate(
        {"entries": [{"negative_test": registry._GENERIC_NEGATIVE_TEST}]}, root=Path.cwd()
    ) == ["WRITE_REGISTRY_NEGATIVE_TEST_GATE_INVALID"]


def test_registry_main_reports_invalid_file(tmp_path: Path, capsys) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    code = registry.main(["--root", str(tmp_path), "--registry", str(path)])
    assert code == 2
    assert '"status": "FAIL"' in capsys.readouterr().out


def test_registry_scanner_branch_matrix_and_ast_aliases(tmp_path: Path) -> None:
    assert registry._is_repository_local_only(Path(".superpowers/run.json"))
    assert registry._is_repository_local_only(Path("artifacts/evidence/report.json"))
    assert registry._is_repository_local_only(Path("config/policies/rules.yaml"))
    assert registry._is_repository_local_only(Path("config/env.local.yaml"))
    assert not registry._is_repository_local_only(Path("config/env.template.yaml"))
    assert registry._expected_entry_rejection("DELEGATE_ONLY", "LOCAL_OBSERVATION") == "CANONICAL_LAUNCHER_REQUIRED"
    assert registry._expected_entry_rejection("READ_ONLY", "LOCAL_OBSERVATION") == "EXTERNAL_WRITE_NOT_AUTHORIZED"
    assert (
        registry._expected_entry_rejection("HARD_HOLD", "CONTROL_RESUME_AUTHORITY_REQUIRED")
        == "CONTROL_AUTHORITY_REQUIRED"
    )
    assert registry._expected_entry_rejection("HARD_HOLD", "LEGACY_TRADE_TOOL") == "NONCANONICAL_ENTRYPOINT_HELD"
    assert (
        registry._expected_entry_rejection("HARD_HOLD", "LOCAL_OBSERVATION") == "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
    )

    constant_tree = registry.ast.parse("x = 'left' + ('right' + 'end')\ny = name\n")
    assert registry._constant_string(constant_tree.body[0].value) == "leftrightend"
    assert registry._constant_string(constant_tree.body[1].value) is None

    source = """
from beidou_exchange import create_order as make_order
import httpx
import importlib

class Surface:
    def method(self, client, value):
        direct = client.request
        attr = self.submit
        item = {'post': client.post, 'dynamic': value}
        values = [client.cancel_order, value]
        result = direct('POST', '/x')
        getattr(client, 'delete')('/x')
        getattr(client, name)('/x')
        client.__getattribute__('create_order')('/x')
        client.__getattribute__(name)('/x')
        client['create_order']('/x')
        setattr(client, 'request', client.request)
        return result, attr, item, values

async def async_surface(client):
    return client.create_algo_order('/x')

factory = make_order
factory('/x')
getattr(client, 'cancel_order')('/x')
__import__('httpx')
"""
    visitor = registry._TerminalWriteCallVisitor("surface.py")
    visitor.visit(registry.ast.parse(source))
    assert visitor.calls
    assert any("getattr" in key or "partial" in key or "callable_argument" in key for key in visitor.calls)
    assert registry._TerminalWriteCallVisitor._expression_key(registry.ast.parse('a.b["x"]').body[0].value) == "a.b[x]"
    assert registry._TerminalWriteCallVisitor._expression_key(registry.ast.parse("1").body[0].value) == ""
    visitor = registry._TerminalWriteCallVisitor("surface.py")
    assert visitor._getattr_target(registry.ast.parse("getattr(x, name)").body[0].value) == "DYNAMIC"
    assert visitor._getattr_target(registry.ast.parse("getattr(x, 'POST')").body[0].value) == "http[POST]"
    assert (
        visitor._reflective_target(registry.ast.parse("x.__getattribute__(x, 'create_order')").body[0].value)
        == "reflective[create_order]"
    )
    assert visitor._reflective_target(registry.ast.parse("x.method()").body[0].value) == ""
    visitor.aliases[-1]["x"] = "request"
    assert visitor._reference_target(registry.ast.parse("x").body[0].value) == "request"
    assert visitor._reference_target(registry.ast.parse("aiohttp").body[0].value).startswith("network_module")
    assert visitor._reference_target(registry.ast.parse("vars(x)").body[0].value) == "vars[request]"
    assert visitor._reference_target(registry.ast.parse("x.get('k')").body[0].value) == "container_extract[request]"
    assert visitor._reference_target(registry.ast.parse("lambda: x").body[0].value) == "request"
    assert visitor._reference_target(registry.ast.parse("[x]").body[0].value) == "request"
    assert visitor._reference_target(registry.ast.parse("[x, aiohttp]").body[0].value) == "DYNAMIC"
    assert visitor._reference_target(registry.ast.parse("{}").body[0].value) == ""
    assert visitor._explicit_callable_reference(registry.ast.parse("x").body[0].value) == "request"
    assert visitor._explicit_callable_reference(registry.ast.parse("x.create_order").body[0].value) == "create_order"
    assert visitor._explicit_callable_reference(registry.ast.parse("1").body[0].value) == ""
    assert registry._TerminalWriteCallVisitor._is_direct_http_client(registry.ast.parse("httpx.post").body[0].value)
    assert registry._TerminalWriteCallVisitor._is_direct_http_client(
        registry.ast.parse("httpx.Client().post").body[0].value
    )
    assert not registry._TerminalWriteCallVisitor._is_direct_http_client(
        registry.ast.parse("client.post").body[0].value
    )

    assert registry._logical_shell_lines("one \\\ntwo\nlast\\\n") == ["one two", "last"]
    assert registry._curl_method("curl --request PATCH url") == "PATCH"
    assert registry._curl_method("curl --request GET url") == "DYNAMIC"
    assert registry._curl_method("curl -F field=value url") == "POST"


def test_validate_registry_walks_complete_record_validation(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "beidou_launcher").mkdir()
    (tmp_path / "beidou_launcher" / "cli.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_negative_test_reference_exists", lambda *_args: True)
    monkeypatch.setattr(registry, "_bound_negative_test", lambda *_args: "tests/unit/test_contract.py::test_bound")
    monkeypatch.setattr(registry, "discover_governed_source_digests", lambda _root: {"a.py": "a" * 64})
    monkeypatch.setattr(registry, "discover_source_scan_issues", lambda _root: [])
    monkeypatch.setattr(registry, "discover_declared_entrypoints", lambda _root: {"console:demo": "demo:main"})
    monkeypatch.setattr(registry, "discover_network_imports", lambda _root: {"a.py::httpx": 2})
    monkeypatch.setattr(registry, "discover_sensitive_entry_paths", lambda _root: {"beidou_launcher/cli.py"})
    monkeypatch.setattr(
        registry, "discover_terminal_write_calls", lambda _root: {"engine.py::<module>::request[POST]": 1}
    )

    negative = "tests/unit/test_contract.py::test_bound"
    declaration = {
        "command": "demo:main",
        "owner": "Engineering Owner",
        "capability": "LOCAL_OBSERVATION",
        "status": "READ_ONLY",
        "negative_test": negative,
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    }
    network = {
        "id": "net-1",
        "source": "a.py::httpx",
        "occurrences": 2,
        "owner": "Engineering Owner",
        "purpose": "EXCHANGE_TRANSPORT",
        "status": "READ_ONLY",
        "negative_test": negative,
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    }
    entry = {
        "id": "cli",
        "path": "beidou_launcher/cli.py",
        "kind": "runtime",
        "owner": "Engineering Owner",
        "capability": "WRITABLE_RUNTIME_CANDIDATE",
        "status": "HARD_HOLD",
        "call_graph": "cli -> engine",
        "negative_test": negative,
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    }
    terminal = {
        "id": "terminal-1",
        "source": "engine.py::<module>::request[POST]",
        "occurrences": 1,
        "owner": "Engineering Owner",
        "capability": "TERMINAL_CREATE_SCOPE_REQUIRED",
        "status": "HARD_HOLD",
        "call_graph": "engine -> request",
        "negative_test": negative,
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    }
    payload = {
        "schema_version": "1.0",
        "behavioral_negative_tests": sorted(registry._REQUIRED_BEHAVIORAL_NEGATIVE_TESTS),
        "governed_source_digests": {"a.py": "a" * 64},
        "declared_entrypoints": {"console:demo": "demo:main"},
        "declared_entrypoint_records": {"console:demo": declaration},
        "network_imports": [network],
        "entries": [entry],
        "terminal_write_paths": [terminal],
    }
    payload["governance_digest"] = registry.compute_governance_digest(payload)
    assert registry.validate_registry(payload, root=tmp_path) == []

    malformed = deepcopy(payload)
    malformed["governance_digest"] = "bad"
    malformed["schema_version"] = "0"
    malformed["declared_entrypoints"] = []
    malformed["declared_entrypoint_records"] = {"console:demo": {"command": "bad"}, "extra": "bad"}
    malformed["network_imports"] = [None, {"id": "", "source": "a.py::httpx", "occurrences": True}]
    malformed["entries"] = [
        None,
        {"id": "duplicate"},
        {
            **entry,
            "id": "cli",
            "path": "../outside",
            "status": "bad",
            "owner": "bad",
            "capability": "bad",
            "expected_rejection": "bad",
            "call_graph": "bad",
            "negative_test": registry._GENERIC_NEGATIVE_TEST,
        },
    ]
    malformed["terminal_write_paths"] = [None, {"id": "", "source": "", "occurrences": False}]
    issues = registry.validate_registry(malformed, root=tmp_path)
    assert "WRITE_REGISTRY_GOVERNANCE_DIGEST_MISMATCH" in issues
    assert "WRITE_REGISTRY_DECLARED_ENTRYPOINTS_INVALID" in issues
    assert any(issue.startswith("WRITE_REGISTRY_NETWORK_IMPORT") for issue in issues)
    assert any(
        issue.startswith("WRITE_REGISTRY_ENTRY_") or issue.startswith("WRITE_REGISTRY_FIELDS_") for issue in issues
    )
    assert any(issue.startswith("WRITE_REGISTRY_TERMINAL_") for issue in issues)


def test_registry_scan_exception_and_negative_gate_paths(monkeypatch, tmp_path: Path) -> None:
    bad_test = tmp_path / "bad.py"
    bad_test.write_text("not valid(", encoding="utf-8")
    assert registry._negative_test_reference_exists("bad.py::test_x", tmp_path) is False
    monkeypatch.setattr(registry.subprocess, "run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})())
    assert registry.run_negative_test_gate(
        {"entries": [{"negative_test": "tests/unit/test_x.py::test_x"}]}, root=tmp_path
    ) == ["WRITE_REGISTRY_NEGATIVE_TEST_GATE_FAILED"]
