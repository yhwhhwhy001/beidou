"""一键启动前置检查与安全门禁。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess  # nosec B404 - fixed local inspection commands
import sys
import time
from pathlib import Path
from typing import Any

from .models import CheckResult, CheckSeverity, CheckStatus
from .registry import check_package_imports

WRITE_MODE = "testnet"


def _g5_certificate_probe(project_root: Path, commit: str) -> tuple[bool, str, dict[str, Any]]:
    """Read-only verify the predecessor G5 certificate for writable Testnet.

    The engine/ladder may expose G5 status after construction, but allowing a
    writable worker to reach that point without a bound PASS certificate makes
    the launch gate dependent on implementation order.  Verify the artifact
    before constructing the engine; this probe never performs exchange I/O.
    """

    certificate_path = project_root / "artifacts" / "evidence" / "testnet" / "g5-certificate.json"
    plan_path = project_root / "config" / "g5-testnet-plan.yaml"
    evidence: dict[str, Any] = {
        "certificate_path": str(certificate_path),
        "plan_path": str(plan_path),
        "commit": commit,
    }
    if not certificate_path.is_file():
        return False, "G5 certificate is missing", evidence
    if not plan_path.is_file():
        return False, "G5 plan is missing", evidence
    try:
        import yaml

        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        expected_scenarios = plan.get("scenarios") if isinstance(plan, dict) else None
        max_notional = plan.get("max_test_notional_usdt") if isinstance(plan, dict) else None
        if (
            not isinstance(certificate, dict)
            or not isinstance(expected_scenarios, list)
            or not expected_scenarios
            or max_notional in (None, "")
        ):
            return False, "G5 certificate or scenario plan is malformed", evidence
        from beidou_certification.gate_verifier import verify_g5_certificate

        # Ruling-22:plan 显式声明 allow_withdraw_permission 时豁免 demo-fapi
        # canWithdraw 恒 True(缺省 False,行为与现状一致;豁免仍需 host 非
        # mainnet,由 verify 内双重判定把关)
        verification = verify_g5_certificate(
            certificate,
            expected_commit=commit,
            expected_scenarios=[str(item) for item in expected_scenarios],
            max_notional_usdt=float(max_notional),
            allow_withdraw_permission=plan.get("allow_withdraw_permission") is True,
        )
        evidence["verification"] = verification.to_dict()
        if not verification.passed:
            return False, "G5 certificate is not independently verifiable", evidence
        return True, "G5 Testnet certificate verified", evidence
    except Exception as exc:
        return False, "G5 certificate verification failed", {**evidence, "error_type": type(exc).__name__}


def _postgres_authority_probe(project_root: Path, database_url: str) -> tuple[bool, str, dict[str, Any]]:
    """Read-only verify the PostgreSQL authority and immutable migration head.

    The engine already refuses an unavailable or incomplete PostgreSQL store,
    but startup evidence must expose that fact before a worker is constructed.
    This probe never creates tables, runs migrations, or returns a DSN/error
    string that could contain credentials.
    """

    evidence: dict[str, Any] = {
        "backend": "postgresql",
        "configured": bool(database_url),
        "required_tables": [],
        "required_migrations": [],
        "missing_tables": [],
        "missing_migrations": [],
        "checksum_mismatch": [],
    }
    if not database_url.lower().startswith(("postgresql://", "postgres://")):
        return False, "PostgreSQL DSN is not configured", {**evidence, "backend": "unsupported"}

    try:
        from beidou_infra.postgres_store import PostgresPersistentStore

        required_tables = tuple(PostgresPersistentStore._required_tables)
        required_versions = tuple(PostgresPersistentStore._required_migration_versions)
    except Exception as exc:
        return (
            False,
            "PostgreSQL runtime contract unavailable",
            {**evidence, "error_type": type(exc).__name__},
        )

    evidence["required_tables"] = list(required_tables)
    evidence["required_migrations"] = list(required_versions)
    migration_dir = project_root / "migrations"
    expected_checksums: dict[str, str] = {}
    missing_files: list[str] = []
    for version in required_versions:
        migration_file = migration_dir / f"{version}.sql"
        if not migration_file.is_file():
            missing_files.append(migration_file.name)
            continue
        expected_checksums[version] = hashlib.sha256(migration_file.read_bytes()).hexdigest()
    if missing_files:
        return (
            False,
            "Required PostgreSQL migration files are missing",
            {**evidence, "missing_migration_files": missing_files},
        )

    try:
        import psycopg

        # A bounded connect is essential: a dead database cannot hold the
        # launcher indefinitely while the operator believes it is guarded.
        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as conn:
            missing_tables: list[str] = []
            for table in required_tables:
                row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
                if row is None or row[0] is None:
                    missing_tables.append(table)
            rows = conn.execute(
                "SELECT version, checksum FROM schema_migrations WHERE version = ANY(%s)",
                (list(required_versions),),
            ).fetchall()
    except Exception as exc:
        return (
            False,
            "PostgreSQL authority connection failed",
            {**evidence, "error_type": type(exc).__name__},
        )

    applied = {str(row[0]): str(row[1]) for row in (rows or []) if row and row[0] is not None}
    missing_migrations = [version for version in required_versions if version not in applied]
    checksum_mismatch = [
        version
        for version in required_versions
        if version in applied and applied[version] != expected_checksums.get(version)
    ]
    evidence.update(
        {
            "missing_tables": missing_tables,
            "missing_migrations": missing_migrations,
            "checksum_mismatch": checksum_mismatch,
        }
    )
    if missing_tables:
        return False, "PostgreSQL required tables are missing", evidence
    if missing_migrations:
        return False, "PostgreSQL migration head is incomplete", evidence
    if checksum_mismatch:
        return False, "PostgreSQL migration checksum drift detected", evidence
    return True, "PostgreSQL authority and migration head verified", evidence


def current_commit(project_root: Path | None = None) -> str:
    try:
        result = subprocess.run(  # nosec B603, B607 - fixed git/port inspection command, shell disabled
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_root,
        )
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _git_worktree_state(project_root: Path) -> tuple[bool, list[str], str]:
    try:
        result = subprocess.run(  # nosec B603, B607 - fixed git/status inspection command, shell disabled
            ["git", "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, [], result.stderr.strip() or "git status failed"
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    return True, dirty, ""


def _result(
    check_id: str,
    name: str,
    ok: bool,
    severity: CheckSeverity,
    pass_message: str,
    fail_message: str,
    *,
    evidence: dict[str, Any] | None = None,
    warn: bool = False,
    started: float | None = None,
) -> CheckResult:
    if ok:
        status = CheckStatus.WARN if warn else CheckStatus.PASS
        message = pass_message
    elif warn:
        status = CheckStatus.WARN
        message = fail_message
    else:
        status = CheckStatus.FAIL
        message = fail_message
    return CheckResult(
        check_id=check_id,
        name=name,
        status=status,
        severity=severity,
        message=message,
        evidence=evidence or {},
        duration_ms=round((time.perf_counter() - started) * 1000, 3) if started is not None else 0.0,
    )


def _port_available(port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))  # nosec B104 - availability probe, not a listening service
        return True, "available"
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def _launchd_plist_drift(project_root: Path, installed_path: Path | None = None) -> tuple[list[str], dict[str, Any]]:
    """比对 deploy 模板与实装 launchd plist（M00-F07，P0-02 治理）。

    返回 (drift_items, evidence)。漂移项仅为 WARN（P2）：模板变更/实装
    变更需要操作者显式决定，preflight 不替操作者改系统配置。
    """
    import plistlib

    template_path = project_root / "deploy" / "com.beidou.autopilot.plist"
    evidence: dict[str, Any] = {"installed": False}
    if not template_path.is_file():
        return ["模板缺失: deploy/com.beidou.autopilot.plist"], evidence
    with open(template_path, "rb") as f:
        template = plistlib.load(f)
    resolved = installed_path or (Path.home() / "Library" / "LaunchAgents" / "com.beidou.autopilot.plist")
    if not resolved.is_file():
        evidence["note"] = "实装 plist 不存在（未部署）；无漂移可比"
        return [], evidence
    evidence["installed"] = True
    with open(resolved, "rb") as f:
        installed = plistlib.load(f)
    drift: list[str] = []
    t_ka, i_ka = template.get("KeepAlive"), installed.get("KeepAlive")
    if i_ka is True and t_ka is not True:
        drift.append("实装 KeepAlive=true：监督器终态退出码 5/6（LOCKED/FAILED）会被无限重启（崩溃-重启循环）")
    t_thr, i_thr = template.get("ThrottleInterval"), installed.get("ThrottleInterval")
    if i_thr is not None and t_thr is not None and int(i_thr) != int(t_thr):
        drift.append(f"ThrottleInterval 实装 {i_thr}s vs 模板 {t_thr}s")
    args = " ".join(str(a) for a in (installed.get("ProgramArguments") or []))
    if "eval" in args and "zshrc" in args:
        drift.append("实装使用 shell eval 注入凭据（模板禁止：凭据应经 wrapper 从 .env 注入）")
    if "HTTPS_PROXY" not in args and template.get("EnvironmentVariables", {}).get("HTTPS_PROXY"):
        drift.append("模板配置代理但实装未携带代理环境（网络可达性可能受地域限制）")
    evidence["installed_keep_alive"] = str(i_ka)
    evidence["installed_throttle"] = str(i_thr)
    evidence["installed_has_eval"] = bool("eval" in args and "zshrc" in args)
    return drift, evidence


def _run_preflight(
    project_root: Path,
    mode: str,
    port: int,
    *,
    require_g5_certificate: bool,
) -> tuple[list[CheckResult], Any | None]:
    checks: list[CheckResult] = []
    version_ok = (3, 12) <= sys.version_info[:2] < (4, 0)
    checks.append(
        _result(
            "preflight.python",
            "Python 版本",
            version_ok,
            CheckSeverity.P1,
            f"Python {platform.python_version()} 满足 >=3.12,<4.0",
            f"Python {platform.python_version()} 不满足 >=3.12,<4.0",
            evidence={"executable": sys.executable, "version": platform.python_version()},
        )
    )

    commit = current_commit(project_root)
    commit_ok = len(commit) == 40 and all(char in "0123456789abcdef" for char in commit.lower())
    checks.append(
        _result(
            "preflight.git_commit",
            "Git 提交身份",
            commit_ok,
            CheckSeverity.P1,
            f"当前提交: {commit}",
            "无法解析当前 Git commit，证据不可绑定",
            evidence={"commit": commit},
        )
    )

    git_ok, dirty_files, git_error = _git_worktree_state(project_root)
    if not git_ok:
        checks.append(
            CheckResult(
                check_id="preflight.git_worktree",
                name="Git 工作区状态",
                status=CheckStatus.FAIL,
                severity=CheckSeverity.P1,
                message=f"无法读取工作区状态: {git_error}",
                evidence={"error": git_error},
            )
        )
    elif dirty_files:
        # A writable Testnet run must be bound to reproducible source. Paper
        # and research remain usable with a warning.
        strict = mode == WRITE_MODE
        checks.append(
            CheckResult(
                check_id="preflight.git_worktree",
                name="Git 工作区状态",
                status=CheckStatus.FAIL if strict else CheckStatus.WARN,
                severity=CheckSeverity.P0 if strict else CheckSeverity.P2,
                message=(
                    f"Testnet 工作区有 {len(dirty_files)} 项未提交变更，拒绝启动"
                    if strict
                    else f"非写模式允许脏工作区，但证据降级: {len(dirty_files)} 项变更"
                ),
                evidence={"dirty": dirty_files[:100], "count": len(dirty_files)},
            )
        )
    else:
        checks.append(
            CheckResult(
                check_id="preflight.git_worktree",
                name="Git 工作区状态",
                status=CheckStatus.PASS,
                severity=CheckSeverity.P1,
                message="工作区干净",
                evidence={"dirty": []},
            )
        )

    required_paths = ["pyproject.toml", "config", "beidou_core", "apps/autopilot"]
    missing_paths = [item for item in required_paths if not (project_root / item).exists()]
    checks.append(
        _result(
            "preflight.project_layout",
            "项目结构",
            not missing_paths,
            CheckSeverity.P1,
            "项目根目录与关键路径存在",
            f"缺少关键路径: {missing_paths}",
            evidence={"project_root": str(project_root), "missing": missing_paths},
        )
    )

    available, port_error = _port_available(port)
    checks.append(
        _result(
            "preflight.health_port",
            "健康检查端口",
            available,
            CheckSeverity.P1,
            f"端口 {port} 可用",
            f"端口 {port} 不可用: {port_error}",
            evidence={"port": port},
        )
    )

    evidence_dir = project_root / "evidence" / "bootstrap"
    runtime_dir = project_root / ".beidou"
    storage_targets = (runtime_dir, evidence_dir)
    storage_evidence = {
        "probe": "read_only_access_check",
        "targets": [
            {
                "path": str(target),
                "exists": target.exists(),
                "is_directory": target.is_dir(),
                "write_access": target.is_dir() and os.access(target, os.W_OK | os.X_OK),
            }
            for target in storage_targets
        ],
    }
    missing_storage = [str(target) for target in storage_targets if not target.exists()]
    inaccessible_storage = [
        str(target)
        for target in storage_targets
        if target.exists() and (not target.is_dir() or not os.access(target, os.W_OK | os.X_OK))
    ]
    if missing_storage:
        checks.append(
            CheckResult(
                check_id="preflight.runtime_storage",
                name="运行证据目录",
                status=CheckStatus.UNKNOWN,
                severity=CheckSeverity.P1,
                message="运行目录缺失；预检不会创建目录或写探针",
                evidence={**storage_evidence, "missing": missing_storage},
            )
        )
    else:
        checks.append(
            _result(
                "preflight.runtime_storage",
                "运行证据目录",
                not inaccessible_storage,
                CheckSeverity.P1,
                "现有运行状态和证据目录具备进程写权限",
                "现有运行目录不可写或不是目录",
                evidence={**storage_evidence, "inaccessible": inaccessible_storage},
            )
        )

    api_key_env = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
    api_secret_env = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")
    signing_key = os.environ.get("BEIDOU_SIGNING_KEY", "")
    checks.extend(check_package_imports())

    settings = None
    try:
        from beidou_shared.config import ConfigProvider

        settings = ConfigProvider().load(environment=mode)
        rest_url = settings.exchange.rest_base_url
        checks.append(
            _result(
                "preflight.config",
                "统一配置加载",
                True,
                CheckSeverity.P0,
                f"配置加载成功: {settings.source}",
                "",
                evidence={"source": settings.source, "config_hash": settings.config_hash, "rest_url": rest_url},
            )
        )
        if mode == WRITE_MODE and require_g5_certificate:
            g5_ok, g5_message, g5_evidence = _g5_certificate_probe(project_root, commit)
            checks.append(
                _result(
                    "preflight.g5_certificate",
                    "G5 Testnet 证书",
                    g5_ok,
                    CheckSeverity.P0,
                    g5_message,
                    f"{g5_message}；Testnet 保持阻断",
                    evidence=g5_evidence,
                )
            )
        effective_api_key = api_key_env or settings.exchange.api_key_ref
        effective_api_secret = api_secret_env or settings.exchange.api_secret_ref
        account_credentials_ok = len(effective_api_key) >= 10 and len(effective_api_secret) >= 10
        # 非写模式（research/paper/shadow/safety_only）不需要真实 API 凭据；
        # 引擎会优雅降级，仅 testnet 模式必须提供有效凭据。
        if mode == WRITE_MODE:
            cred_severity = CheckSeverity.P0
            cred_fail_msg = (
                "Testnet 模式缺少有效 API Key/Secret。"
                "请配置环境变量 BEIDOU_BINANCE_API_KEY / BEIDOU_BINANCE_API_SECRET 或对应 env YAML。"
            )
        else:
            cred_severity = CheckSeverity.P2
            cred_fail_msg = "未提供 API Key/Secret，引擎将无法读取真实账户数据。非写模式下此为警告，引擎会降级运行。"
        checks.append(
            _result(
                "preflight.engine_account_credentials",
                "引擎账户读取凭据",
                account_credentials_ok,
                cred_severity,
                "账户读取所需 API Key/Secret 已提供",
                cred_fail_msg,
                evidence={
                    "mode": mode,
                    "api_key_present": bool(effective_api_key),
                    "api_secret_present": bool(effective_api_secret),
                    "source": settings.source,
                },
                warn=(not account_credentials_ok and mode != WRITE_MODE),
            )
        )
        # BD-V3/T01: Testnet 风险增加必须有真实批准签名密钥。
        # 缺少密钥不得由引擎回退到 mock/default key；预检直接阻断启动。
        if mode == WRITE_MODE:
            checks.append(
                _result(
                    "preflight.signing_key",
                    "风险批准签名密钥",
                    len(signing_key) >= 16,
                    CheckSeverity.P0,
                    "BEIDOU_SIGNING_KEY 已提供",
                    "Testnet 模式缺少 BEIDOU_SIGNING_KEY，风险增加路径被阻断",
                    evidence={"present": bool(signing_key), "length": len(signing_key)},
                    warn=False,
                )
            )
            policy_dir = project_root / "config" / "policies"
            valid_policy_id: str | None = None
            policy_error = ""
            try:
                from beidou_policy.loader import PolicyLoader

                policy_loader = PolicyLoader(policy_dir=str(policy_dir), signing_key=signing_key)
                for candidate in ("risk_parameters", "autopilot_risk"):
                    envelope = policy_loader.load(candidate)
                    if envelope is None:
                        continue
                    complete, completeness_message = envelope.validate_risk_parameters()
                    if complete:
                        valid_policy_id = candidate
                        break
                    policy_error = f"{candidate}: {completeness_message}"
                if valid_policy_id is None:
                    policy_error = policy_error or "no valid signed policy envelope"
            except Exception as exc:
                policy_error = f"{type(exc).__name__}: {exc}"
            checks.append(
                _result(
                    "preflight.signed_policy",
                    "签名风险策略",
                    valid_policy_id is not None,
                    CheckSeverity.P0,
                    f"有效签名策略已加载: {valid_policy_id}",
                    f"Testnet 缺少有效签名风险策略: {policy_error}",
                    evidence={
                        "policy_dir": str(policy_dir),
                        "policy_id": valid_policy_id,
                        "available": policy_dir.exists(),
                    },
                )
            )
            # Writable Testnet must use the configured PostgreSQL authority;
            # SQLite is diagnostic-only and cannot provide restart/fencing
            # truth for an execution worker.
            database_url = str(getattr(settings.database, "url", "") or "")
            postgres_backend_ok = database_url.lower().startswith(("postgresql://", "postgres://"))
            checks.append(
                _result(
                    "preflight.state_backend",
                    "交易状态持久化后端",
                    postgres_backend_ok,
                    CheckSeverity.P0,
                    "Testnet 配置指向 PostgreSQL 持久化权威源",
                    "Testnet 禁止使用缺少事务出站链的 SQLite/未知状态后端",
                    evidence={"backend": "postgresql" if postgres_backend_ok else "unsupported"},
                )
            )
            if postgres_backend_ok:
                postgres_ok, postgres_message, postgres_evidence = _postgres_authority_probe(project_root, database_url)
                checks.append(
                    _result(
                        "preflight.state_backend_connection",
                        "PostgreSQL 权威源连通与迁移头",
                        postgres_ok,
                        CheckSeverity.P0,
                        postgres_message,
                        f"{postgres_message}；Testnet 保持阻断",
                        evidence=postgres_evidence,
                    )
                )
            fencing_token_text = os.environ.get("BEIDOU_FENCING_TOKEN", "").strip()
            try:
                fencing_token_ok = int(fencing_token_text) > 0
            except (TypeError, ValueError):
                fencing_token_ok = False
            checks.append(
                _result(
                    "preflight.fencing_token",
                    "执行租约 fencing token",
                    fencing_token_ok,
                    CheckSeverity.P0,
                    "BEIDOU_FENCING_TOKEN 为正整数",
                    "Testnet 缺少有效 BEIDOU_FENCING_TOKEN，双 worker/旧租约隔离未建立",
                    evidence={"present": bool(fencing_token_text), "valid": fencing_token_ok},
                )
            )
    except Exception as exc:
        checks.append(
            _result(
                "preflight.config",
                "统一配置加载",
                False,
                CheckSeverity.P0,
                "",
                f"配置加载失败: {type(exc).__name__}: {exc}",
            )
        )
        return checks, None

    try:
        from beidou_core.guard import EnvironmentGuard, StartupGateStatus

        guard = EnvironmentGuard(
            mode=mode,
            rest_url=settings.exchange.rest_base_url,
            api_key=api_key_env or settings.exchange.api_key_ref,
            api_secret=api_secret_env or settings.exchange.api_secret_ref,
            commit=commit,
            config_path="",
            evidence_dir=str(evidence_dir),
        )
        gate = guard.run_all_checks(cli_mode=mode, persist_audit=False)
        gate_ok = gate.status == StartupGateStatus.PASS
        checks.append(
            _result(
                "preflight.environment_guard",
                "环境安全门禁",
                gate_ok,
                CheckSeverity.P0,
                "EnvironmentGuard 全部通过",
                f"EnvironmentGuard 阻断: {gate.failures}",
                evidence={"status": gate.status.value, "checks": gate.checks, "failures": gate.failures},
            )
        )
    except Exception as exc:
        checks.append(
            _result(
                "preflight.environment_guard",
                "环境安全门禁",
                False,
                CheckSeverity.P0,
                "",
                f"EnvironmentGuard 执行异常: {type(exc).__name__}: {exc}",
            )
        )
    # M00-F07: 实装 plist 与模板漂移（非阻断 WARN）
    drift, plist_evidence = _launchd_plist_drift(project_root)
    checks.append(
        CheckResult(
            check_id="preflight.launchd_plist_drift",
            name="launchd 实装 plist 与模板漂移",
            status=CheckStatus.WARN if drift else CheckStatus.PASS,
            severity=CheckSeverity.P2,
            message="; ".join(drift) if drift else "实装 plist 与模板一致（或未部署）",
            evidence=plist_evidence,
        )
    )
    return checks, settings


def run_preflight(project_root: Path, mode: str, port: int) -> tuple[list[CheckResult], Any | None]:
    """Run launcher preflight; writable Testnet always requires an existing G5 certificate."""

    return _run_preflight(project_root, mode, port, require_g5_certificate=True)


def run_g5_producer_preflight(project_root: Path, port: int) -> tuple[list[CheckResult], Any | None]:
    """Run the read-only G5 producer preflight without a circular existing-G5 requirement."""

    return _run_preflight(project_root, WRITE_MODE, port, require_g5_certificate=False)
