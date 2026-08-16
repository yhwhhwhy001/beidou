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
    assert len(ids) == 18, f"登记表数量变化需评审: {len(ids)}"
    for item in TESTNET_EXEMPTIONS:
        assert item.rationale.strip(), f"{item.exemption_id} 缺理由"
        assert item.reassessment_module.strip(), f"{item.exemption_id} 缺责任模块"
        assert item.risk_note.strip(), f"{item.exemption_id} 缺风险记录"


# 非安全门语义的 testnet 分支白名单（环境切换/配置/日志/循环周期等），
# 每一类必须注释理由；白名单增长需评审。
_ALLOWED_TESTNET_LINE_TOKENS = (
    "TESTNET-EXEMPT:",  # 标记行本身
    "EnvironmentMode.TESTNET",  # 环境枚举注册（非安全门）
    "KernelMode.TESTNET",  # 内核模式选择（非安全门）
    "FSTREAM_TESTNET_URL",  # 交易所 WebSocket 端点选择（传输配置）
    "[beidou-autopilot]",  # 启动打印
    "testnet 模式",  # 中文注释描述
    "len(active_symbols) > 10",  # 近线批量规模限制（性能，非安全门）
    "exchange API unavailable",  # 启动期长周期重试策略（存活策略，非风险放宽）
    "start_ws",  # WS 传输 testnet 标志透传
    "_nearline_interval",  # 时钟域周期（testnet 30s，性能参数）
    "_offline_interval",  # 时钟域周期（testnet 300s，性能参数）
    "is_testnet = self._env_mode.value",  # run() 启动段周期选择辅助变量
)


def test_testnet_branches_are_marked_or_allowlisted() -> None:
    """引擎内每个 testnet 条件分支必须在特赦标记 ±3 行内或处于白名单。

    对抗审查（M00-F10 反例 G）：登记测试只做双向一致无法发现遗漏特赦。
    该扫描要求每个 `"testnet"` 比较行都可溯源：要么靠近 TESTNET-EXEMPT
    标记（已登记），要么匹配白名单（经评审的非安全门语义）。
    """
    lines = ENGINE_SRC.splitlines()
    marked: set[int] = set()
    for i, line in enumerate(lines):
        if "TESTNET-EXEMPT:" in line:
            for j in range(max(0, i - 3), min(len(lines), i + 4)):
                marked.add(j)
    violations: list[int] = []
    for i, line in enumerate(lines):
        if '"testnet"' not in line or i in marked:
            continue
        if any(token in line for token in _ALLOWED_TESTNET_LINE_TOKENS):
            continue
        violations.append(i + 1)
    assert not violations, f"未登记/未豁免的 testnet 分支行: {violations}"


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
