"""Adversarial registry records exercise every validation failure family."""

from __future__ import annotations

import ast
from pathlib import Path

from beidou_launcher import write_registry as registry


def _entry(
    *,
    entry_id: object = "entry",
    path: object = "entry.py",
    capability: object = "LOCAL_OBSERVATION",
    status: object = "READ_ONLY",
    owner: object = "Engineering Owner",
    call_graph: object = "entry -> runtime",
    negative_test: object = "missing",
    expected_rejection: object = "EXTERNAL_WRITE_NOT_AUTHORIZED",
) -> dict[str, object]:
    return {
        "id": entry_id,
        "path": path,
        "kind": "python",
        "owner": owner,
        "capability": capability,
        "status": status,
        "call_graph": call_graph,
        "negative_test": negative_test,
        "expected_rejection": expected_rejection,
    }


def _declaration(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "command": "wrong",
        "owner": "bad-owner",
        "capability": "bad-capability",
        "status": "bad-status",
        "negative_test": "missing",
        "expected_rejection": "",
    }
    result.update(overrides)
    return result


def _network(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "network",
        "source": "network-source",
        "occurrences": 1,
        "owner": "Engineering Owner",
        "purpose": "EXCHANGE_TRANSPORT",
        "status": "READ_ONLY",
        "negative_test": "missing",
        "expected_rejection": "EXTERNAL_WRITE_NOT_AUTHORIZED",
    }
    result.update(overrides)
    return result


def _terminal(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "id": "terminal",
        "source": "terminal-source",
        "occurrences": 1,
        "owner": "Engineering Owner",
        "capability": "TERMINAL_WRITE_BOUNDARY",
        "status": "HARD_HOLD",
        "call_graph": "entry -> transport",
        "negative_test": "missing",
        "expected_rejection": "WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    }
    result.update(overrides)
    return result


def test_validate_registry_covers_declarations_networks_entries_and_terminal_paths(monkeypatch) -> None:
    root = Path.cwd()
    monkeypatch.setattr(
        registry,
        "discover_governed_source_digests",
        lambda _root: {"common.py": "a" * 64, "new.py": "b" * 64},
    )
    monkeypatch.setattr(registry, "discover_source_scan_issues", lambda _root: ["scan-warning"])
    monkeypatch.setattr(registry, "discover_declared_entrypoints", lambda _root: {"console:demo": "actual"})
    monkeypatch.setattr(
        registry,
        "discover_network_imports",
        lambda _root: {"src": 2, "sbad": 3, "network-new": 1},
    )
    monkeypatch.setattr(
        registry,
        "discover_sensitive_entry_paths",
        lambda _root: {"entry.py", "new-entry.py"},
    )
    monkeypatch.setattr(
        registry,
        "discover_terminal_write_calls",
        lambda _root: {"src": 2, "terminal-new": 1},
    )
    monkeypatch.setattr(
        registry,
        "_negative_test_reference_exists",
        lambda reference, _root: reference in {"bound", registry._GENERIC_NEGATIVE_TEST},
    )

    entries = [
        None,
        {},
        _entry(entry_id=1, path=1),
        _entry(entry_id="duplicate", path="duplicate.py"),
        _entry(entry_id="duplicate", path="duplicate.py"),
        _entry(
            entry_id="outside",
            path="../outside.py",
            capability="not-a-capability",
            status="not-a-status",
            owner="not-an-owner",
            call_graph="",
            negative_test="missing",
            expected_rejection="not-a-rejection",
        ),
        _entry(
            entry_id="candidate",
            path="beidou_launcher/cli.py",
            capability="WRITABLE_RUNTIME_CANDIDATE",
            status="READ_ONLY",
            negative_test="missing",
            expected_rejection="EXTERNAL_WRITE_NOT_AUTHORIZED",
        ),
    ]
    network_imports = [
        None,
        {},
        _network(id="", source="idbad"),
        _network(id="sourcebad", source=""),
        _network(id="countbad", source="countbad", occurrences=True),
        _network(id="valid", source="src", occurrences=1),
        _network(id="duplicate-source", source="src"),
        _network(
            id="sbad",
            source="sbad",
            occurrences=2,
            owner="bad-owner",
            purpose="bad-purpose",
            status="bad-status",
            negative_test="missing",
            expected_rejection="bad-rejection",
        ),
    ]
    terminal_paths = [
        None,
        {},
        _terminal(id="", source="idbad"),
        _terminal(id="sourcebad", source=""),
        _terminal(id="countbad", source="countbad", occurrences=False),
        _terminal(
            id="bound-terminal",
            source="src",
            owner="bad-owner",
            capability="bad-capability",
            status="READ_ONLY",
            call_graph="",
            negative_test="bound",
            expected_rejection="bad-rejection",
        ),
        _terminal(
            id="stale-terminal",
            source="stale",
            capability="CONTROL_RESUME_AUTHORITY_REQUIRED",
            status="HARD_HOLD",
            negative_test=registry._GENERIC_NEGATIVE_TEST,
            expected_rejection="WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        ),
    ]
    payload = {
        "governance_digest": "wrong",
        "behavioral_negative_tests": list(registry._REQUIRED_BEHAVIORAL_NEGATIVE_TESTS),
        "governed_source_digests": {"common.py": "c" * 64, "stale.py": "d" * 64},
        "schema_version": "bad",
        "declared_entrypoints": {"console:demo": "declared"},
        "declared_entrypoint_records": {
            "bad-record": None,
            "missing": {},
            "full": _declaration(),
        },
        "network_imports": network_imports,
        "entries": entries,
        "terminal_write_paths": terminal_paths,
    }

    issues = registry.validate_registry(payload, root=root)

    expected_fragments = (
        "WRITE_REGISTRY_GOVERNANCE_DIGEST_MISMATCH",
        "WRITE_REGISTRY_BEHAVIORAL_NEGATIVE_TEST_MISSING",
        "WRITE_REGISTRY_SOURCE_DIGEST_UNREGISTERED:new.py",
        "WRITE_REGISTRY_SOURCE_DIGEST_STALE:stale.py",
        "WRITE_REGISTRY_SOURCE_DIGEST_MISMATCH:common.py",
        "WRITE_REGISTRY_DECLARED_RECORD_INVALID:bad-record",
        "WRITE_REGISTRY_DECLARED_FIELDS_MISSING:missing",
        "WRITE_REGISTRY_DECLARED_OWNER_INVALID:full",
        "WRITE_REGISTRY_NETWORK_IMPORT_ID_INVALID",
        "WRITE_REGISTRY_NETWORK_IMPORT_SOURCE_INVALID",
        "WRITE_REGISTRY_NETWORK_IMPORT_COUNT_INVALID",
        "WRITE_REGISTRY_NETWORK_IMPORT_PURPOSE_INVALID:sbad",
        "WRITE_REGISTRY_ENTRY_TYPE_INVALID",
        "WRITE_REGISTRY_DUPLICATE_ID:duplicate",
        "WRITE_REGISTRY_PATH_OUTSIDE_ROOT:outside",
        "WRITE_REGISTRY_WRITABLE_CANDIDATE_NOT_HELD",
        "WRITE_REGISTRY_TERMINAL_ID_INVALID",
        "WRITE_REGISTRY_TERMINAL_SOURCE_INVALID",
        "WRITE_REGISTRY_TERMINAL_COUNT_INVALID",
        "WRITE_REGISTRY_TERMINAL_CAPABILITY_INVALID:bound-terminal",
        "WRITE_REGISTRY_TERMINAL_NEGATIVE_TEST_UNBOUND:bound-terminal",
        "WRITE_REGISTRY_NEGATIVE_TEST_GENERIC:stale-terminal",
        "WRITE_REGISTRY_UNREGISTERED_TERMINAL_PATH:terminal-new",
        "WRITE_REGISTRY_STALE_TERMINAL_PATH:stale",
        "WRITE_REGISTRY_TERMINAL_COUNT_MISMATCH:src",
    )
    assert all(any(issue.startswith(fragment) for issue in issues) for fragment in expected_fragments)


def test_validate_registry_returns_early_for_invalid_terminal_section(monkeypatch) -> None:
    monkeypatch.setattr(registry, "discover_governed_source_digests", lambda _root: {})
    monkeypatch.setattr(registry, "discover_source_scan_issues", lambda _root: [])
    monkeypatch.setattr(registry, "discover_declared_entrypoints", lambda _root: {})
    monkeypatch.setattr(registry, "discover_network_imports", lambda _root: {})
    monkeypatch.setattr(registry, "discover_sensitive_entry_paths", lambda _root: set())
    payload = {
        "governance_digest": "wrong",
        "behavioral_negative_tests": [],
        "governed_source_digests": {},
        "schema_version": "1.0",
        "declared_entrypoints": {},
        "declared_entrypoint_records": {},
        "network_imports": [],
        "entries": [],
        "terminal_write_paths": None,
    }
    issues = registry.validate_registry(payload, root=Path.cwd())
    assert "WRITE_REGISTRY_TERMINAL_PATHS_INVALID" in issues


def test_negative_test_gate_reports_subprocess_success_and_failure(monkeypatch) -> None:
    payload = {"entries": [{"negative_test": "tests/unit/test_something.py::test_behavior"}]}
    monkeypatch.setattr(registry.subprocess, "run", lambda *_args, **_kwargs: type("R", (), {"returncode": 0})())
    assert registry.run_negative_test_gate(payload, root=Path.cwd()) == []
    monkeypatch.setattr(registry.subprocess, "run", lambda *_args, **_kwargs: type("R", (), {"returncode": 1})())
    assert registry.run_negative_test_gate(payload, root=Path.cwd()) == ["WRITE_REGISTRY_NEGATIVE_TEST_GATE_FAILED"]


def test_scanners_cover_skips_parse_failures_and_executable_artifacts(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "check.yml").write_text("run: python -m pytest\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def bad(:\n", encoding="utf-8")
    (tmp_path / "run.sh").write_text("#!/bin/sh\ncurl -X POST https://example.invalid\n", encoding="utf-8")
    (tmp_path / "empty.sh").write_text("", encoding="utf-8")
    (tmp_path / "bad.plist").write_bytes(b"not plist")
    import plistlib

    with (tmp_path / "embedded.plist").open("wb") as handle:
        plistlib.dump({"ProgramArguments": "curl"}, handle)
    with (tmp_path / "network.plist").open("wb") as handle:
        plistlib.dump({"ProgramArguments": ["python", "-m", "requests"]}, handle)
    (tmp_path / "bad.yaml").write_text("a: 1\na: 2\n", encoding="utf-8")
    (tmp_path / "scalar.yaml").write_text("just-a-string\n", encoding="utf-8")
    (tmp_path / "network.yaml").write_text("run: 'python -m requests call'\n", encoding="utf-8")
    (tmp_path / "nightly.cron").write_text("0 * * * * curl -X PUT https://example.invalid\n", encoding="utf-8")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "ignored.sh").write_text("curl -X DELETE url\n", encoding="utf-8")
    (tmp_path / "skip" / "ignored.yaml").write_text("run: curl url\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ignored.py").write_text("broken(:\n", encoding="utf-8")
    (tmp_path / "docs" / "ignored.sh").write_text("curl -X DELETE url\n", encoding="utf-8")
    (tmp_path / "docs" / "ignored.yaml").write_text("run: curl url\n", encoding="utf-8")
    (tmp_path / "docs" / "ignored.plist").write_bytes(b"not plist")
    (tmp_path / "docs" / "ignored.cron").write_text("curl -X DELETE url\n", encoding="utf-8")
    (tmp_path / "surface.py").write_text("def f(client):\n    return client.create_order('x')\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("network:\n\thttpx.post https://example.invalid\n", encoding="utf-8")
    (tmp_path / "folder.yaml").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "write-capability-registry.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".beidou").mkdir()
    (tmp_path / ".beidou" / "runtime.txt").write_text("runtime\n", encoding="utf-8")
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "runtime.txt").write_text("evidence\n", encoding="utf-8")
    (tmp_path / "ignored.bin").write_bytes(b"ignored")
    executable = tmp_path / "operator-tool"
    executable.write_text("binary-ish\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | 0o111)

    sensitive = registry.discover_sensitive_entry_paths(tmp_path)
    assert {"docker-compose.yml", ".pre-commit-config.yaml", "run.sh", "bad.plist", "nightly.cron"} <= sensitive
    assert ".github/workflows/check.yml" in sensitive
    imports = registry.discover_network_imports(tmp_path)
    assert imports == {}
    terminal = registry.discover_terminal_write_calls(tmp_path)
    assert any("curl[POST]" in key for key in terminal)
    assert any("curl[PUT]" in key for key in terminal)
    assert any("create_order" in key for key in terminal)
    assert any("embedded_network" in key for key in terminal)
    monkeypatch.setattr(
        registry,
        "_discover_shell_write_calls",
        lambda *_args: (_ for _ in ()).throw(OSError("shell read")),
    )
    assert registry.discover_terminal_write_calls(tmp_path)
    monkeypatch.setattr(
        registry,
        "_discover_cron_write_calls",
        lambda *_args: (_ for _ in ()).throw(OSError("cron read")),
    )
    assert registry.discover_terminal_write_calls(tmp_path)
    issues = registry.discover_source_scan_issues(tmp_path)
    assert any("bad.py" in issue for issue in issues)
    assert any("empty.sh" in issue for issue in issues)
    assert any("bad.plist" in issue for issue in issues)
    assert any("bad.yaml" in issue for issue in issues)
    assert any("scalar.yaml" in issue for issue in issues)
    digests = registry.discover_governed_source_digests(tmp_path)
    assert "config/write-capability-registry.json" not in digests
    assert ".beidou/runtime.txt" not in digests
    assert "evidence/runtime.txt" not in digests
    assert "operator-tool" in digests

    original_stat = registry.Path.stat

    def stat_with_failure(path, *args, **kwargs):
        if path.name == "stat-error.py":
            raise OSError("stat failed")
        return original_stat(path, *args, **kwargs)

    (tmp_path / "stat-error.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(registry.Path, "stat", stat_with_failure)
    assert "stat-error.py" not in registry.discover_governed_source_digests(tmp_path)


def test_ast_visitors_cover_alias_storage_reflection_and_callable_boundaries() -> None:
    source = """
from beidou_core.engine import AutonomousEngine as Engine
from beidou_exchange import create_order as imported_create
import functools
import httpx
import asyncio

holder.attr = Engine
holder['factory'] = Engine
module_factory = module.AutonomousEngine
factory = Engine
mapping = {'factory': Engine}
factory([])
callback = lambda: Engine([])
getattr(holder, 'AutonomousEngine')(None)

class Surface:
    def method(self, client, value):
        request: object = client.request
        attr = client.post
        mapping = {'post': client.post, 'delete': client.delete}
        one_mapping = {'post': client.post}
        mapping['post']('/x')
        one_mapping['post']('/x')
        self.request_alias = client.request
        self['request_alias'] = client.request
        partial_call = functools.partial(client.request, 'POST')
        partial_call('/x')
        functools.partial(client.request, 'POST')('/x')
        getattr(client, 'urlopen')('/x')
        getattr(client, value)('/x')
        client.__getattribute__(client, 'create_order')('/x')
        client.__getattribute__(client, value)('/x')
        client.__getattribute__(client, 'urlopen')('/x')
        client._api('/x', method='POST')
        client._api('/x', method=value)
        client.execute_action('RESUME')
        client.execute_action(ControlAction.RESUME)
        client.execute_action(value)
        client.post('/x')
        client.urlopen('/x')
        setattr(client, 'request', client.request)
        consume(client.request)
        consume(one_mapping['post'])
        return request

async def async_surface(client):
    return client.create_algo_order('/x')

imported_create(holder)
__import__('requests')
"""
    tree = ast.parse(source)
    entry = registry._EntrySurfaceVisitor()
    entry.visit(tree)
    assert entry.sensitive is True
    assert registry._EntrySurfaceVisitor._constructor_name(ast.parse("x.y").body[0].value) == "y"
    assert registry._EntrySurfaceVisitor._constructor_name(ast.parse("1").body[0].value) == ""

    visitor = registry._TerminalWriteCallVisitor("surface.py")
    visitor.visit(tree)
    assert visitor.calls
    assert any("partial[" in key for key in visitor.calls)
    assert any("execute_action[RESUME]" in key for key in visitor.calls)
    assert any("DYNAMIC" in key for key in visitor.calls)
    assert registry._TerminalWriteCallVisitor._is_direct_http_client(ast.parse("httpx.post('/x')").body[0].value.func)
    assert registry._TerminalWriteCallVisitor._is_direct_http_client(
        ast.parse("httpx.Client().post('/x')").body[0].value.func
    )
    assert not registry._TerminalWriteCallVisitor._is_direct_http_client(
        ast.parse("client.post('/x')").body[0].value.func
    )
    assert not registry._TerminalWriteCallVisitor._is_direct_http_client(ast.parse("client").body[0].value)
    assert not registry._TerminalWriteCallVisitor._is_direct_http_client(
        ast.parse("items[0].post('/x')").body[0].value.func
    )
    visitor._reference_target(ast.parse("client.urlopen").body[0].value)
    visitor._remember_alias(ast.Constant(value="not-a-target"), ast.Name(id="client"))
    assert registry._curl_method("curl https://example.invalid") is None


def test_validation_binding_and_main_negative_gate_paths(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(registry, "discover_governed_source_digests", lambda _root: {})
    monkeypatch.setattr(registry, "discover_source_scan_issues", lambda _root: [])
    monkeypatch.setattr(registry, "discover_declared_entrypoints", lambda _root: {"console:demo": "actual"})
    monkeypatch.setattr(registry, "discover_network_imports", lambda _root: {"network": 1})
    monkeypatch.setattr(registry, "discover_sensitive_entry_paths", lambda _root: {"entry.py"})
    monkeypatch.setattr(registry, "discover_terminal_write_calls", lambda _root: {"terminal": 1})
    monkeypatch.setattr(registry, "_negative_test_reference_exists", lambda _reference, _root: True)
    monkeypatch.setattr(registry, "_bound_negative_test", lambda kind, identity: f"bound:{kind}:{identity}")

    entry = _entry(
        entry_id="entry",
        path="entry.py",
        capability="LOCAL_OBSERVATION",
        status="READ_ONLY",
        negative_test="unbound-entry",
        expected_rejection="WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        call_graph="",
    )
    declaration = _declaration(
        command="actual",
        owner="Engineering Owner",
        capability="LOCAL_OBSERVATION",
        status="READ_ONLY",
        negative_test=registry._GENERIC_NEGATIVE_TEST,
        expected_rejection="",
    )
    network = _network(
        id="network",
        source="network",
        negative_test="unbound-network",
        expected_rejection="WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
    )
    terminal = _terminal(
        id="terminal",
        source="terminal",
        owner="bad-owner",
        capability="bad-capability",
        status="READ_ONLY",
        negative_test=registry._GENERIC_NEGATIVE_TEST,
        expected_rejection="WRITE_CAPABILITY_REGISTRY_INCOMPLETE",
        call_graph="",
    )
    bad_status_terminal = _terminal(
        id="bad-status",
        source="bad-status",
        status="BAD",
        capability="TERMINAL_WRITE_BOUNDARY",
        negative_test="bound-terminal",
        expected_rejection="EXTERNAL_WRITE_NOT_AUTHORIZED",
    )
    stale_network = _network(id="stale-network", source="stale-network", negative_test="bound-network")
    payload = {
        "governance_digest": "wrong",
        "behavioral_negative_tests": [],
        "governed_source_digests": {},
        "schema_version": "1.0",
        "declared_entrypoints": {"console:demo": "actual"},
        "declared_entrypoint_records": {"console:demo": declaration},
        "network_imports": [network, stale_network],
        "entries": [entry],
        "terminal_write_paths": [terminal, bad_status_terminal],
    }
    issues = registry.validate_registry(payload, root=tmp_path)
    assert any(issue.startswith("WRITE_REGISTRY_DECLARED_NEGATIVE_TEST_UNBOUND") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_DECLARED_NEGATIVE_TEST_GENERIC") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_DECLARED_REJECTION_EMPTY") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_NETWORK_IMPORT_NEGATIVE_TEST_UNBOUND") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_STALE_NETWORK_IMPORT") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_REJECTION_MISMATCH:terminal") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_TERMINAL_STATUS_INVALID:bad-status") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_TERMINAL_FIELD_EMPTY") for issue in issues)
    assert any(issue.startswith("WRITE_REGISTRY_CALL_GRAPH_INVALID") for issue in issues)
    assert registry.validate_registry({"entries": None}, root=tmp_path) == ["WRITE_REGISTRY_ENTRIES_INVALID"]

    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(registry, "load_registry", lambda _path: {"entries": []})
    monkeypatch.setattr(registry, "validate_registry", lambda _payload, root: [])
    monkeypatch.setattr(registry, "run_negative_test_gate", lambda _payload, root: [])
    assert registry.main(["--root", str(tmp_path), "--registry", str(registry_path), "--run-negative-tests"]) == 0
    assert '"status": "PASS"' in capsys.readouterr().out
