"""When the fingerprint's own definition grows, that is not the book changing (2026-09-07).

Restart #5 recorded a new construction digest while the book was byte-identical.  The cause was the P22
merge adding a 23rd field, `exits.unit_mode`, to the payload `construction_fingerprint` hashes: adding a
key changes the hash even when every value is unchanged.  Proved by recomputing both field sets against
the same live config - 22 fields reproduce `0dcd044d...` exactly, 23 fields reproduce `b441ea62...`
exactly.  `unit_mode` BELONGS in the fingerprint (it decides what the exit overlay does - KILL-R14's
7-of-18 unplaceable stops are that setting), so the field is right and the silence was the defect:
`evidence_window` dropped from three days to one bar with nothing saying why.

Two mechanisms, decided by the operator on 2026-09-07:

* **A version on the payload**, reported OUTSIDE the hash.  Outside matters: inside, adding the version
  would itself change today's digest, which is the very thing being fixed.  Outside, a reader comparing
  two rows can see whether the FIELD SET differed, and a bump is now the explicit signal that a digest
  change may be definitional rather than real.
* **An alias table**, in code rather than config, so declaring two digests to be the same book takes a
  commit that carries the proof.  History is never rewritten - the old rows keep the digest they were
  written with, and the equivalence lives in the reader.
"""

from __future__ import annotations

from beidou_live.health import (
    CONSTRUCTION_ALIASES,
    CONSTRUCTION_PAYLOAD_VERSION,
    canonical_construction,
)

BEFORE = "0dcd044d0158c6aec263429eab9cdba9449dba0b55b07807dfd0e3d3a3a9b6e0"
AFTER = "b441ea62d02184bfb6292ec1033f20482bc9c7c0a2c6ebfcc14623241b967417"
AFTER_P23 = "c0e5c49c5a4acb1d0e5bb709873ce38b0674c10a027405d5269dbb724d734d1c"
AFTER_MIN_HISTORY = "dd32720d3faf5ab0e6a2934a76e9d26489095d7b5a7d19d537bcef44751a7cf6"
AFTER_P30 = "ab3cb75fb2f8d94813aa44c5b6d869c34bf82bcc78757bf1fcfa0bba4e16e1b3"


def test_the_restart_5_digest_resolves_to_the_book_it_actually_is() -> None:
    assert canonical_construction(AFTER) == BEFORE


def test_an_undeclared_digest_is_returned_unchanged() -> None:
    """Silence means "different book"; only a written declaration makes two digests one."""
    assert canonical_construction("f" * 64) == "f" * 64
    assert canonical_construction(BEFORE) == BEFORE


def test_no_alias_points_at_something_that_is_itself_aliased() -> None:
    """A chain would make the canonical form depend on resolution order, which is a silent wrong answer."""
    for target in CONSTRUCTION_ALIASES.values():
        assert target not in CONSTRUCTION_ALIASES


def test_a_missing_digest_does_not_become_the_string_none() -> None:
    assert canonical_construction(None) is None


def test_the_payload_version_is_reported_outside_the_hash() -> None:
    """Structural, not a pinned literal: the digest must equal the hash of everything BUT the version."""
    import hashlib
    import json

    from beidou_live.engine import construction_fingerprint
    from tests.live.helpers_construction import live_config_for_profile

    out = construction_fingerprint(live_config_for_profile())
    hashed = {k: v for k, v in out.items() if k not in ("digest", "payload_version")}
    expected = hashlib.sha256(json.dumps(hashed, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    assert out["payload_version"] == CONSTRUCTION_PAYLOAD_VERSION
    assert out["digest"] == expected, "payload_version leaked into the hashed payload"


# The field set, written down.  This is the part that makes the next addition LOUD: twice in one day a
# merge added fields (P22's `unit_mode`, P23's four `regime_*`), each time moving the digest while the
# book was byte-identical, and each time `evidence_window` read it as a construction change and reset
# M-010.  Nothing failed, so nobody knew.  Now adding or removing a key fails here, and the fix is to
# bump CONSTRUCTION_PAYLOAD_VERSION and decide - in writing - whether the book actually changed.
EXPECTED_FIELDS = {
    "portfolio": {
        "vol_target",
        "vol_halflife",
        "covariance_halflife",
        "min_asset_vol",
        "max_scalar",
        # v4 (2026-09-09): it decides WHICH SYMBOLS may be held, so it decides the book.
        "min_history_bars",
        "sleeve_max_gross",
    },
    "guards": {"max_gross", "max_weight", "daily_loss_pause", "stale_bars_max"},
    # v6: what the loop does when a symbol's bars do not arrive.  Not a portfolio parameter and not a
    # guard - it decides whether a position survives an empty REST answer - so it gets its own block
    # rather than being filed under one of theirs.
    "inputs": {"dropped_after"},
    "rebalance": {
        "no_trade_band",
        "no_trade_rel_band",
        "max_participation",
        "max_order_notional",
        "exempt_reductions",
    },
    "exits": {
        "stop_loss",
        "trailing_stop",
        "take_profit",
        "cooldown_bars",
        "vol_halflife",
        "unit_mode",
        "regime_window",
        "regime_er_cut",
        "regime_tp_scale",
        "regime_side",
        "stale_carry_bars",
    },
    "throttle": {"enabled", "start", "stop", "floor"},
    "leverage": {"mode", "margin_cap", "max_leverage", "margin_buffer"},
}


def test_changing_the_hashed_field_set_fails_here_instead_of_silently_resetting_m010() -> None:
    from beidou_live.engine import construction_fingerprint
    from tests.live.helpers_construction import live_config_for_profile

    out = construction_fingerprint(live_config_for_profile())
    actual = {k: set(v) for k, v in out.items() if isinstance(v, dict) and k != "strategy_weights"}
    assert actual == EXPECTED_FIELDS, (
        "the construction fingerprint's field set moved.  That changes the digest even when every value "
        "is identical, and evidence_window reads a changed digest as a changed book - it reset M-010 "
        "twice on 2026-09-07 this way.  Bump CONSTRUCTION_PAYLOAD_VERSION, update EXPECTED_FIELDS, and "
        "say in the commit whether the BOOK changed; if it did not, add a CONSTRUCTION_ALIASES entry."
    )


def test_every_definitional_digest_so_far_resolves_to_the_one_book() -> None:
    """v1 22 fields, v2 +unit_mode, v3 +4 regime_*, v4 +min_history_bars, v5 +sleeve_max_gross.

    One book, measured five ways.  Every one of the four additions was a field the record needed and a
    value that had not moved; the alias is what keeps "the fingerprint learned to say something" from
    reading as "the book changed".
    """
    for digest in (BEFORE, AFTER, AFTER_P23, AFTER_MIN_HISTORY, AFTER_P30):
        assert canonical_construction(digest) == BEFORE


# --- the readers.  A canonicaliser nothing calls leaves M-010 reset exactly as before ----------------


def _rows(digests, start_ms=1_788_000_000_000, step=3_600_000):
    return [
        {"bar_open_ms": start_ms + i * step, "construction": d, "equity": 10_000.0 + i} for i, d in enumerate(digests)
    ]


def test_the_evidence_window_reaches_back_through_a_declared_alias(tmp_path) -> None:
    """The whole point of the ruling: M-010's window continues across restart #5 instead of restarting."""
    import json

    from beidou_live.reports import evidence_window
    from beidou_live.state import StateStore

    store = StateStore(tmp_path / "live")
    with store.cycles_path.open("w", encoding="utf-8") as handle:
        for row in _rows([BEFORE] * 5 + [AFTER] * 2):
            handle.write(json.dumps(row) + "\n")
    out = evidence_window(store)
    assert out["bars"] == 7, "the window stopped at the alias boundary"
    assert out["changes_7d"] == 0, "a definitional change was counted as a construction change"


def test_a_genuinely_different_construction_still_cuts_the_window(tmp_path) -> None:
    """The alias must not become a blanket amnesty: an undeclared digest still ends the window."""
    import json

    from beidou_live.reports import evidence_window
    from beidou_live.state import StateStore

    store = StateStore(tmp_path / "live")
    with store.cycles_path.open("w", encoding="utf-8") as handle:
        for row in _rows(["a" * 64] * 5 + [AFTER] * 2):
            handle.write(json.dumps(row) + "\n")
    assert evidence_window(store)["bars"] == 2
    assert evidence_window(store)["changes_7d"] == 1


def test_realised_vol_does_not_refuse_over_a_definitional_change() -> None:
    """`realised_vol` gives up when the window spans two constructions - it must not see two here."""
    from beidou_live.risk_budget import RiskBudgetParams, realised_vol

    rows = _rows([BEFORE] * 300 + [AFTER] * 300)
    out = realised_vol(rows, RiskBudgetParams(min_vol_bars=10))
    assert out["enforced"] is True, out.get("why")
