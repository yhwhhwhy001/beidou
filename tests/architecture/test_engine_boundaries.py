"""
PKG03 (BDS-P1-066): Engine 域边界强制执行测试。

验证：
1. Domain ports 已定义且可被独立测试
2. 禁止跨域读取私有字段（Engine 私有属性访问检测）
3. 域 Authority 注册表行为正确
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


# ================================================================
# Domain Ports 验证
# ================================================================


def test_domain_ports_module_exists() -> None:
    """PKG03: beidou_core/ports.py 存在且可导入。"""
    ports_file = ROOT / "beidou_core" / "ports.py"
    assert ports_file.exists(), "beidou_core/ports.py 不存在"

    from beidou_core.ports import DOMAINS

    assert len(DOMAINS) == 8, f"期望 8 个域，实际 {len(DOMAINS)} 个"
    assert "Risk" in DOMAINS
    assert "Execution" in DOMAINS
    assert "Ledger" in DOMAINS
    assert "Protection" in DOMAINS


def test_domain_authority_registry_single_writer() -> None:
    """PKG03: 每个域只能有一个 Authority（单写原则）。"""
    from beidou_core.ports import DomainAuthorityRegistry

    registry = DomainAuthorityRegistry()

    class MockRiskAuthority:
        pass

    class AnotherRiskAuthority:
        pass

    registry.register_authority("Risk", MockRiskAuthority())

    with pytest.raises(ValueError, match="已注册"):
        registry.register_authority("Risk", AnotherRiskAuthority())


def test_domain_authority_registry_read_models() -> None:
    """PKG03: 只读消费者可以多个。"""
    from beidou_core.ports import DomainAuthorityRegistry

    registry = DomainAuthorityRegistry()
    registry.register_read_model("Market", "monitor_1")
    registry.register_read_model("Market", "monitor_2")

    assert len(registry.registered_domains) == 0  # 只读不注册域


def test_fact_models_are_immutable() -> None:
    """PKG03: 核心事实模型是不可变的 frozen dataclass。"""
    from beidou_core.ports import (
        MarketFact,
        RiskDecision,
        StrategyProposal,
    )

    # 所有核心事实都是 frozen
    fact = MarketFact(symbol="BTCUSDT", price=50000.0, timestamp=__import__("datetime").datetime.utcnow())
    with pytest.raises(Exception):
        fact.price = 60000.0  # type: ignore[misc]

    risk = RiskDecision(
        level="NORMAL",
        reason="ok",
        generation=1,
        snapshot_hash="abc",
        timestamp=__import__("datetime").datetime.utcnow(),
    )
    with pytest.raises(Exception):
        risk.level = "LOCKED"  # type: ignore[misc]

    proposal = StrategyProposal(
        strategy_id=__import__("beidou_shared.types", fromlist=["StrategyId"]).StrategyId("test"),
        symbol="BTCUSDT",
        direction="LONG",
        target_pct=0.5,
        confidence=0.8,
    )
    with pytest.raises(Exception):
        proposal.target_pct = 1.0  # type: ignore[misc]


# ================================================================
# 跨域私有字段访问检测
# ================================================================


# PKG03: 已知的跨域私有字段访问（待后续阶段修复）
KNOWN_PRIVATE_ACCESS = {
    # Monitoring 读取 Engine 私有字段 — 待 S5 修复
    "beidou_observability/monitoring/__init__.py",
    "beidou_observability/monitoring/checks/protection.py",
    "beidou_observability/monitoring/checks/execution.py",
    "beidou_observability/monitoring/checks/account.py",
}

# 不应读取 Engine 私有字段的模块（除已知违规外）
PROTECTED_MODULES = {
    "beidou_observability",
    "beidou_strategy",
    "beidou_research",
}


def test_no_cross_domain_private_field_access() -> None:
    """PKG03: 非核心模块不得读取 Engine 私有字段。

    检测模式：`engine._xxx` 或 `self._engine._xxx`。
    已知违规记录在 KNOWN_PRIVATE_ACCESS 中，由后续 PKG 修复。
    """
    violations: list[str] = []

    for pkg_name in PROTECTED_MODULES:
        pkg_dir = ROOT / pkg_name
        if not pkg_dir.is_dir():
            continue

        for pyfile in pkg_dir.rglob("*.py"):
            rel = str(pyfile.relative_to(ROOT))
            if rel in KNOWN_PRIVATE_ACCESS:
                continue
            if pyfile.name.startswith("test_"):
                continue

            try:
                content = pyfile.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue

            # 检测 _engine._xxx 或 engine._xxx 模式（私有字段访问）
            lines = content.split("\n")
            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 检测私有属性访问
                if "._engine." in stripped or "engine._" in stripped:
                    if "test" not in rel.lower():
                        violations.append(f"{rel}:{lineno}: {stripped.strip()[:80]}")

    # 只报告新增违规（不在已知列表中）
    new_violations = [v for v in violations if not any(k in v for k in KNOWN_PRIVATE_ACCESS)]

    if new_violations:
        raise AssertionError("跨域私有字段访问检测到新增违规:\n" + "\n".join(new_violations))


def test_known_private_access_list_is_up_to_date() -> None:
    """PKG03: 已知违规列表应保持准确（防退化）。"""
    # 验证已知违规确实存在
    existing = []
    for path in KNOWN_PRIVATE_ACCESS:
        full = ROOT / path
        if full.exists():
            existing.append(path)

    # 所有已知违规条目应对应真实文件
    missing = set(KNOWN_PRIVATE_ACCESS) - set(existing)
    assert not missing, f"KNOWN_PRIVATE_ACCESS 包含不存在的文件: {missing}"


# ================================================================
# Mutation 测试
# ================================================================


def test_mutation_duplicate_authority_registration_blocked() -> None:
    """PKG03 mutation: 重复注册 Authority 必须被阻止。"""
    from beidou_core.ports import DomainAuthorityRegistry

    registry = DomainAuthorityRegistry()

    class A:
        pass

    class B:
        pass

    registry.register_authority("Ledger", A())
    try:
        registry.register_authority("Ledger", B())
        pytest.fail("Mutation: 重复注册 Ledger Authority 未被阻止")
    except ValueError:
        pass  # 预期行为


def test_mutation_immutable_fact_modification_blocked() -> None:
    """PKG03 mutation: 修改不可变事实必须失败。"""
    from beidou_core.ports import OrderIntent

    intent = OrderIntent(
        intent_id="test-001",
        symbol="BTCUSDT",
        side="BUY",
        quantity=1.0,
    )
    # dataclass frozen 保证不可变性
    with pytest.raises(Exception):
        intent.side = "SELL"  # type: ignore[misc]
