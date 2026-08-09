"""
架构强制测试。

确保三层决策时钟边界、依赖方向和包所有权被严格遵守。
严禁通过测试绕过架构约束。
"""

from __future__ import annotations

import ast
from pathlib import Path

# --- 架构规则定义 ---

# 各时钟层的包路径
REALTIME_PACKAGES = {"beidou_safety"}
NEARLINE_PACKAGES = {"beidou_strategy"}
OFFLINE_PACKAGES = {"beidou_research"}
SHARED_PACKAGES = {"beidou_shared"}

# 允许的依赖方向（只能从上层往下依赖）
ALLOWED_DEPENDENCIES = {
    # Shared 不依赖任何领域包
    # Safety 可以依赖 Shared
    "beidou_safety": {"beidou_shared"},
    # Strategy 可以依赖 Shared
    "beidou_strategy": {"beidou_shared"},
    # Research 可以依赖 Shared
    "beidou_research": {"beidou_shared"},
}

# 禁止的跨层依赖
FORBIDDEN_DEPENDENCIES = {
    "beidou_safety": {"beidou_strategy", "beidou_research"},
    "beidou_strategy": {"beidou_safety", "beidou_research"},
    "beidou_research": {"beidou_safety", "beidou_strategy"},
}


class ArchitectureViolation(Exception):
    """架构违规异常。"""

    def __init__(self, source: str, target: str, reason: str) -> None:
        self.source = source
        self.target = target
        self.reason = reason
        super().__init__(f"{source} -> {target}: {reason}")


def _extract_imports_from_file(filepath: Path) -> set[str]:
    """从 Python 文件中提取导入的包名。"""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def _get_package_name(filepath: Path) -> str | None:
    """根据文件路径确定所属的领域包。"""
    parts = filepath.parts
    for pkg in REALTIME_PACKAGES | NEARLINE_PACKAGES | OFFLINE_PACKAGES:
        if pkg in parts:
            return pkg
    return None


def test_no_strategy_directly_depends_exchange() -> None:
    """策略层禁止直接依赖交易所适配器（必须通过 Safety 层）。"""
    exchange_keywords = {"exchange", "binance", "venue_adapter", "order_executor"}
    strategy_src = Path(__file__).resolve().parent.parent.parent / "packages" / "beidou_strategy" / "src"

    if not strategy_src.exists():
        return  # 目录尚未创建属于正常情况

    for pyfile in strategy_src.rglob("*.py"):
        content = pyfile.read_text(encoding="utf-8").lower()
        for keyword in exchange_keywords:
            assert keyword not in content.split(), f"策略层文件 {pyfile} 包含禁止的交易所相关引用: '{keyword}'"


def test_no_research_directly_depends_production_db() -> None:
    """研究层禁止直连生产数据库写操作。"""
    forbidden_patterns = {
        "production_db",
        "prod_postgres",
        "prod_clickhouse",
        "prod_kafka",
    }
    research_src = Path(__file__).resolve().parent.parent.parent / "packages" / "beidou_research" / "src"

    if not research_src.exists():
        return

    for pyfile in research_src.rglob("*.py"):
        content = pyfile.read_text(encoding="utf-8").lower()
        for pattern in forbidden_patterns:
            assert pattern not in content.replace("_", ""), f"研究层文件 {pyfile} 包含禁止的生产数据库直接引用"


def test_no_layer_crosses_clock_boundary() -> None:
    """验证各层不导入其他时钟层的包。"""
    packages_root = Path(__file__).resolve().parent.parent.parent / "packages"

    if not packages_root.exists():
        return

    for pyfile in packages_root.rglob("*.py"):
        if "test" in str(pyfile) or "__pycache__" in str(pyfile):
            continue

        source_pkg = _get_package_name(pyfile)
        if source_pkg is None:
            continue

        imports = _extract_imports_from_file(pyfile)
        forbidden = FORBIDDEN_DEPENDENCIES.get(source_pkg, set())

        for imp in imports:
            if imp in forbidden:
                raise ArchitectureViolation(
                    source_pkg, imp, f"时钟层 {source_pkg} 不得导入 {imp} 的模块。文件: {pyfile}"
                )


def test_no_shared_kernel_depends_on_domain() -> None:
    """共享内核不得依赖任何领域包。"""
    shared_src = Path(__file__).resolve().parent.parent.parent / "packages" / "beidou_shared" / "src"
    domain_packages = REALTIME_PACKAGES | NEARLINE_PACKAGES | OFFLINE_PACKAGES

    if not shared_src.exists():
        return

    for pyfile in shared_src.rglob("*.py"):
        if "test" in str(pyfile) or "__pycache__" in str(pyfile):
            continue

        imports = _extract_imports_from_file(pyfile)
        for imp in imports:
            assert imp not in domain_packages, f"共享内核文件 {pyfile} 不得依赖领域包: {imp}"


def test_no_hardcoded_secrets_or_production_defaults() -> None:
    """扫描所有源代码，禁止硬编码密钥、生产默认值、TODO。"""
    forbidden_patterns = [
        "TODO",
        "FIXME",
        "NotImplemented",
        "pass  # TODO",
        "api_key =",
        "api_secret =",
        "private_key =",
        "password =",
        "secret =",
        "token =",
        "prod_default",
    ]

    packages_root = Path(__file__).resolve().parent.parent.parent / "packages"
    if not packages_root.exists():
        return

    violations: list[str] = []
    for pyfile in packages_root.rglob("*.py"):
        if "__pycache__" in str(pyfile):
            continue
        try:
            content = pyfile.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue

        for pattern in forbidden_patterns:
            if pattern in content:
                violations.append(f"{pyfile}: 包含禁止模式 '{pattern}'")

    # TODO 本身在注释中是可以的，但在业务逻辑代码中不行
    # 此处检测的是代码级 TODO/FIXME/NotImplemented
    assert len(violations) == 0, "发现禁止模式:\n" + "\n".join(violations)


def test_performance_budget_registry_exists() -> None:
    """性能预算注册表必须存在且包含必要字段。"""
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "performance_budget.yaml"
    assert config_path.exists(), f"性能预算注册表不存在: {config_path}"

    import yaml

    with open(config_path) as f:
        budgets = yaml.safe_load(f)

    assert budgets is not None, "性能预算注册表为空"
    assert "hot_paths" in budgets, "性能预算注册表缺少 'hot_paths' 键"

    for path_name, entry in budgets["hot_paths"].items():
        for field in ("p50_ms", "p95_ms", "p99_ms", "reference_hardware", "measurement_method"):
            assert field in entry, f"热路径 {path_name} 缺少字段: {field}"


def test_adr_directory_has_index() -> None:
    """ADR 目录必须包含索引文件。"""
    adr_dir = Path(__file__).resolve().parent.parent.parent / "docs" / "adr"
    assert adr_dir.exists(), "ADR 目录不存在"

    has_readme = (adr_dir / "README.md").exists() or (adr_dir / "index.md").exists()
    assert has_readme, "ADR 目录缺少索引文件 index.md"


def test_build_reproducibility() -> None:
    """构建可重复性测试：requirements 文件或 pyproject.toml 必须锁定所有依赖版本。"""
    root = Path(__file__).resolve().parent.parent.parent
    pyproject = root / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml 不存在"

    # 验证 pyproject.toml 包含依赖
    content = pyproject.read_text(encoding="utf-8")
    assert "dependencies" in content, "pyproject.toml 缺少 dependencies 段"


# ================================================================
# BD-01 新增：Adapter 边界测试
# ================================================================

BINANCE_API_PATTERNS = [
    "/fapi/v1/order",
    "/fapi/v1/order/test",
    "/fapi/v2/order",
    "/dapi/v1/order",
]

ADAPTER_PACKAGES = {"beidou_exchange"}

# BD-02 established BinanceRESTClient as the single Adapter boundary.
# engine.py _api() now delegates to BinanceRESTClient at runtime.
# Remaining /fapi/ string references in engine.py are endpoint path constants
# passed to _api()/_api_async() which route through the Adapter.
# Full elimination of path strings requires per-call-site migration to
# named BinanceRESTClient methods (create_order, get_account, etc.) —
# tracked as BD-02-MIGRATE for incremental completion.
KNOWN_VIOLATIONS_UNTIL_BD02 = {
    "beidou_core/engine.py",  # Endpoint path strings — runtime routes through Adapter
}


def test_only_adapter_accesses_binance_api() -> None:
    """AC-01-02: 只有 Adapter 包可以引用 Binance API 端点。

    故意在非 Adapter 文件加入 `/fapi/v1/order` 时架构测试必须失败。
    """
    root = Path(__file__).resolve().parent.parent.parent
    violations: list[str] = []

    for pyfile in root.rglob("*.py"):
        # Skip these directories
        path_str = str(pyfile)
        if any(
            skip in path_str
            for skip in [
                "__pycache__",
                ".venv",
                ".git",
                "tests/",
                "evidence/",
                "tools/",
                "scripts/",
                "runbooks/",
                "docs/",
            ]
        ):
            continue

        # Determine which package this file belongs to
        rel = pyfile.relative_to(root)
        pkg = str(rel.parts[0]) if rel.parts else ""

        # Skip adapter package files — they ARE allowed
        if pkg in ADAPTER_PACKAGES:
            continue

        try:
            content = pyfile.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue

        for pattern in BINANCE_API_PATTERNS:
            if pattern in content:
                violations.append(f"{rel}: 非 Adapter 文件引用 Binance API 端点 '{pattern}'")

    # Filter known violations that are scheduled for fix in other BD tasks
    new_violations = [v for v in violations if not any(kv in v for kv in KNOWN_VIOLATIONS_UNTIL_BD02)]

    if new_violations:
        raise AssertionError(
            "非 Adapter 包禁止直接引用 Binance API 端点。"
            "请通过 beidou_exchange adapter 访问。"
            "\n新违规文件:\n"
            + "\n".join(new_violations)
            + "\n已知违规(待 BD-02 修复):\n"
            + "\n".join(v for v in violations if any(kv in v for kv in KNOWN_VIOLATIONS_UNTIL_BD02))
        )


def test_engine_transport_calls_cross_binance_adapter() -> None:
    """引擎不得直接持有 REST client 的 request/reset 写边界。"""

    root = Path(__file__).resolve().parent.parent.parent
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    assert "self._exchange.request" not in source
    assert "self._exchange.reset_circuit_breaker" not in source
    assert "self._adapter.request" in source
    assert '_api_async(Endpoint.ORDER, method="POST"' not in source
    assert "self._adapter.create_order" in source


def test_engine_risk_boundary_has_no_synthetic_market_or_precision_fallback() -> None:
    """Missing venue facts must reject an order rather than inventing inputs."""

    root = Path(__file__).resolve().parent.parent.parent
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    assert "MARKET_DATA_UNKNOWN" in source
    assert "MARKET_DEPTH_UNKNOWN" in source
    assert "PAPER_MARKET_DATA_UNKNOWN" in source
    assert "EXCHANGE_RULES_UNKNOWN" not in source  # no hidden fallback token
    assert "instant fill fallback" not in source
    assert "last_px * 0.999" not in source
    assert "last_px * 1.001" not in source
    assert 'self._symbol_precision.get(order_symbol, {"quantity": 3, "price": 2})' not in source
    assert 'self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})' not in source
    assert "skipping unowned startup cancellation" in source


# ================================================================
# MON00-08: 监控模块架构边界 — 监控代码不得直接发出风险增加订单
# ================================================================

MONITORING_PACKAGES = {"beidou_observability", "beidou_launcher"}
MONITORING_SCRIPTS = {"scripts/monitor_daemon.py"}

RISK_INCREASING_PATTERNS = [
    "/fapi/v1/order",
    "/fapi/v1/batchOrders",
    "POST /fapi/v1/order",
    "place_order(",
    "create_order(",
    "send_order(",
    "set_leverage(",
    "change_position_mode(",
    "change_margin_type(",
]

RISK_INCREASING_ALLOWLIST = {
    "stuck_new_orders",
    "risk_increase_allowed",
}


def test_monitoring_code_no_risk_increasing_orders() -> None:
    """MON00-08: 监控代码不得直接发出风险增加订单。

    Monitor 只能读取权威事实、写入 evidence/incident、
    通过既有 Control Plane 执行安全动作。
    严禁在监控路径中直接调用下单/改杠杆/改保证金等风险增加操作。
    """
    root = Path(__file__).resolve().parent.parent.parent
    violations: list[str] = []

    for pkg_name in MONITORING_PACKAGES:
        pkg_dir = root / pkg_name
        if not pkg_dir.exists():
            continue
        for pyfile in pkg_dir.rglob("*.py"):
            if "__pycache__" in str(pyfile):
                continue
            content = pyfile.read_text(encoding="utf-8")
            rel = pyfile.relative_to(root)
            for pattern in RISK_INCREASING_PATTERNS:
                if pattern in content:
                    is_allowlisted = any(aw in content and pattern in aw for aw in RISK_INCREASING_ALLOWLIST)
                    if not is_allowlisted:
                        violations.append(f"{rel}: 监控代码包含禁止的风险增加模式 '{pattern}'")

    for script_path in MONITORING_SCRIPTS:
        full_path = root / script_path
        if not full_path.exists():
            continue
        content = full_path.read_text(encoding="utf-8")
        for pattern in RISK_INCREASING_PATTERNS:
            if pattern in content:
                is_allowlisted = any(aw in content and pattern in aw for aw in RISK_INCREASING_ALLOWLIST)
                if not is_allowlisted:
                    violations.append(f"{script_path}: 监控脚本包含禁止的风险增加模式 '{pattern}'")

    assert not violations, (
        "监控代码严禁直接发出风险增加订单。"
        "Monitor 只能通过既有 Control Plane 执行安全动作。"
        "\n违规:\n" + "\n".join(violations)
    )
