"""testnet 特赦登记表完整性测试（M00-F05）。"""

from __future__ import annotations

from pathlib import Path

from beidou_launcher.testnet_exemptions import TESTNET_EXEMPTIONS

ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_SRC = (ROOT / "beidou_core" / "engine.py").read_text(encoding="utf-8")


def test_exemption_registry_entries_are_complete() -> None:
    """每项特赦必须有唯一 id、非空理由与责任模块。"""
    ids = [item.exemption_id for item in TESTNET_EXEMPTIONS]
    assert len(ids) == len(set(ids)), f"重复 exemption_id: {ids}"
    assert len(ids) == 8, f"登记表数量变化需评审: {len(ids)}"
    for item in TESTNET_EXEMPTIONS:
        assert item.rationale.strip(), f"{item.exemption_id} 缺理由"
        assert item.reassessment_module.strip(), f"{item.exemption_id} 缺责任模块"
        assert item.risk_note.strip(), f"{item.exemption_id} 缺风险记录"


def test_every_registered_exemption_has_code_marker() -> None:
    """登记表中的每项都必须在引擎代码有对应标记（登记与代码一致）。"""
    for item in TESTNET_EXEMPTIONS:
        assert item.marker in ENGINE_SRC, f"{item.exemption_id} 在 engine.py 无标记"


def test_every_code_marker_is_registered() -> None:
    """引擎中的每个 TESTNET-EXEMPT 标记都必须已登记（防未治理新特赦）。"""
    import re

    registered = {item.marker for item in TESTNET_EXEMPTIONS}
    for match in re.finditer(r"TESTNET-EXEMPT:\s*(EXEMPT-\d+)", ENGINE_SRC):
        marker = f"TESTNET-EXEMPT: {match.group(1)}"
        assert marker in registered, f"未登记的特赦标记: {marker}"
