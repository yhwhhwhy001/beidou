"""M-003: the non-alpha line budget is measured, so a breach is visible instead of assumed away.

The plan set a budget - `beidou_live` <= 2,000 lines, non-alpha <= 6,000, and at least 60% of the source
in `beidou_alpha` - and made exceeding it trigger a KILL-003 re-review.  Nothing ever measured it, so the
budget was breached without anyone noticing: the audit found non-alpha at 6,761 lines against 6,000, and
`beidou_live` at 2,776 against 2,000.

This test does not enforce the plan's numbers, because meeting them today would mean deleting tested code
the operator asked for, which is a worse outcome than carrying the debt.  It ratchets instead: today's
measurement is the ceiling, so the breach cannot grow while the operator decides whether to re-price the
budget or spend effort shrinking it.  Decided 2026-09-04: the operator carries the breach for now and
revisits it as long-term work, so this test's job is to hold the line rather than to force a cleanup.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("beidou_alpha", "beidou_live", "beidou_cli", "beidou_data", "beidou_exchange", "beidou_shared")

# The plan's budget, kept here so the gap between intent and reality stays legible.
# The plan's original budget, plus the operator's 2026-09-04 revision of the alpha share from 60% to 90%.
# The share is deliberately NOT asserted against the tree: reaching 90% of lines would mean 68,706 lines of
# signal code against today's 3,661, and bloated signal code is what the V5 rebuild deleted.  The 90% target
# governs newly authored work and is measured per week by `beidou report weekly` (reports.effort_share).
PLAN_BUDGET = {"beidou_live": 2_000, "non_alpha_total": 6_000, "alpha_share_tree": 0.60, "alpha_share_effort": 0.90}

# Measured 2026-09-04 after the audit remediation.  A ceiling, not a target: lower is always fine.
#
# The first version of this file set beidou_live at 2,900 from the audit's own 2,776 reading and failed
# immediately at 3,043, which is the ratchet doing its job on its author: the remediation itself - the
# per-strategy income drift, the leg split, the probe correlation, the exit and pool sections - added
# about 270 lines to that package.  Raising a ceiling is allowed only in the commit that says why, and
# this is that sentence.  It then failed a second time on beidou_alpha, for the sign-bucketed IC that
# closed KILL-042.  Final measurement after the whole remediation: alpha 3,654, live 3,171, cli 2,276.
# The gap to the plan's 2,000 for beidou_live is 1,171 lines, and the alpha share is 34% against a 60%
# target, revised to 90% for new work on 2026-09-04.  Both are open operator decisions, recorded rather
# than redefined.  Noted without irony intended: instrumenting the 90% alpha target cost non-alpha lines,
# in beidou_live and beidou_cli, which is the tension the target exists to make visible rather than a
# reason to skip measuring it.
# Third raise, 2026-09-04, and the sentence the rule requires: +16 in beidou_exchange and +10 in
# beidou_shared, for the position parser.  demo-fapi's /fapi/v2/account rows carry a correct `notional`
# but no `markPrice`, and the parser derived notional as qty x mark, so every account-derived position
# came out at zero: `gross_notional()` read 0.00 while fifteen positions held 2,884 USDT of exposure.
# The same rows spell it `unrealizedProfit` where positionRisk spells it `unRealizedProfit`.  Most of the
# 26 lines are the docstring recording those two disagreements, which is the part that stops the next
# reader from "simplifying" the parser back into the bug.
# Fourth raise, 2026-09-04, with the sentence the rule requires: +22 in beidou_alpha for the D-029 gate
# that lets a probe book cite a REJECT only when the registry acknowledges it in writing.  The alternative
# was leaving the flow sleeve pointed at an ACCEPT whose universe no longer existed on disk, which is the
# stale-pointer failure this round kept finding.  Most of the 22 lines are the docstring naming the two
# bad options it replaces - the part that stops a later reader from deleting the acknowledgement as
# ceremony.  Worth noting which package grew: this one is beidou_alpha, so it moves the alpha share the
# right way, unlike the three raises above it.
# Fifth raise, 2026-09-04, with the sentence the rule requires: +90 in beidou_live for three measurements
# a full system check found missing, all in the same shape - a number that read zero while the thing it
# named was not zero.  M-007's "peak margin usage" measured what a cycle's new *orders* asked for, so it
# printed 0.00% on a day whose only margin-consuming order predated the evidence window while fifteen
# positions carried 577 USDT of initial margin; `Snapshot.margin_usage` now records what the *held* book
# consumes, every cycle.  `clock_health` says how far the report's own timestamps sit from the venue,
# because the host clock was a full hour behind and nothing in the report mentioned it.  `data_coverage`
# names live symbols with no research klines, because CYSUSDT traded for sixteen hours while every
# backtest silently excluded it behind a log line.  This one grows the wrong package and the alpha share
# with it; the alternative was leaving three instruments reading zero, which is the failure mode this
# whole file exists to make visible.
# Sixth raise, 2026-09-04, with the sentence the rule requires: +36 in beidou_live and +11 in
# beidou_exchange for D-030, the income window.  A wrong host clock is invisible on the auth path - a
# -1021 makes the REST client resync and retry, so a signed request's `timestamp` is always
# venue-correct - but `startTime`/`endTime` on /fapi/v1/income are plain query parameters and were
# passed through untouched.  With the host an hour behind, the loop asked for an hour-old window:
# measured after an operator flatten, 96 rows worth +23.98 USDT sat at venue times 07:51-07:53 while
# the loop queried [06:53, 06:58] and ingested nothing.  Income was not lost, it arrived an hour late
# and was attributed to the book held an hour after it earned it, which is the one corruption M-010
# cannot absorb.  Most of the addition is `venue_time_ms` plus the docstrings recording why the
# obvious simplification - "just use self.clock like everything else" - is the bug.
# Seventh raise, 2026-09-04, with the sentence the rule requires: +45 more in beidou_live for D-031's
# venue-health quarantine and for the D-014 entry-side fix it exposed.  The fix is the part worth the lines:
# `leaving` was being read as "outside the pool", but it is filtered to symbols that still hold a position,
# so in the cycle after a departing symbol was flattened the loop opened a fresh position in it - 952 USDT
# of a name that had left the pool the day before, reproduced in a test before it was believed.  Most of the
# 45 lines are the two docstrings recording why quarantine needs evidence from another symbol in the same
# cycle, and why the entry side follows the universe while the exit side follows positions; deleting either
# comment restores a bug that looks like a simplification.  Three of the four raises on this page landed the
# same day from two sessions working in parallel, and this one had to be renumbered twice - fifth to sixth to
# seventh - which is worth a line here: with concurrent authors the ratchet is doing double duty as a merge
# detector, and that is a feature.  Noted honestly: non-alpha growth against the 90% target, buying plumbing
# correctness rather than signal.
# Eighth raise, 2026-09-04, with the sentence the rule requires: +124 in beidou_live and +14 in
# beidou_exchange for D-032, the foreign-fill reconciliation.  The operator flattened the book by hand and
# its +26.30 realised P&L was attributed to tsmom.  That is right in economic terms - tsmom chose and held
# those positions - but it crystallised a whole holding period into one bar, and M-010 reads a per-bar
# income series, so both mean and variance moved.  `external_flows` could not catch it: that only knows
# TRANSFER rows, and a manual close is REALIZED_PNL.  An income row names a tradeId and nothing else about
# provenance, so the split needs a /fapi/v1/userTrades join (tradeId -> orderId -> our own order log).
# Most of the addition is that join plus the docstrings recording why "attribute everything" is wrong here
# and why a failed reconciliation must fall back to it anyway rather than reclassify a cycle's P&L as
# somebody else's.  This one lands on the live/exchange side again; the honest note is that the whole
# family of raises since the audit has been instrumentation, which is what the operator keeps asking for
# and what the alpha-share target keeps counting against us.
# Ninth raise, 2026-09-04, with the sentence the rule requires: +68 in beidou_live for two silences the
# operator hit on the same day.  The first is the leverage record: `state.leverage_set` was consulted to
# decide whether to POST, but nothing on this venue can report the setting back - positionRisk v2 and v3,
# the account rows and symbolConfig all read 0 or null - so the cache was authoritative over a venue it
# could not observe, and after an account reset took the setting back to its default the loop never
# re-sent it.  Startup now re-asserts unconditionally; most of the addition is the docstring recording
# which four endpoints were checked, because "just skip the POST when it already matches" is exactly the
# simplification that restores the bug.  The second is the no-trade band, whose skip was the most common
# outcome of a cycle and the only one that left no trace at all: a bare `continue`.  CYSUSDT sat in the
# universe for a day with a -41 USDT target against a 54 USDT absolute band - scored every cycle, ordered
# never - and no instrument in the system could name it, because a symbol the band can never let in and a
# symbol that did not need trading produced identical records.  BAND_BLOCKS_ENTRY/EXIT separate the
# structural cases from the ordinary suppressed resize, and `plan_gaps` puts all three in the daily report
# where the ordinary count doubles as P10 cell B's registered turnover falsifier.  Non-alpha growth again,
# and again buying observability rather than signal; the honest note is that both of these were found by
# looking at the live account rather than by any test, which is what the instrumentation is for.
# Tenth raise, 2026-09-04, with the sentence the rule requires: +95 in beidou_alpha and +10 in
# beidou_data for a strategy/factor audit that found three silent divergences between what runs and what
# the evidence describes.  The one that cost money was the funding alignment: `funding_per_bar` and
# `Panel.from_frames` matched a settlement to a bar by equality, Binance stamps `fundingTime` 1-47 ms
# past the hour, and 43.7% of the 1,010,914-row archive was therefore filled with zero - every
# `use_actual_funding` backtest under-charged funding by about half (the shipped book reads 1.5809 with
# the drop and 1.5477 without), and the share lost differed by year, so folds were not even biased
# alike.  The second was the no-trade band, applied both in the model and in the rebalancer while only
# the rebalancer has the reference the rule means; the model's copy was rebuilt each cycle over a
# sliding window, which made a weight a function of where that window began.  The third was warmup
# declarations that ignored chained rolling windows.  Nearly all of the addition is docstring: each of
# the three reads like a tidy-up that a later simplification would happily undo, and the reason it is
# wrong has to sit next to the code, not in this file.  Alpha growth that buys no signal, which the
# alpha-share target rightly keeps counting against us.
# Eleventh raise, 2026-09-04, with the sentence the rule requires, and the first one that moves the alpha
# share in the intended direction: +694 in beidou_alpha for the candidate-signal miner recovered from the V2 tree
# (a typed expression language and an enumerating search), +159 in beidou_data for the dataset manifest, and
# +103 in beidou_cli for `research mine` plus recording that manifest in every validate report.  The alpha
# share goes 32.4% -> 34.8%; every previous raise moved it the wrong way, which is the tension the 90%
# target exists to expose.  The miner is worth its lines only because it does NOT bring V2's evaluation
# stack with it: a candidate compiles to an ordinary SignalSpec, so walk-forward, CPCV, the trials ledger
# and the D-020/D-028 verdict judge it unchanged.  Most of beidou_data's addition is the manifest's stated
# limit - it detects replacement and drift, not in-place corruption of individual bars - which is the part
# that stops a later reader from trusting it for something it does not do.
# Twelfth raise, 2026-09-04, with the sentence the rule requires: +146 in beidou_alpha for four more
# expression families and the two nodes they need.  The first search could only express momentum - with no
# product node there was no way to write a minus sign - so it offered 93 candidates that were all the same
# shape, and its best was 0.7 Sharpe below the book already running.  `Mul` (dimensionless x dimensionless
# only) buys reversal and gating; `RangePosition` is the only family that reads high and low.  The
# distinction being paid for here is structural, not parametric: a fourth scale on an existing family
# would raise `declared_trials`, and so the DSR bar anything promoted must clear, without adding a
# hypothesis.  Alpha-package growth that buys hypothesis space is the one kind this file should welcome.
# Thirteenth raise, 2026-09-04 - renumbered from twelfth on the merge, because a parallel session took
# that number the same day; the file has done this before and it is the ratchet working as a merge
# detector.  With the sentence the rule requires: +118 in beidou_alpha and +31 in
# beidou_live so the backtest can score the book the loop would actually hold.  Two guards bind the whole
# book - the per-symbol and gross caps, and the -5% daily-loss pause - and neither was ever in the
# backtest, which was invisible because neither was reachable: 5.6 years of the shipped construction
# produced a worst UTC day of -3.5% against the -5% pause and never crossed gross 2.0, so a replay would
# have changed nothing.  Doubling the vol target makes both reachable (measured: 355 capped bars and 36
# paused bars on the point-in-time universe), which is KILL-027's shape, so the semantics moved into
# beidou_alpha and beidou_live/guards.py now calls them - one definition, no drift.  Most of the alpha
# addition is the replay loop and the two docstrings that keep it correct: it rolls the day on the
# DECISION bar, not the execution bar, because the loop rolls `day_start_equity` in `_roll_day(bar_open_ms)`
# and at an hourly interval those straddle UTC midnight once a day; and it cannot be vectorised, because
# the pause reads the equity path it is itself producing.  Both read like something a later reader would
# tidy away.  The live side is the D-016 startup check, which refuses `auto` when `max_leverage` cannot
# satisfy `margin_cap` - unreachable today only because 2.0 = 5 x 0.40 is exact - plus LiveConfig finally
# carrying the portfolio params so the construction digest can see the vol target that sets the book's
# size.  This raise moves the alpha share the intended way: 118 of the 170 lines are in beidou_alpha.
# The +21 in beidou_cli is `research backtest --guards/--no-guards`, on by default: a report that does
# not say whether the guards were replayed cannot be compared with one that does, and the evidence for
# the vol-target change has to be a report rather than a scratch script.
# Fourteenth raise, 2026-09-04, with the sentence the rule requires: +20 in beidou_cli so a mined
# candidate is addressable by its hash across commands.  Enumeration is deterministic and touches no
# data, so `mined_<hash>` re-derives rather than persists - which also means a hash that no longer
# enumerates is reported as gone instead of quietly resolving to a stale definition.  It earned its
# lines immediately: it is what let `research correlate` answer the only question that could have
# rescued the search's best candidate, and the answer was no (correlation 0.47 with tsmom, marginal
# Sharpe -0.08).  Twenty lines to close a line of enquiry is the right trade.
# Fourteenth raise, 2026-09-04, with the sentence the rule requires: +264 in beidou_live and +12 in
# beidou_cli for P13's monitoring - the drawdown ladder, the realised-vol band, the slippage check against
# the cost model's 7 bps, and the counts for the two guards that were dormant until this week.  The lines
# are worth it for one reason: the vol target was raised on a bootstrap that resamples WEEKLY blocks, so it
# preserves within-week autocorrelation and destroys the multi-month regime structure real bear markets
# have.  It is optimistic by construction and the ladder is what covers that gap; a pre-registered
# threshold nothing measures is the failure this whole file exists to make visible.  Most of the addition
# is the refusal to answer: a window with too few bars, or one straddling a construction change, reports
# `enforced: false` with the reason rather than a number that reads like a pass - the same shape as the
# three instruments that were printing zero in the fifth raise above.  Also deliberate, and the reason
# there is no automation here to pay for: this alerts, it does not trade.  Rewriting live position sizing
# from a cron job is a different risk from measuring it, and the ladder removes the discretion about WHAT
# to do, not the step of a human doing it.  Non-alpha growth again, against the 90% target, and this one
# has no excuse except that an unmeasured risk budget is worse.
# Fifteenth raise, 2026-09-05, with the sentence the rule requires: +106 in beidou_live, +20 in
# beidou_alpha and +8 in beidou_cli for M-015, because the operator has now asked the same question on
# three separate days - why is every order at 5x - and the report was the reason it kept coming back.
# D-037 had already answered it (maintenance margin is indexed by notional tier, not by the chosen
# leverage, so the venue setting carries no risk; adaptation is stage 1's `vol_target / asset_vol`),
# but it answered it in ARCHITECTURE.md, and the daily report showed `last_targets` with nothing to
# read them against - so the only per-symbol number visible anywhere was the uniform 5x.  A true fact
# that no instrument states is indistinguishable from an unproven one, which is this file's whole
# subject.  The lines buy a falsifier rather than a display: `compression` is the risk-contribution
# spread over the market-vol spread, it reads 0.13 on the live book (a 12.2x spread in annualised
# volatility compressed to 1.6x in risk) and it converges on 1.00 if stage 1 is ever removed, so the
# ALERT fires on the regression rather than on the question.  The obvious simplification a later
# reader will reach for is recomputing sigma in the report from the klines archive instead of
# recording it per cycle; that is shorter and answers a different question - a different bar, and
# nothing tying it to the halflife the construction used - which is why `asset_vol` was extracted in
# beidou_alpha and is carried on `TargetWeights` from the same panel the weights came from.  Twenty of
# the 134 lines land in beidou_alpha and they delete a duplicated formula rather than adding one; the
# rest is instrumentation against the 90% target, same as most of this page, and the honest note is
# the same as the ninth raise's: this was found by an operator looking at the live account, not by a
# test.
# Sixteenth raise, 2026-09-05, with the sentence the rule requires: +9 in beidou_live so `realised_vol`
# skips a bar that absorbed an external cash flow.  This is the cheapest raise on the page and the least
# defensible as new capability, because it buys none: `drawdown_state` in the same file already re-bases
# its high-water mark on exactly these rows and `reports.drift_check` already drops them, so the defect
# was an inconsistency inside one module rather than a concept nobody had had.  The size is why it is not
# cosmetic.  Today's reset injected -0.187% and would have added 0.0065 - harmless, but only because the
# account was flattened before it was reset.  A reset taken while the book is held moves equity by several
# percent in one bar, and one +4.7% bar adds 0.163 of annualised vol over a full window, which is wider
# than the whole [0.26, 0.38] band: a single one could fire the ALERT on its own, and the memory says the
# operator may reset at any time.  Recorded while here, because it changes what the daily report means:
# the vol band does not arm on `min_vol_bars` around 2026-09-14 as the report's "52 bars, needs 240"
# reads, but on the single-construction gate around 2026-10-04, and only if the construction is untouched
# until then.  The report prints the first gate because the code returns on it first.  Honest note, same
# as the ninth and fifteenth raises: found by an operator asking whether history could substitute for the
# 30-day window, not by a test.
# Seventeenth raise, 2026-09-05, with the sentence the rule requires: +19 in beidou_alpha for the margin
# buffer the guard replay now reports (M-016).  Buys no capability and changes no number the project has
# published: the replay's weights, net returns and both guard counts are untouched, and the instrument is
# read by nobody in the book.  It exists because "liquidation is unreachable at gross 2.0" was true and
# unstated.  `margin_cap` 0.40, `max_weight` 0.15 and the -5% pause all sit far in front of it, so the
# conclusion was right - but D-037 is the standing lesson that a correct fact no instrument reports is
# indistinguishable from an unproven one, and the shipped book now prints the distance instead of relying
# on the argument.  Measured on the inertness fixture: 1,999x the requirement.  The audit is
# docs/analysis/2026-09-05-backtest-guard-external-audit.md, so "an outside audit" resolves in-repo.  Two limits recorded so the
# number is not read for more than it says.  It inherits the replay's continuous-rebalancing assumption -
# the book is a fraction of *current* equity, so it deleverages as equity falls and the buffer is a
# function of the bar's return and gross, not of the equity level.  And it is close-to-close, so an
# intra-bar path that liquidates and recovers inside one hour is invisible to it; seeing that needs the
# drifted-position model this module's docstring already flags as its one approximation against live.
# The test that earns the raise is the second one, not the first: an instrument that reports zero touches
# proves nothing until something makes it fire, so a 0.45 maintenance rate drives the buffer under 1.0 on
# an ordinary bar.  Honest note: found by an outside audit of this repo, not by a test.
# Eighteenth raise, 2026-09-05, with the sentence the rule requires: +64 in beidou_alpha for the
# participation instrument (M-017).  Same shape as the seventeenth and the same restraint: it reports
# what `beidou_live.rebalancer.plan_rebalance` would have refused and applies nothing, so weights, net
# returns and every published Sharpe are bit-for-bit what they were - asserted, not asserted-to.
# It is deliberately NOT the fix.  `config/live.demo.yaml` already records the real one: the vol-target
# k is scale-free only because gross P&L, turnover cost and funding are all linear in the weights, that
# identity "proves nothing about a world with market impact", and k must be re-derived under an
# impact-aware cost model - "recorded as out of scope, not as done".  Building that model here would
# have moved a registered book on an auditor's initiative.  This measures how urgent it is instead.
# `capital` is the honest cost of the instrument: it is the first parameter in this module that is not
# scale-free, because the cap is an absolute notional while everything else is a fraction of equity.
# Two fidelity details are in the code rather than here because they change the number: a full close is
# exempt (live's `closing`), and a symbol with unknown liquidity is never capped (live requires
# `cap is not None and cap > 0`) - without the second, the measurement would refuse every early bar.
# Honest note: found by an outside audit, whose first write-up had the exemption backwards - it claimed
# only risk-adding orders were capped, when live exempts full closes alone.  The test encodes the real
# rule, which is why it is worth having a test rather than a paragraph.  Curve in scratchpad/participation_capacity_sweep.py;
# audit and its correction in docs/analysis/2026-09-05-backtest-guard-external-audit.md.
# Nineteenth raise, 2026-09-05, with the sentence the rule requires: +37 in beidou_alpha, all of it the
# edge statement in signals/tsmom.py.  No code, no behaviour, no number moves.  The external audit asked
# what the one strategy that trades is paid for and found the answer nowhere in the repo - the docstring
# was the formula and nothing else - and this page is the wrong place to argue that a comment can be
# worth 37 lines, so: it names the counterparty (the late leveraged long), marks the edge behavioural
# rather than structural so its decay is expected rather than surprising, and records the measured
# NEGATIVE that narrows it - carry in rank mode is gross -6% over five years, so this is not funding
# carry in disguise.  It also carries the per-fold table of the crowding modifier, because the modifier
# helps most where the base is weakest (fold 2, 0.50 -> 0.90) and costs a little where it is strongest
# (fold 5, 2.66 -> 2.56), which is a tail-mitigation shape and not a return enhancer, and the docstring
# would be a story rather than a statement without it.  Marked in the text as a hypothesis: the pattern
# was read after the fact, five folds is five observations, and M-010 is the live arbiter.
CEILING = {
    "beidou_alpha": 4_876,
    "beidou_live": 4_198,
    "beidou_cli": 2_582,
    "beidou_data": 1_258,
    "beidou_exchange": 539,
    "beidou_shared": 280,
}


def _lines(package: str) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted((ROOT / package).rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def test_no_package_grows_past_its_measured_ceiling() -> None:
    measured = {package: _lines(package) for package in PACKAGES}
    over = {name: (count, CEILING[name]) for name, count in measured.items() if count > CEILING[name]}
    assert not over, (
        f"these packages grew past the ratchet: {over}. "
        "Either take the growth back out, or raise the ceiling in the same commit that justifies it."
    )


def test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined() -> None:
    """A failing budget the operator has seen is honest; a budget nobody measures is not."""
    measured = {package: _lines(package) for package in PACKAGES}
    non_alpha = sum(count for name, count in measured.items() if name != "beidou_alpha")
    alpha_share = measured["beidou_alpha"] / max(1, sum(measured.values()))
    # These are the facts the operator is deciding about.  If a future change happens to bring the tree
    # back inside the plan, this test starts failing and the decision can simply be closed.
    assert non_alpha > PLAN_BUDGET["non_alpha_total"], "non-alpha is back inside the plan; close the decision"
    assert measured["beidou_live"] > PLAN_BUDGET["beidou_live"], "beidou_live is back inside the plan"
    assert alpha_share < PLAN_BUDGET["alpha_share_tree"], "alpha share recovered; close the decision"
