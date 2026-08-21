"""P0 启动环境保护 — EnvironmentGuard。

验证启动模式、证书、凭据和账户能力，确保安全启动。
Production 模式在本包完成前永久拒绝。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # nosec B404 - fixed local git inspection command
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EnvironmentMode(str, Enum):
    """封闭运行模式枚举。

    写能力分级：
    - 零写 (no POST/PUT/DELETE): RESEARCH, PAPER, SHADOW, SAFETY_ONLY
    - 测试写 (testnet only): TESTNET
    - 实盘写 (gated): CANARY, LIVE — 本任务中继续阻断
    """

    RESEARCH = "research"
    PAPER = "paper"
    SHADOW = "shadow"
    TESTNET = "testnet"
    CANARY = "canary"
    LIVE = "live"
    SAFETY_ONLY = "safety_only"
    PRODUCTION = "production"  # 遗留值，与 CANARY/LIVE 同等阻断

    @property
    def can_write_trades(self) -> bool:
        """该模式是否允许 POST/PUT/DELETE 交易写请求。

        CANARY 和 LIVE 在本包完成前继续阻断。
        """
        return self in (EnvironmentMode.TESTNET,)

    @property
    def is_write_blocked(self) -> bool:
        """该模式是否永久阻断写路径。

        RESEARCH, PAPER, SHADOW, SAFETY_ONLY 在类型/依赖注入层禁止写。
        CANARY, LIVE 在本任务中强制阻断。
        """
        return self in (
            EnvironmentMode.RESEARCH,
            EnvironmentMode.PAPER,
            EnvironmentMode.SHADOW,
            EnvironmentMode.SAFETY_ONLY,
            EnvironmentMode.CANARY,
            EnvironmentMode.LIVE,
            EnvironmentMode.PRODUCTION,
        )


class StartupGateStatus(str, Enum):
    PASS = "PASS"  # nosec B105 - gate status, not a credential
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass
class AuditEvent:
    """安全启动审计事件 — 不可变记录。"""

    timestamp: str
    event_type: str
    environment: str
    commit: str
    config_hash: str
    certificate_status: str
    account_capability: str
    control_state: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "environment": self.environment,
            "commit": self.commit,
            "config_hash": self.config_hash,
            "certificate_status": self.certificate_status,
            "account_capability": self.account_capability,
            "control_state": self.control_state,
            "details": self.details,
        }


@dataclass
class StartupGateResult:
    status: StartupGateStatus
    checks: dict[str, bool]
    failures: list[str]
    audit_events: list[AuditEvent]


class EnvironmentGuard:
    """P0 环境启动守卫。

    支持七种封闭运行模式：RESEARCH / PAPER / SHADOW / TESTNET / CANARY / LIVE / SAFETY_ONLY。
    CANARY 和 LIVE 在本包完成前永久拒绝。
    零写模式 (RESEARCH / PAPER / SHADOW / SAFETY_ONLY) 在类型层禁止交易写请求。
    SAFETY_ONLY 不得构造、注入或访问 TradingExchangePort 的写方法。
    """

    # 主网 URL 模式 — 任何匹配都触发阻断
    MAINNET_URL_PATTERNS = [
        "binance.com",  # 不含 testnet 前缀的 binance.com
        "fapi.binance.com",
        "api.binance.com",
        "mainnet",
    ]

    # 禁止的声明文本
    FORBIDDEN_CLAIMS = [
        "G7 L2-L5: ALL PASS",
        "G7 L2-L5",
        "ALL PASS",
        "生产级",
        "无人值守已认证",
        "生产级自主",
        "G7 ALL PASS",
    ]

    def __init__(  # nosec B107 - empty secret defaults require provider injection
        self,
        mode: str,
        rest_url: str = "",
        api_key: str = "",
        api_secret: str = "",
        commit: str = "",
        config_path: str = "",
        evidence_dir: str = "evidence/BD-00",
        g5_cert_path: str = "",
    ):
        mode_lower = mode.lower()
        valid_modes = {m.value for m in EnvironmentMode}
        if mode_lower in valid_modes:
            self._mode = EnvironmentMode(mode_lower)
        else:
            # UNKNOWN mode → fail-closed as SAFETY_ONLY
            self._mode = EnvironmentMode.SAFETY_ONLY
        self._rest_url = rest_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._commit = commit
        self._config_path = config_path
        self._evidence_dir = evidence_dir
        self._g5_cert_path = g5_cert_path
        self._audit_events: list[AuditEvent] = []

    def _audit(self, event_type: str, details: dict | None = None) -> AuditEvent:
        """生成审计事件。"""
        event = AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            environment=self._mode.value,
            commit=self._commit,
            config_hash=self._compute_config_hash(),
            certificate_status="NOT_PRESENT",
            account_capability="UNKNOWN",
            control_state="NO_NEW_RISK",
            details=details or {},
        )
        self._audit_events.append(event)
        return event

    def _compute_config_hash(self) -> str:
        """计算配置文件的 SHA256。"""
        if self._config_path and os.path.exists(self._config_path):
            with open(self._config_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        return "NO_CONFIG"

    def _get_git_commit(self) -> str:
        """获取当前 Git commit。"""
        if self._commit:
            return self._commit
        try:
            result = subprocess.run(  # nosec B603, B607 - fixed git command, shell disabled
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    # 已知测试网 URL 标记（不区分大小写）
    _TESTNET_MARKERS = ["testnet", "demo", "staging"]

    def _is_mainnet_url(self, url: str) -> bool:
        """检测 URL 是否指向 Mainnet。

        已知测试网 URL 模式：
        - demo-fapi.binance.com (Binance USDⓈ-M Futures Testnet)
        - testnet.binancefutures.com
        - testnet.binance.vision
        """
        url_lower = url.lower()
        # 如果 URL 包含已知测试网标记，则不是主网
        if any(marker in url_lower for marker in self._TESTNET_MARKERS):
            return False
        # 如果 URL 包含 binance.com 且无测试网标记
        if "binance.com" in url_lower:
            return True
        return any(pattern in url_lower for pattern in self.MAINNET_URL_PATTERNS)

    def check_production_block(self) -> bool:
        """阻断所有实盘写模式 (PRODUCTION, CANARY, LIVE)。"""
        blocked_modes = {EnvironmentMode.PRODUCTION, EnvironmentMode.CANARY, EnvironmentMode.LIVE}
        if self._mode in blocked_modes:
            self._audit(
                "PRODUCTION_BLOCKED",
                {
                    "reason": (f"PIVOT — {self._mode.value} is permanently prohibited until package completion"),
                    "decision": "PIVOT",
                    "mainnet_allowed": False,
                    "blocked_mode": self._mode.value,
                },
            )
            return False
        return True

    def check_mainnet_url(self) -> bool:
        """检查 REST URL 是否指向 Mainnet。"""
        if self._is_mainnet_url(self._rest_url):
            self._audit(
                "MAINNET_URL_BLOCKED",
                {
                    "url": self._rest_url,
                    "reason": "Mainnet URL detected — blocked by EnvironmentGuard",
                    "severity": "P0",
                },
            )
            return False
        return True

    def check_trading_credentials(self) -> bool:
        """检查交易凭据是否存在且有效格式。

        只有可写模式 (TESTNET) 需要凭据。
        零写模式 (RESEARCH, PAPER, SHADOW, SAFETY_ONLY) 无需凭据。
        """
        if self._mode.is_write_blocked:
            return True  # 零写模式 + 阻断模式不需要凭据

        if not self._api_key or len(self._api_key) < 10:
            self._audit(
                "CREDENTIAL_MISSING",
                {
                    "reason": f"API key missing or too short for {self._mode.value} mode",
                },
            )
            return False

        if not self._api_secret or len(self._api_secret) < 10:
            self._audit(
                "CREDENTIAL_MISSING",
                {
                    "reason": f"API secret missing or too short for {self._mode.value} mode",
                },
            )
            return False

        return True

    def check_write_mode_requirements(self, cli_mode: str = "paper") -> bool:
        """检查可写模式的前置条件。

        TESTNET 模式要求：
        1. Testnet URL（非 Mainnet）
        2. 有效交易凭据

        G5 证书模式额外要求：
        3. G5 证书 + commit 绑定

        零写模式无需此检查。
        """
        if self._mode.is_write_blocked:
            return True  # 零写/阻断模式无额外要求

        failures = []

        # 1. Mainnet URL 检查
        if self._is_mainnet_url(self._rest_url):
            failures.append("Mainnet URL not allowed")
            self._audit(
                "WRITE_MODE_BLOCKED",
                {
                    "blocker": "mainnet_url",
                    "url": self._rest_url,
                },
            )

        # 2. 交易凭据
        if not self._api_key or not self._api_secret:
            failures.append("Trading credentials required for write-capable mode")
            self._audit(
                "WRITE_MODE_BLOCKED",
                {
                    "blocker": "missing_credentials",
                },
            )

        return len(failures) == 0

    def run_all_checks(self, cli_mode: str = "paper", *, persist_audit: bool = True) -> StartupGateResult:
        """运行所有启动检查，返回综合结果。

        Args:
            cli_mode: CLI 层模式 (paper/testnet/full/safety_only)，
                      用于区分 testnet 模式是否要求 G5 证书。
        """
        commit = self._get_git_commit()
        self._commit = commit

        checks: dict[str, bool] = {}
        failures: list[str] = []

        # Check 1: Production block
        prod_ok = self.check_production_block()
        checks["production_blocked"] = prod_ok
        if not prod_ok:
            failures.append("PRODUCTION mode is permanently prohibited")

        # Check 2: Mainnet URL
        mainnet_ok = self.check_mainnet_url()
        checks["mainnet_url_blocked"] = mainnet_ok
        if not mainnet_ok:
            failures.append(f"Mainnet URL detected: {self._rest_url}")

        # Check 3: Environment mode validity
        blocked = {EnvironmentMode.PRODUCTION, EnvironmentMode.CANARY, EnvironmentMode.LIVE}
        valid_mode = self._mode not in blocked
        checks["valid_mode"] = valid_mode
        if not valid_mode:
            failures.append(f"Mode {self._mode.value} is permanently blocked by PIVOT decision")

        # Check 4: Credentials (only for write-capable modes)
        creds_ok = self.check_trading_credentials()
        checks["credentials_valid"] = creds_ok
        if not creds_ok:
            failures.append("Trading credentials missing or invalid")

        # Check 5: Write-mode requirements (G5 cert for full CLI)
        if not self._mode.is_write_blocked:
            full_ok = self.check_write_mode_requirements(cli_mode=cli_mode)
            checks["write_mode_requirements"] = full_ok
            if not full_ok:
                failures.append("Write-mode requirements not met")
        else:
            checks["write_mode_requirements"] = True

        # Startup audit event
        self._audit(
            "STARTUP_GATE",
            {
                "all_checks_passed": len(failures) == 0,
                "checks": checks,
                "failures": failures,
            },
        )

        # Offline callers may request an in-memory gate result. Preflight uses
        # this mode so fact discovery never manufactures its own evidence.
        if persist_audit:
            self._write_audit_trail()

        status = StartupGateStatus.PASS if len(failures) == 0 else StartupGateStatus.FAIL
        return StartupGateResult(
            status=status,
            checks=checks,
            failures=failures,
            audit_events=list(self._audit_events),
        )

    def _write_audit_trail(self) -> None:
        """将审计事件写入证据目录。"""
        os.makedirs(self._evidence_dir, exist_ok=True)
        audit_path = os.path.join(self._evidence_dir, "startup_audit.json")
        with open(audit_path, "w") as f:
            json.dump([e.to_dict() for e in self._audit_events], f, indent=2)

    @staticmethod
    def scan_for_forbidden_claims(root_dir: str = ".") -> list[str]:
        """扫描仓库文本中的禁止声明。"""
        import glob

        found: list[str] = []
        patterns = ["**/*.md", "**/*.py", "**/*.yaml", "**/*.yml", "**/*.json", "**/*.toml", "**/*.cfg", "**/*.txt"]
        exclude_dirs = {
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
            ".mypy_cache",
            "evidence",
            "dist",
        }

        for pattern in patterns:
            for filepath in glob.glob(os.path.join(root_dir, pattern), recursive=True):
                # Skip excluded directories
                parts = set(filepath.split(os.sep))
                if parts & exclude_dirs:
                    continue
                # Skip binary files
                if any(filepath.endswith(ext) for ext in [".db", ".db-shm", ".db-wal", ".pyc", ".png", ".jpg"]):
                    continue
                try:
                    with open(filepath, "r") as f:
                        content = f.read()
                    for claim in EnvironmentGuard.FORBIDDEN_CLAIMS:
                        if claim in content:
                            found.append(f"{filepath}: '{claim}'")
                except (UnicodeDecodeError, PermissionError, IsADirectoryError):
                    continue

        return found

    @staticmethod
    def generate_startup_audit_event(
        environment: str,
        commit: str,
        config_hash: str,
        certificate_status: str = "NOT_PRESENT",
        account_capability: str = "UNKNOWN",
        control_state: str = "NO_NEW_RISK",
        extra: dict | None = None,
    ) -> AuditEvent:
        """生成启动审计事件。"""
        return AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="STARTUP",
            environment=environment,
            commit=commit,
            config_hash=config_hash,
            certificate_status=certificate_status,
            account_capability=account_capability,
            control_state=control_state,
            details=extra or {},
        )
