"""Small behavior checks for registry-only production contracts."""

from __future__ import annotations


def test_every_registered_testnet_exemption_has_a_stable_marker() -> None:
    from beidou_launcher.testnet_exemptions import TESTNET_EXEMPTIONS

    assert len(TESTNET_EXEMPTIONS) == 22
    ids = {item.exemption_id for item in TESTNET_EXEMPTIONS}
    assert ids == {f"EXEMPT-{index:02d}" for index in range(1, 23)}
    for item in TESTNET_EXEMPTIONS:
        assert item.marker == f"TESTNET-EXEMPT: {item.exemption_id}"
        assert item.title and item.rationale and item.reassessment_module and item.risk_note
