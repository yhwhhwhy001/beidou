"""一键启动前置检查与安全门禁。"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .models import CheckResult, CheckSeverity, CheckStatus
from .registry import check_package_imports

WRITE_MODE = "testnet"


def current_commit(project_root: Path | None = None) -> str:
    try:
        result = subprocess.run(
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
        result = subprocess.run(
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
        sock.bind(("0.0.0.0", port))
        return True, "available"
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def run_preflight(project_root: Path, mode: str, port: int) -> tuple[list[CheckResult], Any | None]:
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
        strict = mode == WRITE_MODE
        checks.append(
            CheckResult(
                check_id="preflight.git_worktree",
                name="Git 工作区状态",
                status=CheckStatus.FAIL if strict else CheckStatus.WARN,
                severity=CheckSeverity.P0 if strict else CheckSeverity.P2,
                message=(
                    "Testnet 禁止从未提交工作区启动"
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
    writable = True
    write_error = ""
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        probe = runtime_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        writable = False
        write_error = str(exc)
    checks.append(
        _result(
            "preflight.runtime_storage",
            "运行证据目录",
            writable,
            CheckSeverity.P1,
            "运行状态和证据目录可写",
            f"运行目录不可写: {write_error}",
            evidence={"runtime_dir": str(runtime_dir), "evidence_dir": str(evidence_dir)},
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
            cred_fail_msg = (
                "未提供 API Key/Secret，引擎将无法读取真实账户数据。"
                "非写模式下此为警告，引擎会降级运行。"
            )
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
        if mode == WRITE_MODE:
            checks.append(
                _result(
                    "preflight.signing_key",
                    "风险批准签名密钥",
                    len(signing_key) >= 16,
                    CheckSeverity.P0,
                    "BEIDOU_SIGNING_KEY 已提供",
                    "Testnet 模式缺少至少 16 字符的 BEIDOU_SIGNING_KEY",
                    evidence={"present": bool(signing_key), "length": len(signing_key)},
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
        gate = guard.run_all_checks(cli_mode=mode)
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
    return checks, settings
