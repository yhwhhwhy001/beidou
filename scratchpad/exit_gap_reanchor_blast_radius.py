"""How much of the archive the 2026-09-13 gap fix moves, and proof it moves nothing else.

`exit_step` used to fall through its `price > 0` guard on a NaN close, re-anchoring the entry price on
the far side of the gap and turning the stop off for the segment that followed.  The fix holds the
state still on such a bar.  That is a deliberate behaviour change, so it has to be priced:

  1. which PIT members of the 1h archive carry an INTERNAL gap at all (NaN between their first and
     last observation - a listing date or a delisting is not a gap);
  2. that none of them is in the pinned live universe (`config/alpha_registry.yaml: universe`), so the
     running loop's numbers are untouched;
  3. that on a panel with no internal NaN, the pre-fix and post-fix `apply_exits` agree BIT FOR BIT
     over every bar that has a close, weights and events alike.

It also reports the one case the brief did not anticipate: a symbol whose archive simply STOPS.  Held
through its last stored bar, the overlay now keeps carrying that weight, because at the moment it
happens a stopped series and a gap are the same observation.  The numbers for that are printed apart
from the gap numbers, and `run_backtest` prices what it is worth.

The pre-fix code is read out of git rather than re-typed, so (3) compares against the real baseline.

    python scratchpad/exit_gap_reanchor_blast_radius.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from beidou_alpha.backtest import run_backtest
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.panel import Panel
from beidou_data.store import KlineStore

BASELINE = "2371aaec"  # main at the start of this branch
SHIPPED = ExitParams(stop_loss=6.0, take_profit=6.0)  # the live profile's exits


def _baseline_module() -> types.ModuleType:
    """Import `beidou_alpha/overlays/exits.py` as it stood at BASELINE, under its own module name."""
    source = subprocess.run(
        ["git", "show", f"{BASELINE}:beidou_alpha/overlays/exits.py"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    path = Path(tempfile.mkdtemp()) / "exits_baseline.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("exits_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["exits_baseline"] = module
    spec.loader.exec_module(module)
    return module


FIELDS = ["open", "high", "low", "close", "volume"]


def _panel(symbols: list[str], interval: str = "1h") -> Panel:
    store = KlineStore(".beidou/data")  # the root IS `.beidou/data`; "klines" is the `kind` default
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        raw = pd.read_parquet(store.path(symbol, interval), columns=["open_time", *FIELDS])
        raw.index = pd.to_datetime(raw.pop("open_time"), unit="ms", utc=True)
        frames[symbol] = raw.astype(float)
    return Panel.from_frames(frames, interval)


def _spans(close: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Per symbol-bar masks: before the first close, missing inside the span, after the last close.

    The three are what a NaN close can MEAN, and only the first is decidable causally.  `leading` is
    "never seen a price" - `cummax` of `notna`, so a truncated panel gives the same answer.  The other
    two are the same event seen from different ends of the archive: at the moment it happens, a gap and
    a series that has stopped are indistinguishable without reading the future.
    """
    valid = close.notna()
    seen = valid.cummax()
    will_see = valid[::-1].cummax()[::-1]
    return ~seen, seen & will_see & ~valid, seen & ~will_see


def _internal_gaps(close: pd.DataFrame) -> pd.Series:
    return _spans(close)[1].sum()


def _weights(close: pd.DataFrame) -> pd.DataFrame:
    """A deterministic stand-in for the model's book: a 24-bar momentum sign, sized like a real one.

    The exit overlay only ever reads the SIGN and the magnitude of the target, never how it was made,
    so any reproducible book exercises the same branches.  Missing closes become a zero target, which
    is what `build_weights` produces there (it fills NaN with 0).
    """
    signal = np.sign(close.pct_change(24, fill_method=None).fillna(0.0))
    return (signal * 0.02).astype(float)


def main() -> int:
    baseline = _baseline_module()
    membership = pd.read_parquet(".beidou/data/membership.parquet")
    store = KlineStore(".beidou/data")
    members = [s for s in membership.columns if bool(membership[s].any()) and store.exists(s, "1h")]
    panel = _panel(members)
    close = panel.close
    weights = _weights(close)
    print(f"PIT panel: {len(close):,} bars x {len(members)} symbols")

    gaps = _internal_gaps(close)
    gapped = gaps[gaps > 0].sort_values(ascending=False)
    print(f"\nsymbols with internal gaps: {len(gapped)}, {int(gapped.sum()):,} symbol-bars")
    for symbol, count in gapped.items():
        print(f"  {symbol:<16} {int(count):>5}")

    pinned = list(yaml.safe_load(Path("config/alpha_registry.yaml").read_text(encoding="utf-8"))["universe"])
    overlap = sorted(set(pinned) & set(gapped.index))
    print(f"\npinned live universe: {len(pinned)} symbols; overlap with the gapped set: {overlap or 'none'}")
    assert not overlap, "a pinned live symbol carries an internal gap - the loop's numbers would move"

    before = baseline.apply_exits(weights, close, SHIPPED)
    after = apply_exits(weights, close, SHIPPED)
    moved = pd.DataFrame(
        ~np.isclose(before.weights.to_numpy(), after.weights.to_numpy(), equal_nan=True),
        index=close.index,
        columns=close.columns,
    )
    leading, internal, trailing = _spans(close)
    print(f"\nfull PIT panel: {int(moved.to_numpy().sum()):,} symbol-bars move")
    print(f"  before the first close: {int((moved & leading).to_numpy().sum()):,}")
    print(f"  inside the span:        {int((moved & internal).to_numpy().sum()):,}  <- the gap bug")
    print(f"  after the last close:   {int((moved & trailing).to_numpy().sum()):,}  <- ragged archive tails")
    print(f"  on a bar that has a close: {int((moved & close.notna()).to_numpy().sum()):,}")
    print(f"exit events: {len(before.events):,} -> {len(after.events):,}")
    keys = ["time", "symbol", "rule"]
    old_rows = {tuple(r) for r in before.events[keys].itertuples(index=False)}
    new_rows = {tuple(r) for r in after.events[keys].itertuples(index=False)}
    for tag, rows in (("only before the fix", old_rows - new_rows), ("only after the fix", new_rows - old_rows)):
        for row in sorted(rows, key=str):
            print(f"  {tag}: {row[1]} {row[2]} at {row[0]}")
    moved_anchor = before.events.merge(after.events, on=keys, suffixes=("_old", "_new"))
    shifted = moved_anchor[moved_anchor["entry_price_old"] != moved_anchor["entry_price_new"]]
    print(f"  events kept but re-priced (the anchor that used to be lost): {len(shifted)}")
    for row in shifted.itertuples(index=False):
        print(f"    {row.symbol} {row.rule} at {row.time}: entry {row.entry_price_old} -> {row.entry_price_new}")
    per_symbol = moved.sum()
    touched = per_symbol[per_symbol > 0].sort_values(ascending=False)
    print(f"symbols touched at all: {len(touched)}; with an internal gap: {len(gapped)}")
    on_span = (moved & ~(leading | trailing)).sum()
    print("symbols whose OBSERVED bars move:")
    for symbol, count in on_span[on_span > 0].sort_values(ascending=False).items():
        print(f"  {symbol:<16} {int(count):>7}  (internal gaps: {int(gaps[symbol])})")

    # The bit-for-bit claim, on the panel the change is supposed not to touch: no internal gaps, and
    # only the bars each symbol actually has a close for, because a ragged archive tail is a different
    # question (see the `trailing` count above) and is reported separately rather than filtered away.
    clean = [s for s in members if gaps[s] == 0]
    sub_close, sub_weights = close[clean], weights[clean]
    before_clean = baseline.apply_exits(sub_weights, sub_close, SHIPPED)
    after_clean = apply_exits(sub_weights, sub_close, SHIPPED)
    observed = sub_close.notna().to_numpy()
    a = before_clean.weights.to_numpy()[observed]
    b = after_clean.weights.to_numpy()[observed]
    exact = np.array_equal(a.view(np.uint64), b.view(np.uint64))
    print(f"\ngap-free panel: {len(clean)} symbols x {len(sub_close):,} bars")
    print(f"  observed symbol-bars: {int(observed.sum()):,}; bit patterns identical: {exact}")
    pd.testing.assert_frame_equal(before_clean.events, after_clean.events)
    print(f"  events identical: True ({len(before_clean.events):,} rows)")
    assert exact

    # What the difference is actually worth once the backtest reads it.  `run_backtest` fills a
    # missing asset return with 0, so a weight carried past the end of a symbol's archive earns
    # nothing; what it can still move is turnover (the round trip that no longer happens at the gap)
    # and reported gross exposure.
    old_run, new_run = run_backtest(panel, before.weights), run_backtest(panel, after.weights)
    old_sum, new_sum = old_run.summary(), new_run.summary()
    print("\nrun_backtest on the full PIT panel (no cost model, open_to_close):")
    for key in sorted(set(old_sum) & set(new_sum)):
        if old_sum[key] != new_sum[key]:
            print(f"  {key:<18} {old_sum[key]!s:>22} -> {new_sum[key]!s}")
    diff = (new_run.portfolio_net - old_run.portfolio_net).abs()
    print(f"  bars whose net return moves: {int((diff > 0).sum()):,} of {len(diff):,}; max |delta| {diff.max():.3e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
