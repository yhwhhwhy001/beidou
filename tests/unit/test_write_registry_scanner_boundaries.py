"""Boundary tests for the read-only executable-surface registry scanner."""

from __future__ import annotations

import json
import plistlib
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
