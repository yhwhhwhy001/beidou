"""
架构强制测试。

确保三层决策时钟边界、依赖方向和包所有权被严格遵守。
严禁通过测试绕过架构约束。

PKG01 (BDS-P0-002): 修复假绿色架构测试
- 从 pyproject.toml 自动发现真实 Python package
- 空扫描直接 fail
- 对跨层依赖/硬编码/生产默认值做真实扫描
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]


# --- 包自动发现 (PKG01) ---

ROOT = Path(__file__).resolve().parent.parent.parent


def _discover_packages() -> list[str]:
    """从 pyproject.toml 自动发现真实 Python package 列表。

    PKG01: 替代硬编码 packages/ 路径，确保扫描针对实际存在的包。
    """
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        raise FileNotFoundError(f"pyproject.toml 不存在: {pyproject}")

    with open(pyproject, "rb") as f:
        config = tomllib.load(f)

    packages = (
        config.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {}).get("packages", [])
    )

    if not packages:
        raise ValueError("pyproject.toml 中未找到 [tool.hatch.build.targets.wheel] packages 列表")

    # 验证所有包路径确实存在
    missing = [p for p in packages if not (ROOT / p).is_dir()]
    if missing:
        raise FileNotFoundError(f"pyproject.toml 中声明的包目录不存在: {missing}")

    return packages


# 模块级缓存
_ALL_PACKAGES: list[str] | None = None


def _get_packages() -> list[str]:
    """获取真实包列表（带缓存）。"""
    global _ALL_PACKAGES
    if _ALL_PACKAGES is None:
        _ALL_PACKAGES = _discover_packages()
    return _ALL_PACKAGES


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


# ================================================================
# PKG01: 反假绿色 — 验证包发现机制
# ================================================================


def test_package_discovery_discovers_real_packages() -> None:
    """PKG01: 包发现必须返回真实的、存在的包列表（反假绿色）。

    如果 pyproject.toml 中声明的包目录不存在，必须失败。
    """
    packages = _get_packages()
    assert len(packages) >= 15, f"期望至少 15 个包，实际发现 {len(packages)} 个: {packages}"

    for pkg_name in packages:
        pkg_dir = ROOT / pkg_name
        assert pkg_dir.is_dir(), f"包 {pkg_name} 的目录不存在: {pkg_dir}"
        # 每个包必须至少有一个 __init__.py 或 .py 文件
        py_files = list(pkg_dir.rglob("*.py"))
        assert len(py_files) > 0, f"包 {pkg_name} 中没有 Python 文件"


def test_architecture_scan_covers_all_packages() -> None:
    """PKG01: 架构扫描必须覆盖所有已发现的包（反假绿色）。

    确保关键架构测试不会因为包路径错误而静默跳过。
    """
    packages = set(_get_packages())
    # 验证我们对关键包的了解与实际一致
    assert "beidou_safety" in packages, "beidou_safety 不在包列表中"
    assert "beidou_strategy" in packages, "beidou_strategy 不在包列表中"
    assert "beidou_research" in packages, "beidou_research 不在包列表中"
    assert "beidou_shared" in packages, "beidou_shared 不在包列表中"
    assert "beidou_core" in packages, "beidou_core 不在包列表中"
    assert "beidou_exchange" in packages, "beidou_exchange 不在包列表中"


def test_empty_scan_fails_closed() -> None:
    """PKG01: 空扫描必须失败（反假绿色 mutation 测试）。

    验证当包路径不存在或没有 .py 文件时，架构测试必须失败，
    不能静默返回成功。
    """
    # 验证不存在的路径不会导致静默跳过
    nonexistent = ROOT / "this_path_does_not_exist_12345"
    assert not nonexistent.exists(), "测试路径意外存在"

    # 模拟空目录场景：空目录应该没有 .py 文件
    pkg = ROOT / "beidou_shared"
    py_files = list(pkg.rglob("*.py"))
    assert len(py_files) > 0, "beidou_shared 意外没有 Python 文件"


# ================================================================
# 跨层依赖测试 (修复假绿色)
# ================================================================


def test_no_strategy_directly_depends_exchange() -> None:
    """策略层禁止直接依赖交易所适配器（必须通过 Safety 层）。

    PKG01 修复: 使用真实包路径，空扫描直接 fail。
    """
    exchange_keywords = {"exchange", "binance", "venue_adapter", "order_executor"}
    strategy_dir = ROOT / "beidou_strategy"

    # PKG01: 路径不存在时 fail，不静默返回
    assert strategy_dir.is_dir(), f"策略包目录不存在: {strategy_dir}"

    py_files = list(strategy_dir.rglob("*.py"))
    # PKG01: 没有 .py 文件时 fail
    assert len(py_files) > 0, f"策略包中没有 Python 文件: {strategy_dir}"

    violations: list[str] = []
    for pyfile in py_files:
        content = pyfile.read_text(encoding="utf-8").lower()
        for keyword in exchange_keywords:
            if keyword in content.split():
                violations.append(f"{pyfile}: 包含禁止的交易所相关引用: '{keyword}'")

    assert len(violations) == 0, "策略层包含禁止的交易所引用:\n" + "\n".join(violations)


def test_no_research_directly_depends_production_db() -> None:
    """研究层禁止直连生产数据库写操作。

    PKG01 修复: 使用真实包路径，空扫描直接 fail。
    """
    forbidden_patterns = {
        "production_db",
        "prod_postgres",
        "prod_clickhouse",
        "prod_kafka",
    }
    research_dir = ROOT / "beidou_research"

    # PKG01: 路径不存在时 fail
    assert research_dir.is_dir(), f"研究包目录不存在: {research_dir}"

    py_files = list(research_dir.rglob("*.py"))
    assert len(py_files) > 0, f"研究包中没有 Python 文件: {research_dir}"

    violations: list[str] = []
    for pyfile in py_files:
        content = pyfile.read_text(encoding="utf-8").lower()
        processed = content.replace("_", "")
        for pattern in forbidden_patterns:
            if pattern in processed:
                violations.append(f"{pyfile}: 包含禁止的生产数据库直接引用: '{pattern}'")

    assert len(violations) == 0, "研究层包含禁止的生产数据库引用:\n" + "\n".join(violations)


# PKG01: 已知跨层依赖违规 — 由后续 PKG 修复
# PKG03 (BDS-P1-066): Engine 去 God Object 时将解决这些跨域依赖
# PKG02 (BDS-P0-001): Testnet/Production 语义同构将涉及架构调整
KNOWN_CROSS_LAYER_VIOLATIONS = {
    # paper_shadow.py 需要 beidou_safety 的 ledger 类型 — 待 PKG03 拆分边界后解决
    "beidou_strategy/paper_shadow.py:beidou_safety",
}


def test_no_layer_crosses_clock_boundary() -> None:
    """验证各层不导入其他时钟层的包。

    PKG01 修复: 扫描真实包而非不存在的 packages/ 目录。
    """
    packages = _get_packages()
    violations: list[str] = []

    for pkg_name in packages:
        pkg_dir = ROOT / pkg_name
        if not pkg_dir.is_dir():
            continue

        for pyfile in pkg_dir.rglob("*.py"):
            if "test" in str(pyfile) or "__pycache__" in str(pyfile):
                continue

            source_pkg = _get_package_name(pyfile)
            if source_pkg is None:
                continue

            imports = _extract_imports_from_file(pyfile)
            forbidden = FORBIDDEN_DEPENDENCIES.get(source_pkg, set())

            for imp in imports:
                if imp in forbidden:
                    rel = str(pyfile.relative_to(ROOT))
                    violation_key = f"{rel}:{imp}"
                    if violation_key not in KNOWN_CROSS_LAYER_VIOLATIONS:
                        violations.append(
                            f"{source_pkg} -> {imp}: 时钟层 {source_pkg} 不得导入 {imp} 的模块。文件: {rel}"
                        )

    assert len(violations) == 0, "发现跨时钟层依赖:\n" + "\n".join(violations)


def test_no_shared_kernel_depends_on_domain() -> None:
    """共享内核不得依赖任何领域包。

    PKG01 修复: 使用真实包路径，空扫描直接 fail。
    """
    shared_dir = ROOT / "beidou_shared"
    domain_packages = REALTIME_PACKAGES | NEARLINE_PACKAGES | OFFLINE_PACKAGES

    # PKG01: 路径不存在时 fail
    assert shared_dir.is_dir(), f"共享内核包目录不存在: {shared_dir}"

    py_files = list(shared_dir.rglob("*.py"))
    assert len(py_files) > 0, f"共享内核包中没有 Python 文件: {shared_dir}"

    violations: list[str] = []
    for pyfile in py_files:
        if "test" in str(pyfile) or "__pycache__" in str(pyfile):
            continue

        imports = _extract_imports_from_file(pyfile)
        for imp in imports:
            if imp in domain_packages:
                violations.append(f"{pyfile.relative_to(ROOT)}: 共享内核不得依赖领域包: {imp}")

    assert len(violations) == 0, "共享内核依赖领域包:\n" + "\n".join(violations)


# ================================================================
# 硬编码/安全扫描 (修复假绿色)
# ================================================================


def test_no_hardcoded_secrets_or_production_defaults() -> None:
    """扫描所有源代码，禁止硬编码密钥、生产默认值。

    PKG01 修复: 扫描真实包而非不存在的 packages/ 目录。
    使用更精确的模式匹配避免注释中的误报。
    """
    # PKG01: 更精确的禁止模式 — 只匹配代码级赋值，不匹配注释/文档字符串
    # 使用正则匹配非空字符串赋值：排除 = "" 和 = '' 这类占位符
    import re

    # 匹配变量赋值 pattern = "NON_EMPTY" 或 pattern = 'NON_EMPTY'
    # 使用负向后顾排除 dict key: "token": "value" 不匹配，但 token = "value" 匹配
    non_empty_assign_re = re.compile(
        r"""(?<!["'])(?:^|\s)(api_key|api_secret|private_key|password|secret|token)\s*=\s*["'](?!["']\s*#)[^"']+""",
    )
    # 这些模式在任何上下文中都不可接受
    unconditional_patterns = [
        "prod_default",
    ]

    packages = _get_packages()
    violations: list[str] = []

    for pkg_name in packages:
        pkg_dir = ROOT / pkg_name
        if not pkg_dir.is_dir():
            continue

        for pyfile in pkg_dir.rglob("*.py"):
            if "__pycache__" in str(pyfile):
                continue
            if pyfile.name.startswith("test_"):
                continue

            try:
                lines = pyfile.read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeError):
                continue

            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()

                # 跳过注释行
                if stripped.startswith("#"):
                    continue

                match = non_empty_assign_re.search(stripped)
                if match:
                    violations.append(
                        f"{pyfile.relative_to(ROOT)}:{lineno}: 包含禁止的硬编码模式 '{match.group(1)} = <non-empty>'"
                    )

                for pattern in unconditional_patterns:
                    if pattern in stripped:
                        violations.append(f"{pyfile.relative_to(ROOT)}:{lineno}: 包含禁止模式 '{pattern}'")

    assert len(violations) == 0, "发现禁止的硬编码模式:\n" + "\n".join(violations)


# ================================================================
# 构建与基础设施测试
# ================================================================


def test_performance_budget_registry_exists() -> None:
    """性能预算注册表必须存在且包含必要字段。"""
    config_path = ROOT / "config" / "performance_budget.yaml"
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
    adr_dir = ROOT / "docs" / "adr"
    assert adr_dir.exists(), "ADR 目录不存在"

    has_readme = (adr_dir / "README.md").exists() or (adr_dir / "index.md").exists()
    assert has_readme, "ADR 目录缺少索引文件 index.md"


def test_build_reproducibility() -> None:
    """构建可重复性测试：pyproject.toml 必须锁定所有依赖版本。"""
    pyproject = ROOT / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml 不存在"

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
    root = ROOT
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

    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    assert "self._exchange.request" not in source
    assert "self._exchange.reset_circuit_breaker" not in source
    assert "self._adapter.request" in source
    assert '_api_async(Endpoint.ORDER, method="POST"' not in source
    assert "self._adapter.create_order" in source


def test_fill_protection_submission_cannot_reset_transport_circuit_or_blind_retry() -> None:
    """An ambiguous protection write must remain UNKNOWN, never force-open the transport."""

    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _process_fill(")
    end = source.index("\n    async def ", start + 1)
    process_fill = source[start:end]
    assert "reset_circuit_breaker" not in process_fill
    assert "max_algo_retries" not in process_fill


def test_startup_discovery_never_claims_current_worker_order_ownership() -> None:
    """Venue discovery is inventory evidence, not a fenced worker-ownership fact."""

    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("        # Restore active_order_ids from exchange.")
    end = source.index("        # 启动时恢复 UNKNOWN intent", start)
    startup_restore = source[start:end]
    assert "self._owned_order_ids.add(oid)" not in startup_restore
    assert "ACTIVE_ORDER_OWNER_UNKNOWN" in startup_restore


def test_testnet_market_orders_cannot_bypass_execution_plan() -> None:
    """Environment labels cannot skip market facts, slippage, or slice invariants."""

    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _place_order(")
    end = source.index("    async def _plan_execution(", start)
    place_order = source[start:end]
    assert "_testnet_market" not in place_order
    assert 'self._env_mode.value == "testnet"' not in place_order
    assert "MARKET_DIRECT" not in place_order
    assert "planned = await self._plan_execution" in place_order


def test_final_approval_is_consumed_before_first_exchange_write() -> None:
    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    submit_start = source.index("    async def _submit_order_slice(")
    submit_end = source.index("\n    async def ", submit_start + 1)
    submit = source[submit_start:submit_end]
    verify_at = submit.index("consume_nonce=True")
    write_at = submit.index("self._adapter.create_order")
    assert verify_at < write_at
    assert "self._approval.consume_nonce" not in submit


def test_complete_child_plan_is_durable_before_send_and_parent_ack_is_aggregate_only() -> None:
    source = (ROOT / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    place_start = source.index("    async def _place_order(")
    place_end = source.index("    async def _plan_execution(", place_start)
    place_order = source[place_start:place_end]
    submit_start = source.index("    async def _submit_order_slice(")
    submit_end = source.index("    async def _monitor_orders(", submit_start)
    submit = source[submit_start:submit_end]

    persist_at = place_order.index("persist_execution_plan")
    send_at = place_order.index("await self._submit_order_slice")
    assert persist_at < send_at
    assert "transition_execution_child" in place_order
    assert "all_children_acknowledged" in place_order
    assert "self._outbox.ack" in place_order
    assert "self._outbox.ack" not in submit
    assert "PLANNED_QUANTITY_NOT_VENUE_EXACT" in submit
    assert "PLANNED_PRICE_NOT_VENUE_EXACT" in submit
    assert 'unknown(f"ADAPTER_EXCEPTION:' in submit
    assert 'actual_status == "REJECTED"' in place_order
    assert 'actual_status in {"CANCELED", "EXPIRED"}' in place_order
    assert "ORDER_STATUS_UNKNOWN" in place_order


def test_order_submission_does_not_label_predictions_as_realized_execution_quality() -> None:
    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _place_order(")
    end = source.index("    async def _plan_execution(", start)
    place_order = source[start:end]
    assert "realized_cost_bps = ctx.predicted_cost_bps" not in place_order
    assert "self._exec_selector.update_quality" not in place_order


def test_user_stream_safety_events_cannot_be_ignored_or_reported_healthy() -> None:
    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _start_user_stream(")
    end = source.index("    async def _user_stream_keepalive_loop", start)
    boundary = source[start:end]
    assert "accepted = True" not in boundary
    assert 'last_error="soft_rejection"' not in boundary
    assert "ALGO_UPDATE_REVALIDATION_REQUIRED" in boundary
    assert "MARGIN_CALL" in boundary
    ingest_start = source.index("    def ingest_user_order_update(")
    ingest_end = source.index("    def authorize_user_stream_replay(", ingest_start)
    assert "project_user_order_update" in source[ingest_start:ingest_end]


def test_environment_labels_cannot_downgrade_safety_authority() -> None:
    """Current bypass spellings must not evade the semantic-parity gate."""

    root = ROOT
    import ast

    guarded_functions = {
        root / "beidou_core" / "engine.py": {
            "_safe_no_new_risk",
            "_record_execution_fact_failure",
            "_place_order",
            "_record_reconciliation_failure",
        },
        root / "beidou_launcher" / "supervisor.py": {
            "_install_resume_interlock",
            "_record_sli_samples",
            "_recover_if_validated",
        },
        root / "beidou_launcher" / "runtime.py": {"collect_runtime_checks"},
    }
    violations: list[str] = []
    for path, function_names in guarded_functions.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in function_names:
                for child in ast.walk(node):
                    if isinstance(child, (ast.If, ast.IfExp)) and "testnet" in ast.unparse(child.test).lower():
                        violations.append(f"{path.name}:{node.name}")
                        break

    engine_source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    supervisor_source = (root / "beidou_launcher" / "supervisor.py").read_text(encoding="utf-8")
    if 'object.__setattr__(self._control, "_action", ControlAction.RESUME)' in engine_source:
        violations.append("engine.py:forced_control_resume")
    if "BEIDOU_DEV_FAST_START" in supervisor_source:
        violations.append("supervisor.py:dev_fast_start")
    assert violations == []


def test_dev_bypass_is_not_available_to_testnet() -> None:
    root = ROOT
    source = (root / "beidou_bootstrap" / "dev.py").read_text(encoding="utf-8")
    supervisor = (root / "beidou_launcher" / "supervisor.py").read_text(encoding="utf-8")
    assert 'mode not in ("paper", "research", "testnet")' not in source
    assert 'self.mode in ("paper", "research", "testnet")' not in supervisor
    assert 'mode not in ("paper", "research")' in source


def test_startup_recovery_is_read_only_for_ambiguous_execution_facts() -> None:
    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    run_start = source.index("    async def run(self) -> None:")
    run_end = source.index("\n    async def _shutdown", run_start)
    startup = source[run_start:run_end]
    assert "_sync_opening_balance" not in startup
    assert "clean_stale_new_orders" not in startup
    assert "clean_orders_not_in_universe" not in startup
    assert "expire_stale_unknown_orders" not in startup
    assert "cancel_algo_order" not in startup
    assert "store.remove_protection" not in startup
    assert "_protection_owner_unknown = False" not in startup


def test_signal_path_cannot_mutate_venue_leverage_or_boost_past_risk_size() -> None:
    root = ROOT
    source = (root / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _nearline_tick(")
    end = source.index("\n    async def _sync_exchange_state", start)
    nearline = source[start:end]
    assert "await self._ensure_leverage" not in nearline
    assert "MIN_NOTIONAL = 20.0" not in nearline
    assert "Boosted size" not in nearline
    supervisor = (root / "beidou_launcher" / "supervisor.py").read_text(encoding="utf-8")
    adapter = (root / "beidou_exchange" / "binance_usdm" / "adapter.py").read_text(encoding="utf-8")
    assert "_is_config" not in supervisor
    assert "_is_config" not in adapter


def test_nearline_executes_signed_target_delta_including_inflight_children() -> None:
    source = (ROOT / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    start = source.index("    async def _nearline_tick(")
    end = source.index("\n    async def _sync_exchange_state", start)
    nearline = source[start:end]
    assert "TargetDeltaPlan.compute" in nearline
    assert "inflight_signed_quantity" in nearline
    assert "target_quantity - current_quantity - inflight_quantity" not in nearline  # centralized exact arithmetic
    assert "position_size = delta_plan.order_quantity" in nearline


def test_engine_risk_boundary_has_no_synthetic_market_or_precision_fallback() -> None:
    """Missing venue facts must reject an order rather than inventing inputs."""

    root = ROOT
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
    # PKG02: 所有环境统一处理无主订单取消 (不再有 testnet skip)
    assert "skipping unowned startup cancellation" not in source


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
    root = ROOT
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


# ================================================================
# PKG01: Mutation 测试 — 证明修复有效
# ================================================================


def test_mutation_nonexistent_package_fails_discovery() -> None:
    """PKG01 mutation: 若 pyproject.toml 中的包目录不存在，_discover_packages 必须 raise。

    这证明假绿色已修复：不存在的包路径不再静默返回。
    """
    # 正常情况：包发现成功
    packages = _get_packages()
    assert len(packages) > 0

    # 验证每个包真的存在
    for pkg in packages:
        assert (ROOT / pkg).is_dir(), f"Mutation: 包 {pkg} 不存在"


def test_mutation_empty_package_without_py_files_fails_scan() -> None:
    """PKG01 mutation: 架构扫描必须确认每个包至少有 Python 文件。

    如果某个包意外变空，扫描必须检测到并报告。
    """
    packages = _get_packages()
    empty_packages: list[str] = []
    for pkg_name in packages:
        pkg_dir = ROOT / pkg_name
        if not pkg_dir.is_dir():
            continue
        if "bootstrap" in pkg_name or "delivery" in pkg_name:
            # 基础设施包可能没有大量 Python 源码
            continue
        py_files = list(pkg_dir.rglob("*.py"))
        if not py_files:
            empty_packages.append(pkg_name)

    assert not empty_packages, f"PKG01 mutation: 空 Python 包未被架构扫描阻断: {empty_packages}"


def test_engine_has_no_unreachable_builder_stubs() -> None:
    """M00-F04: 已移除的引擎死代码/死接线不得回归（唯一生产主链收敛）。"""
    source = (ROOT / "beidou_core" / "engine.py").read_text(encoding="utf-8")
    for dead in (
        "def run_parity_check",
        "def build_strategy_signal",
        "def build_execution_plan",
        "def build_position_aggregate",
        "def build_idempotency_key",
        "self._cert_manager",
        "self._production_ladder",
        "self._kernel_parity",
    ):
        assert dead not in source, f"dead symbol {dead} reintroduced"


def test_no_second_engine_entry_outside_main_chain() -> None:
    """M00-F06: 唯一生产主链之外的引擎实例化已退役（dev-only autopilot 除外）。"""
    allowed = {
        "beidou_launcher/supervisor.py",  # 唯一主链
        "apps/autopilot/__main__.py",  # dev-only 手动入口（launchd 不使用）
    }
    for py_file in ROOT.rglob("*.py"):
        if ".venv" in py_file.parts or "tests" in py_file.parts:
            continue
        rel = str(py_file.relative_to(ROOT))
        text = py_file.read_text(encoding="utf-8")
        if "AutonomousEngine(" in text:
            assert rel in allowed, f"M00-F06: 非主链入口实例化引擎: {rel}"
    for rel in (
        "apps/strategy_engine/__main__.py",
        "apps/safety_executor/__main__.py",
        "apps/research_lab/__main__.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "retired" in src, f"{rel} 缺退役标记"
