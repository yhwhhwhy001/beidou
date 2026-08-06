"""Preflight checks for the Beidou one-click launcher."""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import socket
import sys
from pathlib import Path
from typing import Any

from .manifest import (
    CRITICAL_COMPONENTS,
    ENGINE_REQUIRED_ATTRIBUTES,
    EXPECTED_ALPHA_COMPONENTS,
    EXPECTED_FACTOR_COUNT,
    HEALTH_PORT,
    REQUIRED_PACKAGES,
    SUPPORTED_MODES,
)
from .models import CheckReport, CheckResult, CheckStatus


def find_project_root(start: Path | None = None) -> Path:
    """Find the repository root by locating pyproject.toml."""

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError("Unable to locate pyproject.toml; run the launcher from the Beidou repository")


def _resolve_target(target: str) -> Any:
    module_name, attribute_name = target.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attribute_name)


class PreflightChecker:
    """Run deterministic checks before any child process is started."""

    def __init__(self, mode: str, symbols: list[str], port: int = HEALTH_PORT) -> None:
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.root = find_project_root()

    def run(self) -> CheckReport:
        report = CheckReport(phase="preflight", mode=self.mode)
        report.results.extend(
            [
                self._check_mode(),
                self._check_python(),
                self._check_project_layout(),
                self._check_runtime_directories(),
                self._check_port(),
            ]
        )
        report.results.extend(self._check_packages())
        report.results.extend(self._check_components())
        report.results.extend(self._check_configuration())
        report.results.extend(self._check_engine_wiring())
        return report.finish()

    def _check_mode(self) -> CheckResult:
        if self.mode in SUPPORTED_MODES:
            return CheckResult(
                code="PF-MODE",
                subject="运行模式",
                status=CheckStatus.PASS,
                message=f"模式 {self.mode} 已纳入封闭枚举",
                evidence={"supported_modes": list(SUPPORTED_MODES)},
            )
        return CheckResult(
            code="PF-MODE",
            subject="运行模式",
            status=CheckStatus.FAIL,
            message=f"未知模式 {self.mode}; 已按 fail-closed 阻断",
            evidence={"supported_modes": list(SUPPORTED_MODES)},
        )

    @staticmethod
    def _check_python() -> CheckResult:
        version = sys.version_info
        passed = version >= (3, 12)
        return CheckResult(
            code="PF-PYTHON",
            subject="Python 运行时",
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            message=f"Python {version.major}.{version.minor}.{version.micro}",
            evidence={"required": ">=3.12,<4.0", "executable": sys.executable},
        )

    def _check_project_layout(self) -> CheckResult:
        required = ["pyproject.toml", "apps/autopilot/__main__.py", "beidou_core/engine.py"]
        missing = [path for path in required if not (self.root / path).is_file()]
        return CheckResult(
            code="PF-LAYOUT",
            subject="项目结构",
            status=CheckStatus.FAIL if missing else CheckStatus.PASS,
            message="关键入口完整" if not missing else f"缺少关键文件: {', '.join(missing)}",
            evidence={"project_root": str(self.root), "missing": missing},
        )

    def _check_runtime_directories(self) -> CheckResult:
        created: list[str] = []
        try:
            for relative in (".beidou", "logs", "evidence/BD-STARTUP"):
                path = self.root / relative
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                created.append(relative)
        except OSError as exc:
            return CheckResult(
                code="PF-FILESYSTEM",
                subject="运行目录",
                status=CheckStatus.FAIL,
                message=f"运行目录不可写: {exc}",
                evidence={"created": created},
            )
        return CheckResult(
            code="PF-FILESYSTEM",
            subject="运行目录",
            status=CheckStatus.PASS,
            message="运行锁、日志和证据目录可写",
            evidence={"directories": created},
        )

    def _check_port(self) -> CheckResult:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            in_use = sock.connect_ex(("127.0.0.1", self.port)) == 0
        return CheckResult(
            code="PF-PORT",
            subject="健康端口",
            status=CheckStatus.FAIL if in_use else CheckStatus.PASS,
            message=f"端口 {self.port} {'已被占用' if in_use else '可用'}",
            evidence={"port": self.port},
        )

    @staticmethod
    def _check_packages() -> list[CheckResult]:
        results: list[CheckResult] = []
        for package in REQUIRED_PACKAGES:
            try:
                importlib.import_module(package)
            except Exception as exc:  # noqa: BLE001 - import failures are evidence
                results.append(
                    CheckResult(
                        code="PF-PACKAGE",
                        subject=package,
                        status=CheckStatus.FAIL,
                        message=f"业务包导入失败: {type(exc).__name__}: {exc}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        code="PF-PACKAGE",
                        subject=package,
                        status=CheckStatus.PASS,
                        message="业务包可导入",
                    )
                )
        return results

    @staticmethod
    def _check_components() -> list[CheckResult]:
        results: list[CheckResult] = []
        for name, target in CRITICAL_COMPONENTS.items():
            try:
                resolved = _resolve_target(target)
            except Exception as exc:  # noqa: BLE001 - component import failures are evidence
                results.append(
                    CheckResult(
                        code="PF-COMPONENT",
                        subject=name,
                        status=CheckStatus.FAIL,
                        message=f"关键组件不可用: {type(exc).__name__}: {exc}",
                        evidence={"target": target},
                    )
                )
            else:
                results.append(
                    CheckResult(
                        code="PF-COMPONENT",
                        subject=name,
                        status=CheckStatus.PASS,
                        message="关键组件存在",
                        evidence={"target": target, "type": getattr(resolved, "__name__", str(resolved))},
                    )
                )
        return results

    def _check_configuration(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        try:
            from beidou_shared.config import ConfigProvider

            settings = ConfigProvider().load(environment=self.mode)
        except Exception as exc:  # noqa: BLE001 - configuration failures must block startup
            return [
                CheckResult(
                    code="PF-CONFIG",
                    subject="统一配置",
                    status=CheckStatus.FAIL,
                    message=f"配置加载失败: {type(exc).__name__}: {exc}",
                )
            ]

        rest_url = settings.exchange.rest_base_url
        env_matches = settings.environment.value == self.mode or self.mode == "safety_only"
        results.append(
            CheckResult(
                code="PF-CONFIG",
                subject="统一配置",
                status=CheckStatus.PASS if env_matches else CheckStatus.FAIL,
                message=(
                    f"配置来源 {settings.source}, 环境 {settings.environment.value}"
                    if env_matches
                    else f"请求模式 {self.mode} 被解析为 {settings.environment.value}"
                ),
                evidence={
                    "source": settings.source,
                    "environment": settings.environment.value,
                    "config_hash": settings.config_hash,
                    "rest_base_url": rest_url,
                },
            )
        )

        is_testnet_url = any(marker in rest_url.lower() for marker in ("testnet", "demo", "staging"))
        results.append(
            CheckResult(
                code="PF-EXCHANGE-URL",
                subject="交易所环境",
                status=CheckStatus.PASS if is_testnet_url else CheckStatus.FAIL,
                message=("检测到测试网地址" if is_testnet_url else "未检测到测试网标记，Mainnet 风险已阻断"),
                evidence={"rest_base_url": rest_url},
            )
        )

        api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "") or settings.exchange.api_key_ref
        api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "") or settings.exchange.api_secret_ref
        credentials_present = len(api_key) >= 10 and len(api_secret) >= 10
        results.append(
            CheckResult(
                code="PF-CREDENTIALS",
                subject="账户访问凭据",
                status=CheckStatus.PASS if credentials_present else CheckStatus.FAIL,
                message=(
                    "API Key/Secret 已注入（内容不记录）"
                    if credentials_present
                    else "当前 Autopilot 启动阶段会读取账户快照，缺少 API Key/Secret"
                ),
                evidence={"api_key_present": bool(api_key), "api_secret_present": bool(api_secret)},
            )
        )

        signing_key_present = bool(os.environ.get("BEIDOU_SIGNING_KEY", ""))
        signing_required = self.mode == "testnet"
        results.append(
            CheckResult(
                code="PF-SIGNING",
                subject="风险审批签名",
                status=(
                    CheckStatus.PASS
                    if signing_key_present or not signing_required
                    else CheckStatus.FAIL
                ),
                message=(
                    "签名密钥已注入（内容不记录）"
                    if signing_key_present
                    else (
                        "Testnet 风险增加路径缺少 BEIDOU_SIGNING_KEY"
                        if signing_required
                        else "零写模式使用隔离的非生产签名器"
                    )
                ),
                evidence={"required": signing_required, "present": signing_key_present},
            )
        )
        return results

    def _check_engine_wiring(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        previous_environment = os.environ.get("BEIDOU_ENV")
        os.environ["BEIDOU_ENV"] = self.mode
        try:
            from beidou_core.engine import AutonomousEngine

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                engine = AutonomousEngine(symbols=self.symbols, mode=self.mode)
        except Exception as exc:  # noqa: BLE001 - constructor failures are startup blockers
            return [
                CheckResult(
                    code="PF-ENGINE-CONSTRUCT",
                    subject="AutonomousEngine",
                    status=CheckStatus.FAIL,
                    message=f"引擎装配失败: {type(exc).__name__}: {exc}",
                )
            ]
        finally:
            if previous_environment is None:
                os.environ.pop("BEIDOU_ENV", None)
            else:
                os.environ["BEIDOU_ENV"] = previous_environment

        missing_attrs = [attribute for attribute in ENGINE_REQUIRED_ATTRIBUTES if not hasattr(engine, attribute)]
        results.append(
            CheckResult(
                code="PF-ENGINE-WIRING",
                subject="引擎模块装配",
                status=CheckStatus.FAIL if missing_attrs else CheckStatus.PASS,
                message="关键模块全部装配" if not missing_attrs else f"缺少装配项: {', '.join(missing_attrs)}",
                evidence={"missing": missing_attrs, "required_count": len(ENGINE_REQUIRED_ATTRIBUTES)},
            )
        )

        component_map = getattr(getattr(engine, "_alpha_graph", None), "_components", {})
        actual_components = frozenset(component_map.keys())
        missing_components = sorted(EXPECTED_ALPHA_COMPONENTS - actual_components)
        extra_components = sorted(actual_components - EXPECTED_ALPHA_COMPONENTS)
        validation_failures: list[str] = []
        for component_id, component in component_map.items():
            try:
                if not bool(component.validate()):
                    validation_failures.append(component_id)
            except Exception as exc:  # noqa: BLE001 - algorithm validation must be isolated
                validation_failures.append(f"{component_id}:{type(exc).__name__}")

        algorithms_ok = not missing_components and not validation_failures
        results.append(
            CheckResult(
                code="PF-ALGORITHMS",
                subject="Alpha 算法 DAG",
                status=CheckStatus.PASS if algorithms_ok else CheckStatus.FAIL,
                message=(
                    f"{len(actual_components)} 个算法组件已注册并通过 validate()"
                    if algorithms_ok
                    else "算法注册或自校验不完整"
                ),
                evidence={
                    "actual": sorted(actual_components),
                    "missing": missing_components,
                    "extra": extra_components,
                    "validation_failures": validation_failures,
                },
            )
        )

        factor_registry = getattr(engine, "_factor_registry", None)
        factor_map = getattr(factor_registry, "_factors", {}) if factor_registry is not None else {}
        active_factors = []
        for factor_id, record in factor_map.items():
            lifecycle = getattr(getattr(record, "lifecycle", None), "value", "UNKNOWN")
            if lifecycle in {"ACTIVE", "CHALLENGER"}:
                active_factors.append(factor_id)
        factor_ok = len(factor_map) >= EXPECTED_FACTOR_COUNT and len(active_factors) >= EXPECTED_FACTOR_COUNT
        results.append(
            CheckResult(
                code="PF-FACTORS",
                subject="因子生命周期",
                status=CheckStatus.PASS if factor_ok else CheckStatus.FAIL,
                message=(
                    f"{len(factor_map)} 个因子已注册，{len(active_factors)} 个处于可运行生命周期"
                    if factor_ok
                    else "因子注册或生命周期晋级不完整"
                ),
                evidence={
                    "registered": sorted(factor_map.keys()),
                    "active_or_challenger": sorted(active_factors),
                    "expected_minimum": EXPECTED_FACTOR_COUNT,
                },
            )
        )

        pool = getattr(engine, "_trading_pool", None)
        active_count = int(pool.active_count()) if pool is not None else 0
        results.append(
            CheckResult(
                code="PF-TRADING-POOL",
                subject="交易池",
                status=CheckStatus.PASS if active_count > 0 else CheckStatus.FAIL,
                message=f"交易池激活 {active_count} 个标的",
                evidence={"requested_symbols": self.symbols, "active_count": active_count},
            )
        )
        return results
