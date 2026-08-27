"""M00-E01: every executable or exchange-sensitive path is registered."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from beidou_launcher.write_registry import (
    discover_declared_entrypoints,
    discover_governed_source_digests,
    discover_network_imports,
    discover_sensitive_entry_paths,
    discover_terminal_write_calls,
    load_registry,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config" / "write-capability-registry.json"


def _write_evidence_artifact(name: str, payload: object) -> None:
    evidence_dir = os.environ.get("BEIDOU_EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        return
    (Path(evidence_dir) / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_local_wheel(destination: Path) -> Path:
    """Build a dependency-free local wheel fixture without network or build backends."""
    wheel = destination / "beidou-2.0.0-py3-none-any.whl"
    package_files = [
        path
        for package in ("beidou_cli", "apps/alpha_app", "beidou_shared", "beidou_strategy")
        for path in sorted((ROOT / package).rglob("*.py"))
    ]
    dist_info = "beidou-2.0.0.dist-info"
    metadata = "Metadata-Version: 2.1\nName: beidou\nVersion: 2.0.0\n"
    wheel_metadata = "Wheel-Version: 1.0\nGenerator: beidou-offline-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entry_points = "[console_scripts]\nbeidou = beidou_cli:main\n"
    archive_paths = [path.relative_to(ROOT).as_posix() for path in package_files]
    archive_paths.extend(
        [
            f"{dist_info}/METADATA",
            f"{dist_info}/WHEEL",
            f"{dist_info}/entry_points.txt",
            f"{dist_info}/RECORD",
        ]
    )
    record = "\n".join(f"{path},," for path in archive_paths) + "\n"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        for path in package_files:
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", wheel_metadata)
        archive.writestr(f"{dist_info}/entry_points.txt", entry_points)
        archive.writestr(f"{dist_info}/RECORD", record)
    return wheel


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_hash_bound_wheel_bootstrap_isolated(tmp_path: Path) -> None:
    """Install a local wheel in a clean venv and prove imports do not use checkout paths."""
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    wheel = _build_local_wheel(wheel_dir)
    venv_dir = tmp_path / "venv"
    environment = {**os.environ, "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("PYTHONPATH", None)
    created = subprocess.run(  # noqa: S603 - fixed local interpreter and test-built wheel
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert created.returncode == 0, created.stderr
    venv_python = venv_dir / "bin" / "python"
    installed = subprocess.run(  # noqa: S603 - fixed local venv interpreter and local wheel
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stderr
    probe = subprocess.run(  # noqa: S603 - fixed local venv interpreter and inline probe
        [
            str(venv_python),
            "-c",
            "import apps.alpha_app, beidou_cli; print(beidou_cli.__file__); print(apps.alpha_app.__file__)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert str(ROOT) not in probe.stdout
    closes = ",".join(str(100 + index * 0.25) for index in range(60))
    evaluated = subprocess.run(  # noqa: S603 - fixed local venv interpreter and local module
        [str(venv_python), "-m", "beidou_cli", "alpha", "evaluate", "--closes", closes],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert evaluated.returncode == 0, evaluated.stderr
    payload = json.loads(evaluated.stdout)
    assert payload["row_count"] == 60
    assert payload["dataset_source"] == "LOCAL"
    _write_evidence_artifact(
        "isolated-install-provenance.json",
        {
            "wheel_name": wheel.name,
            "wheel_sha256": _sha256(wheel),
            "installed_module_paths": probe.stdout.splitlines(),
            "source_checkout": False,
            "network_policy": "DENIED",
            "status": "PASS",
        },
    )


def test_write_capability_registry_is_complete_and_valid() -> None:
    registry = load_registry(REGISTRY)

    assert validate_registry(registry, root=ROOT) == []
    assert {entry["path"] for entry in registry["entries"]} == discover_sensitive_entry_paths(ROOT)
    registered_calls = {item["source"]: item["occurrences"] for item in registry["terminal_write_paths"]}
    assert registered_calls == discover_terminal_write_calls(ROOT)
    _write_evidence_artifact(
        "write-registry-report.json",
        {
            "status": "PASS",
            "governance_digest": registry["governance_digest"],
            "declared_entrypoints": registry["declared_entrypoints"],
            "entry_count": len(registry["entries"]),
            "terminal_write_path_count": len(registry["terminal_write_paths"]),
        },
    )


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
    assert registry["declared_entrypoints"]["console:beidou"] == "beidou_cli:main"


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
        "payload.py::mutate::getattr[cancel_order]": 1,
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
    hidden = (lambda: getattr(client, 'create_' + 'order'))()
    await hidden()
    constructor_box = {'engine': E}
    next(iter(constructor_box.values()))()
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
    assert "getattr[create_order]" in rendered
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


def test_governed_source_digest_detects_hidden_behavior_and_new_sources(tmp_path: Path) -> None:
    payload = tmp_path / "payload.py"
    payload.write_text("import httpx\n", encoding="utf-8")
    baseline = discover_governed_source_digests(tmp_path)

    payload.write_text("import httpx\ngetattr(httpx, 'po' + 'st')('https://offline.invalid')\n", encoding="utf-8")
    changed = discover_governed_source_digests(tmp_path)
    assert changed["payload.py"] != baseline["payload.py"]

    (tmp_path / "activate.cron").write_text("* * * * * dynamic-command\n", encoding="utf-8")
    assert "activate.cron" in discover_governed_source_digests(tmp_path)

    (tmp_path / "migration.sql").write_text("SELECT 1;\n", encoding="utf-8")
    assert "migration.sql" in discover_governed_source_digests(tmp_path)

    runtime_evidence = tmp_path / "evidence" / "generated.json"
    runtime_evidence.parent.mkdir()
    runtime_evidence.write_text('{"result":"runtime"}\n', encoding="utf-8")
    runtime_state = tmp_path / ".beidou" / "state.json"
    runtime_state.parent.mkdir()
    runtime_state.write_text('{"state":"runtime"}\n', encoding="utf-8")
    hidden_script = tmp_path / "evidence" / "activate.sh"
    hidden_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    governed = discover_governed_source_digests(tmp_path)
    assert "evidence/generated.json" not in governed
    assert ".beidou/state.json" not in governed
    assert "evidence/activate.sh" in governed

    (tmp_path / ".superpowers" / "run").mkdir(parents=True)
    (tmp_path / ".superpowers" / "run" / "notes.json").write_text("{}", encoding="utf-8")
    (tmp_path / "artifacts" / "evidence").mkdir(parents=True)
    (tmp_path / "artifacts" / "evidence" / "runtime.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config" / "policies").mkdir(parents=True)
    (tmp_path / "config" / "policies" / "risk.json").write_text("{}", encoding="utf-8")
    (tmp_path / "config" / "env.testnet.yaml").write_text("secret: local\n", encoding="utf-8")
    (tmp_path / "config" / "env.template.yaml").write_text("secret: ''\n", encoding="utf-8")
    governed = discover_governed_source_digests(tmp_path)
    assert ".superpowers/run/notes.json" not in governed
    assert "artifacts/evidence/runtime.json" not in governed
    assert "config/policies/risk.json" not in governed
    assert "config/env.testnet.yaml" not in governed
    assert "config/env.template.yaml" in governed


def test_generated_delivery_packages_are_outside_runtime_governance(tmp_path: Path) -> None:
    package = tmp_path / "delivery" / "packages" / "BD-AF-P0P3-V1"
    package.mkdir(parents=True)
    (package / "task.py").write_text(
        "from beidou_core import AutonomousEngine\nAutonomousEngine()\n",
        encoding="utf-8",
    )
    (package / "task.yaml").write_text("run: python task.py\n", encoding="utf-8")

    assert discover_sensitive_entry_paths(tmp_path) == set()
    assert discover_network_imports(tmp_path) == {}
    assert discover_terminal_write_calls(tmp_path) == {}
    assert discover_governed_source_digests(tmp_path) == {}


def test_primary_governance_implementation_is_itself_hashed() -> None:
    governed = discover_governed_source_digests(ROOT)

    assert "beidou_launcher/write_registry.py" in governed
    assert all(
        path in governed
        for path in ("migrations/001_initial_schema.up.sql", "migrations/006_execution_children.up.sql")
    )


def test_terminal_scan_detects_network_aliases_and_dynamic_loading(tmp_path: Path) -> None:
    (tmp_path / "payload.py").write_text(
        """
import asyncio
import httpx
import socket
import urllib.request

send = httpx.post
relay = send
dynamic_httpx = __import__('htt' + 'px')

async def mutate(client):
    relay('https://offline.invalid/write', data=b'x')
    getattr(dynamic_httpx, 'po' + 'st')('https://offline.invalid/write')
    request_factory = urllib.request.Request
    request_alias = request_factory
    request = request_alias('https://offline.invalid/write', data=b'x')
    opener = urllib.request.urlopen
    opener_alias = opener
    opener_alias(request)
    connect = asyncio.open_connection
    await connect('offline.invalid', 443)
    create_socket = socket.create_connection
    sock = create_socket(('offline.invalid', 443))
    sender = sock.sendall
    sender(b'x')
    table = vars(httpx)
    def factory():
        return table['post']
    factory()('https://offline.invalid/write', data=b'x')
    callback(vars(httpx).get('post'))
    object.__getattribute__(httpx, 'post')('https://offline.invalid/write', data=b'x')
""",
        encoding="utf-8",
    )

    calls = "\n".join(discover_terminal_write_calls(tmp_path))
    assert "http[POST]" in calls
    assert "dynamic_import[httpx]" in calls
    assert "urllib_request" in calls
    assert "urllib_urlopen" in calls
    assert "asyncio_open_connection" in calls
    assert "socket_create_connection" in calls
    assert "socket_sendall" in calls
    assert "vars[network_module[httpx]]" in calls
    assert "reflective[post]" in calls


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
    (tmp_path / "dynamic.sh").write_text(
        '#!/bin/sh\nc=curl\n"$c" --data x https://offline.invalid/write\n',
        encoding="utf-8",
    )
    (tmp_path / "dynamic.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>ProgramArguments</key><array>
<string>/bin/sh</string><string>-c</string><string>curl --data x https://offline.invalid/write</string>
</array></dict></plist>
""",
        encoding="utf-8",
    )
    (tmp_path / "workflow.yml").write_text(
        "jobs:\n  write:\n    steps:\n      - run: curl --data x https://offline.invalid/write\n",
        encoding="utf-8",
    )
    (tmp_path / "schedule.cron").write_text(
        "* * * * * curl --data x https://offline.invalid/write\n",
        encoding="utf-8",
    )

    calls = discover_terminal_write_calls(tmp_path)

    assert calls["payload.sh::<shell>::curl[DYNAMIC]"] == 1
    assert calls["payload.sh::<shell>::curl[POST]"] == 2
    assert calls["payload.plist::<plist>::curl[DELETE]"] == 1
    assert calls["Makefile::<make:write>::curl[PATCH]"] == 1
    assert calls["Makefile::<make:embedded>::embedded_network[DYNAMIC]"] == 1
    assert calls["dynamic.sh::<shell>::curl[POST]"] == 1
    assert calls["dynamic.plist::<plist>::curl[POST]"] == 1
    assert calls["workflow.yml::<yaml>::curl[POST]"] == 1
    assert calls["schedule.cron::<cron>::curl[POST]"] == 1
    assert {"workflow.yml", "schedule.cron"} <= discover_sensitive_entry_paths(tmp_path)
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


def test_yaml_duplicate_keys_fail_source_scan(tmp_path: Path) -> None:
    (tmp_path / "workflow.yml").write_text(
        "jobs:\n  first: {}\njobs:\n  hidden:\n    steps:\n      - run: curl --data x https://offline.invalid\n",
        encoding="utf-8",
    )

    issues = validate_registry(
        {
            "schema_version": "1.0",
            "declared_entrypoints": {},
            "declared_entrypoint_records": {},
            "entries": [],
            "network_imports": [],
            "terminal_write_paths": [],
        },
        root=tmp_path,
    )

    assert any("WRITE_REGISTRY_SOURCE_SCAN_FAILED:workflow.yml:DuplicateKeyError" in issue for issue in issues)


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
    assert "WRITE_REGISTRY_GOVERNANCE_DIGEST_MISMATCH" in issues


def test_governance_digest_rejects_legal_but_false_record_semantics() -> None:
    registry = load_registry(REGISTRY)
    registry["entries"][0]["owner"] = "Security Owner"
    registry["terminal_write_paths"][0]["capability"] = "DYNAMIC_WRITE_BOUNDARY_REQUIRED"
    registry["network_imports"][0]["purpose"] = "LOCAL_HEALTH_READ"
    declaration = next(iter(registry["declared_entrypoint_records"].values()))
    declaration["expected_rejection"] = "THIS_REASON_IS_NEVER_EMITTED"

    assert "WRITE_REGISTRY_GOVERNANCE_DIGEST_MISMATCH" in validate_registry(registry, root=ROOT)


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
