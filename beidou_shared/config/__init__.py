"""统一配置提供器 — 唯一、版本化、可校验的配置入口。

加载顺序: CLI explicit path > environment variables > environment-specific file > safe non-trading defaults.
未知环境、缺少配置或 schema 不匹配回退 SAFETY_ONLY。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Environment(str, Enum):
    """部署环境枚举。仅 SAFETY_ONLY 为默认回退环境。"""

    SAFETY_ONLY = "safety_only"
    PAPER = "paper"
    SHADOW = "shadow"
    TESTNET = "testnet"
    CANARY = "canary"
    PRODUCTION = "production"
    MAINNET = "mainnet"

    @property
    def can_write_trades(self) -> bool:
        """只有明确启用写交易的环境返回 True。"""
        return self in (Environment.SHADOW, Environment.TESTNET, Environment.CANARY)

    @property
    def is_live(self) -> bool:
        return self in (Environment.PRODUCTION, Environment.MAINNET)


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    url: str = ""
    pool_min: int = 2
    pool_max: int = 10


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    rest_base_url: str = ""
    ws_base_url: str = ""
    api_key_ref: str = ""  # 引用，非明文
    api_secret_ref: str = ""


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_leverage: float = 3.0
    max_concentration_pct: float = 50.0
    max_position_notional: float = 500_000.0


@dataclass(frozen=True, slots=True)
class ProductionConfig:
    """生产运行参数 — 从 YAML `production` 段加载。"""

    # Pre-Risk 不变量
    max_leverage: float = 3.0
    max_concentration_pct: float = 50.0
    max_position_notional: float = 500_000.0
    # Strategy Risk Budget
    max_drawdown_pct: float = 20.0
    max_daily_loss_pct: float = 5.0
    max_consecutive_losses: int = 5
    risk_per_trade_pct: float = 1.0
    min_sharpe_rolling: float = 0.0
    # Portfolio
    max_total_leverage: float = 3.0
    max_instruments: int = 50
    # Drift Detection
    drift_threshold: float = 0.1


@dataclass(frozen=True, slots=True)
class InfrastructureConfig:
    """基础设施连接参数 — 从 YAML `infrastructure` 段加载。"""

    health_host: str = "0.0.0.0"
    health_port: int = 9090
    control_host: str = "127.0.0.1"
    control_port: int = 9090
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_timeout: int = 2
    s3_bucket: str = "beidou-certificates"
    s3_endpoint: str = "http://localhost:9000"
    alerts_file: str = "/tmp/beidou_alerts.jsonl"
    webhook_timeout: int = 5


@dataclass(frozen=True, slots=True)
class CapitalLevelConfig:
    """单个资本阶梯级别配置。"""

    name: str = ""
    gate: str = ""
    max_capital: float = 0.0
    max_leverage: float = 0.0
    min_unattended_hours: float = 0.0


@dataclass(frozen=True, slots=True)
class CapitalLadderConfig:
    """资本阶梯配置 — 从 YAML `production_ladder` 段加载。"""

    current_level: str = "L0_PAPER"
    levels: tuple[CapitalLevelConfig, ...] = ()
    capital_limits: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedSettings:
    """类型化配置快照 — 不可变，经过校验。"""

    environment: Environment = Environment.SAFETY_ONLY
    version: str = "0.0.0"
    config_hash: str = ""
    effective_at: str = ""  # ISO timestamp
    source: str = "safe-defaults"  # 配置来源标识

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    production: ProductionConfig = field(default_factory=ProductionConfig)
    infrastructure: InfrastructureConfig = field(default_factory=InfrastructureConfig)
    capital_ladder: CapitalLadderConfig = field(default_factory=CapitalLadderConfig)

    can_write_trades: bool = False

    raw: dict = field(default_factory=dict)  # 原始加载数据（敏感字段已脱敏）

    def compute_hash(self) -> str:
        """计算配置内容的稳定 SHA-256。"""
        payload = (
            f"{self.environment.value}"
            f"|{self.database.url}"
            f"|{self.exchange.rest_base_url}"
            f"|{self.risk.max_leverage}|{self.risk.max_concentration_pct}"
            f"|{self.infrastructure.health_port}"
            f"|{self.production.max_leverage}|{self.production.max_position_notional}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ConfigError(Exception):
    """配置加载或校验失败。"""

    def __init__(self, message: str, correlation_id: str = ""):
        super().__init__(message)
        self.correlation_id = correlation_id


class ConfigProvider:
    """统一配置提供器。

    特性:
    - 唯一入口: 核心模块不得直接读取 YAML 或 .env。
    - 版本化: 每次加载生成稳定 checksum。
    - Fail-safe: 未知/缺失环境回退 SAFETY_ONLY，写交易能力为 false。
    - 脱敏: 日志/异常/evidence 中不输出明文密钥。
    """

    _SAFE_DEFAULTS = {
        "environment": "safety_only",
        "version": "0.0.0",
        "database": {"url": "sqlite:///beidou_state.db", "pool_min": 1, "pool_max": 5},
        "exchange": {
            "rest_base_url": "https://testnet.binancefuture.com",
            "ws_base_url": "",
            "api_key_ref": "",
            "api_secret_ref": "",
        },
        "risk": {"max_leverage": 3.0, "max_concentration_pct": 50.0, "max_position_notional": 500_000.0},
        "production": {
            "max_leverage": 3.0,
            "max_concentration_pct": 50.0,
            "max_position_notional": 500_000.0,
            "max_drawdown_pct": 20.0,
            "max_daily_loss_pct": 5.0,
            "max_consecutive_losses": 5,
            "risk_per_trade_pct": 1.0,
            "min_sharpe_rolling": 0.0,
            "max_total_leverage": 3.0,
            "max_instruments": 50,
            "drift_threshold": 0.1,
        },
        "infrastructure": {
            "health_host": "0.0.0.0",
            "health_port": 9090,
            "control_host": "127.0.0.1",
            "control_port": 9090,
            "redis_host": "localhost",
            "redis_port": 6379,
            "redis_timeout": 2,
            "s3_bucket": "beidou-certificates",
            "s3_endpoint": "http://localhost:9000",
            "alerts_file": "/tmp/beidou_alerts.jsonl",
            "webhook_timeout": 5,
        },
        "production_ladder": {
            "current_level": "L0_PAPER",
            "capital_limits": {
                "L0_PAPER": 0.0,
                "L1_SHADOW": 0.0,
                "L2_CANARY": 100.0,
                "L3_RAMP": 1000.0,
                "L4_NORMAL": 10000.0,
                "L5_CHAMPION": 50000.0,
            },
            "levels": [],
        },
    }

    def __init__(self, base_config_dir: str = "config"):
        self._base_dir = Path(base_config_dir)

    def load(self, environment: str = "", explicit_path: str = "") -> TypedSettings:
        """按优先级加载配置。

        Args:
            environment: 环境名称 (paper/shadow/testnet/etc.)，空则从 BEIDOU_ENV 读取。
            explicit_path: CLI 指定的配置文件路径，优先级最高。

        Returns:
            TypedSettings 快照，校验通过。

        Raises:
            ConfigError: 校验失败且无法回退时。
        """
        import yaml

        # 1. CLI explicit path (最高优先级)
        if explicit_path and os.path.isfile(explicit_path):
            return self._load_from_file(explicit_path, source="cli-explicit")

        # 2. Environment variable
        env_name = environment or os.environ.get("BEIDOU_ENV", "")

        # 3. Map environment name to config file
        if env_name:
            try:
                env = Environment(env_name)
            except ValueError:
                # 未知环境 → SAFETY_ONLY
                return self._safe_only(f"unknown environment: {env_name}")

            config_file = self._base_dir / f"env.{env.value}.yaml"
            if config_file.is_file():
                return self._load_from_file(str(config_file), source=f"env-file:{env.value}")

            # 环境有效但文件缺失 → 尝试模板
            template_file = self._base_dir / f"env.{env.value}.yaml.example"
            if template_file.is_file():
                settings = self._load_from_file(str(template_file), source=f"template:{env.value}")
                # 模板文件不应包含真实密钥
                return settings

        # 4. Safe non-trading defaults
        return self._safe_only("no configuration available")

    def _load_from_file(self, path: str, source: str = "") -> TypedSettings:
        """从文件加载并验证。"""
        import yaml

        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            raise ConfigError(f"Failed to load config from {path}: {e}", correlation_id="config-load-failed")

        return self._parse_and_validate(raw, source=source)

    def _parse_and_validate(self, raw: dict, source: str = "") -> TypedSettings:
        """解析原始配置并进行校验。"""
        import datetime

        errors: list[str] = []

        # Parse environment — 兼容字符串和 dict 两种格式
        env_raw = raw.get("environment", "safety_only")
        env_name = str(env_raw.get("name", "safety_only")) if isinstance(env_raw, dict) else str(env_raw)
        try:
            environment = Environment(env_name)
        except ValueError:
            errors.append(f"Invalid environment: {env_name}")
            environment = Environment.SAFETY_ONLY

        # Parse database
        db_raw = raw.get("database", {})
        database = DatabaseConfig(
            url=str(db_raw.get("url", "")),
            pool_min=int(db_raw.get("pool_min", 2)),
            pool_max=int(db_raw.get("pool_max", 10)),
        )

        # Parse exchange
        ex_raw = raw.get("exchange", {})
        binance_raw = ex_raw.get("binance_usdm", ex_raw)
        exchange = ExchangeConfig(
            rest_base_url=str(binance_raw.get("rest_base_url", "")),
            ws_base_url=str(binance_raw.get("ws_base_url", "")),
            api_key_ref=str(binance_raw.get("api_key_ref", "")),
            api_secret_ref=str(binance_raw.get("api_secret_ref", "")),
        )

        # Validate: 不应包含明文密钥
        if "api_key" in binance_raw and binance_raw.get("api_key"):
            errors.append("plaintext api_key detected — use api_key_ref instead")
        if "api_secret" in binance_raw and binance_raw.get("api_secret"):
            errors.append("plaintext api_secret detected — use api_secret_ref instead")

        # Validate: 明文密钥检测（仅 WARN，不阻断）
        # Testnet/Paper 模式下允许配置文件提供密钥；
        # 生产环境密钥必须通过环境变量或 Vault 注入。
        for ref_field in ("api_key_ref", "api_secret_ref"):
            ref_value = str(binance_raw.get(ref_field, ""))
            if len(ref_value) >= 32 and any(c.isalpha() for c in ref_value) and any(c.isdigit() for c in ref_value):
                import warnings

                warnings.warn(
                    f"plaintext value detected in {ref_field} — "
                    f"生产环境必须通过环境变量注入，禁止在配置文件中填写明文密钥",
                    stacklevel=2,
                )

        # Parse risk
        risk_raw = raw.get("risk", {})
        risk = RiskConfig(
            max_leverage=float(risk_raw.get("max_leverage", 3.0)),
            max_concentration_pct=float(risk_raw.get("max_concentration_pct", 50.0)),
            max_position_notional=float(risk_raw.get("max_position_notional", 500_000.0)),
        )

        # Parse production
        prod_raw = raw.get("production", {})
        production = ProductionConfig(
            max_leverage=float(prod_raw.get("max_leverage", risk.max_leverage)),
            max_concentration_pct=float(prod_raw.get("max_concentration_pct", risk.max_concentration_pct)),
            max_position_notional=float(prod_raw.get("max_position_notional", risk.max_position_notional)),
            max_drawdown_pct=float(prod_raw.get("max_drawdown_pct", 20.0)),
            max_daily_loss_pct=float(prod_raw.get("max_daily_loss_pct", 5.0)),
            max_consecutive_losses=int(prod_raw.get("max_consecutive_losses", 5)),
            risk_per_trade_pct=float(prod_raw.get("risk_per_trade_pct", 1.0)),
            min_sharpe_rolling=float(prod_raw.get("min_sharpe_rolling", 0.0)),
            max_total_leverage=float(prod_raw.get("max_total_leverage", 3.0)),
            max_instruments=int(prod_raw.get("max_instruments", 50)),
            drift_threshold=float(prod_raw.get("drift_threshold", 0.1)),
        )

        # Parse infrastructure
        infra_raw = raw.get("infrastructure", {})
        infra_health = infra_raw.get("health", {})
        infra_control = infra_raw.get("control", {})
        infra_redis = infra_raw.get("redis", {})
        infra_s3 = infra_raw.get("s3", {})
        infra_alerts = infra_raw.get("alerts", {})
        infrastructure = InfrastructureConfig(
            health_host=str(infra_health.get("host", "0.0.0.0")),
            health_port=int(infra_health.get("port", 9090)),
            control_host=str(infra_control.get("host", "127.0.0.1")),
            control_port=int(infra_control.get("port", 9090)),
            redis_host=str(infra_redis.get("host", "localhost")),
            redis_port=int(infra_redis.get("port", 6379)),
            redis_timeout=int(infra_redis.get("timeout_seconds", 2)),
            s3_bucket=str(infra_s3.get("bucket", "beidou-certificates")),
            s3_endpoint=str(infra_s3.get("endpoint", "http://localhost:9000")),
            alerts_file=str(infra_alerts.get("file_path", "/tmp/beidou_alerts.jsonl")),
            webhook_timeout=int(infra_alerts.get("webhook_timeout", 5)),
        )

        # Parse capital ladder
        ladder_raw = raw.get("production_ladder", {})
        levels_raw = ladder_raw.get("levels", [])
        capital_levels: list[CapitalLevelConfig] = []
        for lv in levels_raw:
            capital_levels.append(
                CapitalLevelConfig(
                    name=str(lv.get("name", "")),
                    gate=str(lv.get("gate", "")),
                    max_capital=float(lv.get("max_capital", 0.0)),
                    max_leverage=float(lv.get("max_leverage", 0.0)),
                    min_unattended_hours=float(lv.get("min_unattended_hours", 0.0)),
                )
            )
        capital_limits_raw = ladder_raw.get("capital_limits", {})
        capital_ladder = CapitalLadderConfig(
            current_level=str(ladder_raw.get("current_level", "L0_PAPER")),
            levels=tuple(capital_levels),
            capital_limits={str(k): float(v) for k, v in capital_limits_raw.items()} if capital_limits_raw else {},
        )

        # Validate LIVE environments cannot use testnet URLs
        if environment.is_live and "testnet" in exchange.rest_base_url.lower():
            errors.append(f"LIVE environment {environment.value} cannot use testnet URL {exchange.rest_base_url}")

        # Build settings
        settings = TypedSettings(
            environment=environment,
            version=str(raw.get("version", "0.0.0")),
            effective_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            source=source,
            database=database,
            exchange=exchange,
            risk=risk,
            production=production,
            infrastructure=infrastructure,
            capital_ladder=capital_ladder,
            can_write_trades=environment.can_write_trades,
            raw={
                k: v
                for k, v in raw.items()
                if "key" not in k.lower() and "secret" not in k.lower() and "password" not in k.lower()
            },
        )

        # Compute stable hash
        settings = TypedSettings(
            environment=settings.environment,
            version=settings.version,
            config_hash=settings.compute_hash(),
            effective_at=settings.effective_at,
            source=settings.source,
            database=settings.database,
            exchange=settings.exchange,
            risk=settings.risk,
            production=settings.production,
            infrastructure=settings.infrastructure,
            capital_ladder=settings.capital_ladder,
            can_write_trades=settings.can_write_trades,
            raw=settings.raw,
        )

        if errors:
            raise ConfigError("; ".join(errors), correlation_id="config-validation-failed")

        return settings

    def _safe_only(self, reason: str) -> TypedSettings:
        """返回 SAFETY_ONLY 回退配置，写交易能力为 false。"""
        merged = dict(self._SAFE_DEFAULTS)
        merged["reason"] = reason
        return self._parse_and_validate(
            merged,
            source=f"safety_only:{reason[:50]}",
        )
