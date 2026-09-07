"""Two undeclared conventions, declared (2026-09-08 audit).

Both were correct and neither was written down, which in this repository is the failure mode with its
own entry in the log: a fact that is true and unstated reads exactly like an assumption nobody checked.

* `sharpe` has no risk-free term.  At 30% annualised volatility a 4% rate is worth about 0.13 of
  Sharpe - larger than several effects this repository has re-run whole validations over - and the
  book's equity sits at the venue as margin, so the opportunity cost is real.  The number stays as it
  is (changing it would break comparability with every archived report); what changes is that it says so.
* `backtest`'s module docstring called `open_to_close` "conservative".  That is true of the entry price
  and false of the holding return: the convention silently drops close_t -> open_{t+1} on every held
  bar, worth 2.36% of total absolute price movement, and re-earning it costs -0.029 OOS Sharpe on the
  shipped book - the dropped component is adverse, not favourable.

Asserted rather than trusted, the same way `test_selection_gate` pins "short-sample guard": a paragraph
nothing reads is a paragraph that gets deleted by the next person who tidies the file.
"""

from __future__ import annotations

import beidou_alpha.backtest as backtest_module
from beidou_alpha.validation.metrics import sharpe


def test_the_sharpe_convention_names_its_risk_free_rate() -> None:
    doc = sharpe.__doc__ or ""
    assert "risk-free" in doc
    assert "0" in doc, "the convention is that the rate is zero; say which zero"


def test_the_execution_convention_separates_the_entry_price_from_the_holding_return() -> None:
    doc = backtest_module.__doc__ or ""
    assert "conservative about the entry price" in doc, "the claim survives, scoped to where it holds"
    assert "not about the holding return" in doc, "and the half where it does not is stated"
    assert "-0.029" in doc, "with the size of what is dropped, measured rather than called negligible"
