"""M00-E01: every executable or exchange-sensitive path is registered."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from beidou_launcher.write_registry import (
    discover_declared_entrypoints,
    discover_network_imports,
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
    load_registry,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def test_write_capability_registry_is_complete_and_valid() -> None:
    registry = load_registry(REGISTRY)

    assert validate_registry(registry, root=ROOT) == []
    assert {entry["path"] for entry in registry["entries"]} == discover_sensitive_entry_paths(ROOT)
    registered_calls = {item["source"]: item["occurrences"] for item in registry["terminal_write_paths"]}
    assert registered_calls == discover_terminal_write_calls(ROOT)


def test_registry_covers_delivery_and_make_entry_surfaces() -> None:
    registry = load_registry(REGISTRY)
    registered = {entry["path"] for entry in registry["entries"]}

    assert "Makefile" in registered
    assert {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "delivery" / "scripts").iterdir()
        if path.suffix in {".py", ".sh"}
    } <= registered
    assert registry["declared_entrypoints"] == discover_declared_entrypoints(ROOT)
    assert registry["declared_entrypoints"]["console:beidou"] == "beidou_launcher.cli:main"


def test_terminal_scan_detects_dynamic_alias_and_getattr_calls(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
async def mutate(client, method):
    await client.request(method, '/dynamic')
    send = client.create_order
    await send()
    cancel = getattr(client, 'cancel_order')
    await cancel()
""",
        encoding="utf-8",
    )

    assert discover_terminal_write_calls(tmp_path) == {
        "payload.py::mutate::alias[cancel_order]": 1,
        "payload.py::mutate::alias[create_order]": 1,
        "payload.py::mutate::request[DYNAMIC]": 1,
    }


def test_terminal_scan_fails_closed_for_indirect_write_callables(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
from venue import create_order as imported_send
from engine import AutonomousEngine as E
import functools

async def mutate(client, holder, ops, runtime_name):
    first = client.create_order
    second = first
    await second()
    await getattr(client, runtime_name)()
    await getattr(client, 'cancel_' + 'order')()
    holder.send = client.create_order
    await holder.send()
    callbacks = {'send': client.create_order}
    await callbacks['send']()
    await functools.partial(client.request, 'POST', '/write')()
    partial_send = functools.partial(client.request, 'POST', '/write-two')
    await partial_send()
    setattr(holder, 'cancel', client.cancel_order)
    register_callback(client.create_order)
    def callback_factory():
        return client.create_order
    await imported_send()
    E()
    holder.factory = E
    holder.factory()
""",
        encoding="utf-8",
    )

    calls = discover_terminal_write_calls(tmp_path)
    rendered = "\n".join(calls)
    assert "alias[create_order]" in rendered
    assert "getattr[DYNAMIC]" in rendered
    assert "getattr[cancel_order]" in rendered
    assert "attribute_alias[create_order]" in rendered
    assert "subscript_alias[create_order]" in rendered
    assert "partial[request:POST]" in rendered
    assert "import_alias[create_order]" in rendered
    assert "setattr[cancel_order]" in rendered
    assert "callable_argument[create_order]" in rendered
    assert "returned_callable[create_order]" in rendered
    assert discover_sensitive_entry_paths(tmp_path) == {"payload.py"}


def test_network_import_inventory_catches_aliases_before_call_analysis(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
import httpx as hx
import asyncio as aio
from httpx import post as send
import socket as sock
import urllib.request

async def mutate():
    client = hx.AsyncClient()
    await client.delete('https://offline.invalid/write')
    await send('https://offline.invalid/write')
    request = urllib.request.Request('https://offline.invalid/write', method='POST')
    urllib.request.urlopen(request)
    await aio.open_connection('offline.invalid', 443)
    sock.socket()
""",
        encoding="utf-8",
    )

    assert discover_network_imports(tmp_path) == {
        "payload.py::httpx": 2,
        "payload.py::asyncio.open_connection": 1,
        "payload.py::socket": 1,
        "payload.py::socket.socket": 1,
        "payload.py::urllib.request": 1,
    }


def test_non_python_write_surfaces_are_scanned_and_parse_failures_block(tmp_path: Path) -> None:
    (tmp_path / "payload.sh").write_text(
        '#!/bin/sh\ncurl -X POST https://offline.invalid/order\ncurl -X "$METHOD" https://offline.invalid/dynamic\n'
        'curl \\\n  --data "risk=1" https://offline.invalid/implicit\n',
        encoding="utf-8",
    )
    (tmp_path / "broken.sh").write_text("#!/bin/sh\nif then\n", encoding="utf-8")
    (tmp_path / "payload.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>ProgramArguments</key><array>
<string>/usr/bin/env</string><string>curl</string><string>-X</string><string>DELETE</string><string>https://offline.invalid/order</string>
</array></dict></plist>
""",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "write:\n\t$(CURL) --request PATCH https://offline.invalid/order\n"
        "embedded:\n\tpython -c \"import httpx; httpx.post('https://offline.invalid')\"\n",
        encoding="utf-8",
    )

    calls = discover_terminal_write_calls(tmp_path)

    assert calls["payload.sh::<shell>::curl[DYNAMIC]"] == 1
    assert calls["payload.sh::<shell>::curl[POST]"] == 2
    assert calls["payload.plist::<plist>::curl[DELETE]"] == 1
    assert calls["Makefile::<make:write>::curl[PATCH]"] == 1
    assert calls["Makefile::<make:embedded>::embedded_network[DYNAMIC]"] == 1
    assert any(
        issue.startswith("WRITE_REGISTRY_SOURCE_SCAN_FAILED:broken.sh:")
        for issue in validate_registry(
            {
                "schema_version": "1.0",
                "declared_entrypoints": {},
                "declared_entrypoint_records": {},
                "entries": [],
                "terminal_write_paths": [],
            },
            root=tmp_path,
        )
    )


def test_registry_rejects_semantically_ungoverned_terminal_record() -> None:
    registry = load_registry(REGISTRY)
    record = registry["terminal_write_paths"][0]
    original = dict(record)
    try:
        record.update(
            owner="TBD",
            capability="SOURCE_READ_ONLY",
            call_graph="generic",
            negative_test=(
                "tests/architecture/test_write_capability_registry.py::"
                "test_write_capability_registry_is_complete_and_valid"
            ),
        )
        issues = validate_registry(registry, root=ROOT)
    finally:
        record.clear()
        record.update(original)

    assert "WRITE_REGISTRY_OWNER_INVALID:" + original["id"] in issues
    assert "WRITE_REGISTRY_TERMINAL_CAPABILITY_INVALID:" + original["id"] in issues
    assert "WRITE_REGISTRY_CALL_GRAPH_INVALID:" + original["id"] in issues
    assert "WRITE_REGISTRY_NEGATIVE_TEST_GENERIC:" + original["id"] in issues


def test_registry_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text('{"schema_version":"1.0","schema_version":"2.0","entries":[]}', encoding="utf-8")

    try:
        load_registry(registry_path)
    except ValueError as exc:
        assert str(exc) == "WRITE_REGISTRY_DUPLICATE_KEY:schema_version"
    else:
        raise AssertionError("duplicate JSON key was accepted")


def test_every_declared_console_and_make_entry_has_exact_governance() -> None:
    registry = load_registry(REGISTRY)

    assert set(registry["declared_entrypoint_records"]) == set(discover_declared_entrypoints(ROOT))
    for declaration, record in registry["declared_entrypoint_records"].items():
        assert record["command"] == registry["declared_entrypoints"][declaration]
        assert record["owner"] not in {"", "TBD", "UNKNOWN"}
        assert record["capability"]
        assert record["status"] in {"HARD_HOLD", "READ_ONLY", "OFFLINE_ONLY", "DELEGATE_ONLY"}
        assert record["negative_test"].startswith("tests/")
        assert record["expected_rejection"]


def test_network_import_inventory_is_exact_and_policy_bound() -> None:
    registry = load_registry(REGISTRY)
    registered = {item["source"]: item["occurrences"] for item in registry["network_imports"]}

    assert registered == discover_network_imports(ROOT)
    assert {item["purpose"] for item in registry["network_imports"]} == {
        "ALERT_DELIVERY",
        "EXCHANGE_WEBSOCKET",
        "EXCHANGE_TRANSPORT",
        "FAULT_INJECTION_READ",
        "LOCAL_HEALTH_READ",
        "LOCAL_NETWORK_BIND_CHECK",
    }


def test_registry_read_only_dry_run_is_executable() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository-local module
        [
            sys.executable,
            "-m",
            "beidou_launcher.write_registry",
            "--root",
            str(ROOT),
            "--registry",
            str(REGISTRY),
            "--run-negative-tests",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "PASS", "issues": []}


def test_only_launcher_cli_can_be_a_future_writable_runtime_candidate() -> None:
    registry = load_registry(REGISTRY)
    candidates = [entry for entry in registry["entries"] if entry["capability"] == "WRITABLE_RUNTIME_CANDIDATE"]

    assert [(entry["id"], entry["path"]) for entry in candidates] == [("ENTRY-LAUNCHER-CLI", "beidou_launcher/cli.py")]
    assert candidates[0]["status"] == "HARD_HOLD"


def test_compatibility_apps_do_not_construct_engine_or_default_testnet() -> None:
    for relative in (
        "apps/autopilot/__main__.py",
        "apps/strategy_engine/__main__.py",
        "apps/safety_executor/__main__.py",
        "apps/research_lab/__main__.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "AutonomousEngine(" not in source
        assert 'BEIDOU_ENV"] = "testnet"' not in source


def test_noncanonical_tools_have_no_implicit_testnet_symbols_or_shell_secret_loading() -> None:
    factor_miner = (ROOT / "apps/factor_miner/__main__.py").read_text(encoding="utf-8")
    mining_cron = (ROOT / "scripts/mining_cron.sh").read_text(encoding="utf-8")
    g5_runner = (ROOT / "scripts/testnet/run_g5.py").read_text(encoding="utf-8")

    assert 'setdefault("BEIDOU_ENV", "testnet")' not in factor_miner
    assert "~/.zshrc" not in mining_cron
    assert "eval " not in mining_cron
    assert "BTCUSDT,ETHUSDT" not in mining_cron
    assert "BEIDOU_MINING_SYMBOLS" in mining_cron
    assert "api_key[-4:]" not in g5_runner
    assert '"BTCUSDT"' not in g5_runner
    assert 'parser.add_argument("--symbol", required=True' in g5_runner
    assert "BEIDOU_DEV_FAST_START" not in (ROOT / "beidou_launcher/preflight.py").read_text(encoding="utf-8")


def test_legacy_start_wrapper_has_no_startup_side_effects() -> None:
    source = (ROOT / "start_beidou.sh").read_text(encoding="utf-8")

    for forbidden in (
        ".env",
        "docker",
        "mkdir",
        "source ",
        "export ",
        "risk_parameters",
        "CERT-G5",
        "BTCUSDT",
        ":-testnet",
    ):
        assert forbidden not in source
    assert source.count("exec ") == 1
