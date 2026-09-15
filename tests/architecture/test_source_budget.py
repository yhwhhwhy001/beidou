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
PACKAGES = (
    "beidou_alpha",
    "beidou_live",
    "beidou_cli",
    "beidou_data",
    "beidou_exchange",
    "beidou_shared",
    "beidou_governance",
)

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
# Twentieth raise, 2026-09-05, with the sentence the rule requires: +55 in beidou_data for D-040, the
# manifest's blind spot on the funding archive.  `_store_fact` spelled the store layout out a second
# time and got it wrong - it walked `funding/<SYMBOL>/funding.parquet` and skipped every file in the
# flat `funding/<SYMBOL>.parquet` store - so the fact read {0, 0, _digest({})} on a 231-file / 20 MB
# archive, both sides of `manifest_problems` were zero, and no report's manifest could ever flag the
# funding data.  D-034 rewrote what that archive means underneath four reports and none of them could
# see it.  The lines are not the one-character fix: they are `FundingStore.symbols()` and a `directory`
# property on both stores, so the layout exists in exactly one module and cannot drift again, plus the
# version stamp and `_unrecorded_fields`, which keep a pre-fix zero readable as "never measured"
# instead of silently becoming "the archive grew from nothing" on every historical report.  The cheaper
# option was to branch on `interval is None` in place; it would have left the duplicated layout that
# caused this sitting there for the next reader.  KILL-027 / D-038 shape, one layer down.
# Twenty-first raise, 2026-09-05, with the sentence the rule requires: +62 beidou_data, +64 beidou_live,
# +18 beidou_cli for D-041, which gave `manifest_problems` its first caller.  D-040 repaired the funding
# manifest and left it an instrument nobody read: `validate` wrote a dataset manifest into every report
# and no code path ever compared one back against the data, so the stale-evidence pointer the manifest
# exists to catch still could not be caught.  The lines are almost entirely the severity split, and that
# split is the difference between a gate and an annoyance: a membership rebuild blocks (validation runs
# on that table; P12 is what a rebuilt one does), while klines/funding growth does not, because the
# daily sync causes it every day.  Universe is the case that had to be measured rather than assumed -
# the loop rewrites `universe.json` itself, and tsmom's cited universe read `pool-refresh`/15 against
# `live-refresh`/16 on disk, so blocking on any universe move would have made the loop refuse to start
# because of its own refresh; it now blocks only when the symbol set moves under an unchanged source.
# beidou_cli grew least because the wiring deleted something: `report daily` and `report weekly` held
# byte-identical evidence-loading loops, now one helper.  Verified against the live archive: nothing in
# flight is blocked today, and both enabled strategies report advisory lines only.
# Twenty-second raise, 2026-09-05, with the sentence the rule requires: +13 in beidou_alpha and +51 in
# beidou_cli for E-040 / KILL-027 on the research path.  `AlphaModel.targets` has refused a funding-consuming
# model without funding history since D-023; `strategy_targets` never did, so `beidou research backtest
# --strategy tsmom --no-funding` exited 0 and reported a Sharpe for a run in which the crowding modifier its
# own report cites had consumed nothing.  Research is where evidence is produced, so that is the worse half.
# The alpha lines are the guard plus the paragraph saying why it sits on `strategy_targets` rather than on
# `evaluate`: `decompose_book` reaches the signals without ever calling `evaluate`, so the obvious placement
# would have left `research decompose` producing exactly the report this exists to prevent.  The cli lines are
# two helpers.  `_require_funding` is not redundant with the library guard - `research diagnose` computes the
# signal directly and builds no model at all, so nothing else would stop it, and an operator who typed
# `--no-funding` should be told which strategy and which flag rather than handed a traceback.  `_funding_facts`
# is the part no guard can cover: `FundingStore.load` returns an empty frame for a symbol with no archive, so
# `--funding` against a partial archive yields a zero column, and tsmom's crowding rank reads an unobserved
# symbol as uncrowded rather than failing.  `symbols_settled` is what makes that legible on disk instead of
# arriving as a quietly weaker modifier.  Historical reports are deliberately NOT retrofitted: what earlier
# `--no-funding` evidence is worth is the operator's call and docs/RESEARCH_LOG.md's to record.
# The cli figure above then grew again before this landed, and the extra lines are the more important half.
# A review of the first draft found that the flag it guarded was the wrong thing to guard: BOTH checks keyed
# off `panel.funding is None`, which asks "did the operator type --no-funding", not "does the signal have the
# inputs it was judged on".  `--funding` is the DEFAULT, and against a root whose klines are synced but whose
# funding never was, `FundingStore.load` returns an empty frame per symbol, so `load_panel` builds a funding
# frame of all ZEROS - not None - and neither guard could tell that from real data.  Reproduced: exit 0, a
# report citing `crowding_window: 72`, an annualised Sharpe of 4.34, `symbols_settled: 0`, and no mention of
# funding in the terminal or the markdown.  That is byte-for-byte the report this whole change exists to
# prevent, reached by typing nothing at all.  So `_require_funding` now refuses zero settlements the same way
# it refuses a missing frame, and warns on the partial case instead of leaving it in the JSON where the
# operator will not look.  `_funding_consumers` and `_settled_symbols` are extracted because the guard and
# the report block would otherwise compute the same two things three times between them.
# Twenty-third raise, 2026-09-06, with the sentence the rule requires: +18 in beidou_alpha, and beidou_cli
# comes DOWN 5 to 2,673.  A second review round found that the twenty-second raise had fixed its own bug in
# one place only: `_require_funding` learned that an all-zero funding frame is not funding, and
# `AlphaModel.strategy_targets` was left on `panel.funding is None`, so the library guard - the one the
# docstring calls "the guard that cannot be forgotten" - had become the WEAKER of the two.  Verified by
# direct call: `strategy_targets` on a 0-settlement panel returned targets and raised nothing, which left
# `research book`'s robustness universes and every direct library caller (including
# scratchpad/verify_crowding_arms.py, the script the registry cites as corroboration for the crowding
# modifier) on the weak test.  The lines are `Panel.settled_symbols` and its docstring: the predicate now
# exists once, in the layer that owns the frame, and both guards ask it - which is why the cli figure falls
# rather than rises.  The alpha count also carries the test that the modifier CHANGES SOMETHING, and that
# one is the uncomfortable half: the same review showed a mutation deleting `apply_crowding_modifier` from
# `tsmom.compute` left every new test green, because the funded fixture wrote one constant rate to two
# symbols, so the trailing cross-sectional rank tied at 0.0 and `rank >= crowding_cut` never held.  The
# suite proved the precondition (funding was present) and never the conclusion (the signal read it and it
# mattered) - which is E-040's own shape, reproduced inside the tests written to prevent it.  Measured:
# 671 target cells move with the modifier wired, 0 with it unwired.
# Lowered 2026-09-06, which needs no justification but is worth a sentence anyway: beidou_cli 2,673 -> 2,670
# because `research overlay` stopped rebuilding its model by re-listing seven constructor fields and started
# using `dataclasses.replace`.  The re-listing had dropped `books=`, so `--min-history` raised on any
# registry declaring a sleeve.  Re-listing fields IS the bug class; the shorter form cannot rot.
# Twenty-fourth raise, 2026-09-06, with the sentence the rule requires: +6 in beidou_cli, all of it the
# paragraph explaining why the report block is called `funding_inputs` and not `funding`.  The blast-radius
# review found the collision the shorter name creates: a validation report already carries `dataset.funding`
# from D-040, which counts FILES IN THE ARCHIVE, so the report held two blocks named `funding`, each with a
# `symbols` key meaning a different thing - 2 files on disk against a 4-symbol panel.  On a partially-synced
# root the two even coincide by accident (both read 2, from different measurements), which is the worst kind
# of collision to leave in the artifact an operator reads to decide whether to trust a strategy.  No code
# confused them - the paths differ - so the whole cost of this is the comment that stops the next reader,
# or the next author looking for a shorter name, from re-creating it.
# Twenty-fifth raise, 2026-09-06, with the sentence the rule requires: +16 in beidou_alpha, +4 in
# beidou_cli, for `FundingUnavailable`.  The guard was raising a bare ValueError into two loops that treat
# a failure as a property of the ITEM being scored: `research mine` drops a candidate that raises into an
# `error` row, and `parameter_neighborhood` records a perturbation that raises as `None`.  A missing
# funding archive is a property of the RUN, so under those handlers the refusal degraded into a quietly
# thinner shortlist or a missing neighbour.  Reproduced on `mine` with a family declaring `uses_funding`:
# exit 0, 34 candidates error-rowed, and a shortlist printing `--prior-trials 225` - a count including
# candidates never scored, which is the number that goes on to size the DSR denominator.  Most of the 16
# alpha lines are the docstring saying why the type exists at all; it subclasses ValueError so no existing
# caller changes.  `mine` now also refuses up front, after enumeration, since its candidates ARE its
# strategies and `--strategy` is ignored there.  A guard any blanket handler can absorb is not a guard.
# Twenty-sixth raise, 2026-09-06, with the sentence the rule requires: +6 in beidou_cli, giving the
# `correlation` and `mine-shortlist` payloads the `costs` block every other research report already had.
# Both rank on cost-NET Sharpe and recorded neither the costs nor the funding stance that produced them,
# which is what left six historical correlate reports unknowable when the 2026-09-06 log entry tried to
# settle which evidence had been produced under `--no-funding`: `costs.use_funding` mirrors that flag
# verbatim everywhere else, and these two simply did not carry it.  Four of the six lines are the comment
# saying the numbers are net, because that is the part that makes the block look necessary rather than
# decorative to whoever next tidies a payload.
# Twenty-seventh raise, 2026-09-06, with the sentence the rule requires: merging P17's carry search
# (branch feat/mining-funding-node) into main.  Measured against main: +139 in beidou_alpha and +145
# in beidou_cli.  The cli figure is four lines under what the branch carried alone, because the merge
# removed four duplicates - both branches had independently taught `research mine` to record its cost
# model and to count settled symbols, and the merge keeps one of each: the top-level `costs` and
# `funding_inputs` that every other report in the file already used, and `panel.settled_symbols`
# instead of a count inlined in `research_mine`.  What the carry search adds:  The alpha lines are the kind this file says it should welcome,
# because they buy hypothesis space rather than plumbing: a `Funding` leaf, `Expr.reads_funding`, and a
# `_funding_family` of 42 expressions.  Funding was the one panel input no node could read - the archive
# has been on disk since 2026-09-03 and tsmom's crowding modifier already consumes it by hand, but the
# expression language could not, so every one of P14's 225 candidates was a price-or-volume shape and the
# clean negative it produced ("no money left in this expression space") was measured over a space that
# excluded carry entirely.  Roughly half of the 136 is docstring, deliberately: the family emits both
# signs, so the mirror of its worst candidate is its best and a carry expression is ALWAYS near the top of
# the shortlist by construction; the momentum-times-carry shape is not a searchable version of tsmom's
# crowding modifier and must not be read as one; and shape three carries a POSITIVE carry weight, the
# opposite of carry.py's prior.  Three things the next reader would otherwise get wrong, in the only
# place they will be read.
#
# The cli lines are the cheaper alternative's bill coming due.  `research mine` recorded a dataset
# manifest but not its own `--funding`, cost model, execution mode or portfolio params, so establishing
# what the 2026-09-04 shortlist actually ran - vol_target 0.15, funding charged - took a four-arm
# reproduction rather than a read.  D-024 requires a validate report to be reproducible from itself;
# `mine` was not, and now is (a `run` block, a stamped filename, and an `outcomes` count that makes the
# candidates which enumerate but never trade visible instead of merely absent).  The rest is `--baseline`,
# which answers the question the search exists to ask - is there a SECOND, uncorrelated book - rather than
# the one a bare ranking answers.  Eight of those lines are a refusal the ratchet itself extracted: this
# file failed on them, which is how the guard got written down rather than assumed.  `AlphaModel.targets`
# refuses a funding-consuming model without funding history (D-023) but `evaluate` - the research path -
# does not, so `--baseline tsmom --no-funding` would have run the crowding modifier inert and measured
# every candidate's marginal against a book nobody validated.  The general hole in the research path is
# older than P17 and is not closed here.
#
# Thirteen of the cli lines are the second time this file extracted a guard, and the first version of
# that guard was wrong.  It read `if include_funding and not funding` - the CLI FLAG, not the panel.
# `--funding` against a store with no funding archive yields an all-zero frame rather than None, so the
# flag says funding was requested, a None-check would say it arrived, and neither is the question.  Under
# that guard the whole carry family was kept, charged to `--prior-trials`, scored on constants, and the
# report recorded `include_funding: true`: an artefact asserting a family was searched when it was not,
# which is KILL-027 standing inside the guard written to stop it.  Measured before the fix on the August
# fixture with `--funding`: 267 evaluated, 42 carry candidates kept, 36 of them never traded.  The guard
# now counts symbols carrying a settlement and NARROWS - which is what the delivery contract asked for
# and what the first version had silently replaced with a refusal - and the run block records the
# searched value, the requested value and the count, so the three can never disagree unnoticed.
#
# The last twenty-three cli lines close two pre-registered rules the delivery had quietly not honoured.
# Rule 6 asked the shortlist to rank on the marginal against a named baseline; it was ranking on the
# full-sample Sharpe, which is the exact quantity the adversarial pass says produces a false "the space
# is empty" verdict - the best absolute candidate is usually the one most correlated with the book
# already running, and it is also the maximum of a few hundred noisy draws.  Rule 2 asked for
# `scored == evaluated`, which no run can satisfy, because `evaluated` fires before the complexity and
# lookback caps drop anything; the achievable form of the same intent is that every counted expression
# lands in exactly one bucket, and it is now computed, recorded and refused rather than described.  The
# amendment itself is written down in docs/analysis/2026-09-05-mining-proposer-pivot.md - a frozen rule
# that turns out to be unsatisfiable is replaced on the record, not silently.  Ten more carry the
# baseline block's params, net return and drawdown, and three more move `_resolve_mined` into `_entry`,
# the one chokepoint every strategy id passes through - it had been wired into `correlate` alone, so
# `research validate --strategy mined_<hash>` raised a bare KeyError, which is the wall an operator hits
# the moment the shortlist hands them something worth validating.  On the baseline block: naming the strategy is not enough when the registry moves under it, since a marginal measured
# against tsmom-with-crowding is a different number from one measured against tsmom-without and nothing
# in the artefact separated them.
#
# The three plan thresholds below all move the safe way: non-alpha grew, but beidou_live did not move at
# all, and the alpha share rose (5,061 of 14,347 against 4,922 of 14,063).
#
# 2026-09-06, +96 in beidou_alpha: P1-01 / DL-Q1, the cross-sectional reference population.  The
# operators ranked, demeaned and took breadth over "whatever columns the caller loaded", so research
# (a point-in-time panel of every symbol that was ever a member, ~123 names on an average bar) and the
# live loop (the 15-18 it manages that day) computed different signals from identical registry
# parameters - KILL-027's shape, on the one modifier that is enabled and on the flow probe.  The lines
# buy an explicit contract instead of a convention: ``Panel.reference`` plus a ``within_reference``
# primitive, threaded through the five signals and the miner's ``cs`` node.  What it deliberately does
# NOT do is filter the panel, which would be shorter: masking the frames themselves would restart a
# re-entering symbol's rolling windows from NaN while the live path, which always requests full
# history, would not - a second divergence in place of the first.  The docstrings carry that reasoning
# because the cheap wrong version is the one a later reader would otherwise write.  A further +32 wires
# the two callers to it: ``reference_for`` (the population IS ``eligible`` - point-in-time membership
# intersected with the listing-age filter, so research never ranks a name the loop could not hold) and
# ``targets(reference_symbols=...)``, which the engine fills from the universe it manages that cycle.
#
# 2026-09-06, +90 in beidou_live and +26 in beidou_cli: DL-Q0 / KILL-Q15, the registry digest.  The
# engine builds its model once at startup and never reloads it, so editing the registry changes what
# the FILE says without changing what the LOOP trades - and on 2026-09-04 that ran for 93 cycles
# (loop up 17:21Z, `crowding_window` 0 -> 72 on disk at 20:03Z, process still on 0) with no instrument
# able to say so: the construction fingerprint covers the portfolio layer, the evidence gate runs
# before the edit, and `live verify` rebuilds from the same file it is checking.  So: every cycle and
# heartbeat records the digest of the configuration the PROCESS holds, and `live status --check`
# compares it against the file.  The rest is the live half of P1-01 - the cycle declares its
# cross-sectional population instead of letting it fall out of which frames came back, and `verify`
# reproduces against that same population so the monitor cannot report a mismatch it caused itself.
# Twenty-eighth raise, 2026-09-06, with the sentence the rule requires: +55 in beidou_alpha and +25 in
# beidou_live for M-018, the instrument the crowding modifier never had.  D-042's correction is the
# argument for it: `inputs.funding_history` was true for the whole window it measured, and the modifier
# was inert for 37 of those cycles because the process held `crowding_window: 0` - the field says the
# INPUT arrived, never that the modifier bit.  This is the D-038 shape again, so the answer is the same
# one: an instrument, not a paragraph.
#
# It records two counts rather than one, and the second is the reason the raise is not just plumbing.
# Under `conviction_mode: sign` a position is +-1 either way, so shrinking a score by half changes
# nothing unless it drops under `entry_threshold`; measured over 2021-2026 on the live 18, that is
# 9.87% of cells against 17.78% of shrinks, i.e. the adopted conviction mode absorbs 44.5% of the
# modifier.  `shrunk` alone would therefore have reported roughly twice the effect the book gets, which
# is the proxy this measurement exists to replace.  Derived from the mask the shrink itself applies -
# `crowding_mask` was extracted for that reason - and never from a counterfactual book, which would be
# a second answer to a question the mask already answers exactly.
#
# The live half is `_crowding_effect`, wrapped in a bare `except` on purpose and following `asset_vol`'s
# precedent above it: observability may not stop a trading cycle, and a run that cannot supply it
# records why rather than a zero (D-035's rule).
# Twenty-ninth raise, 2026-09-06, with the sentence the rule requires: +49 in beidou_cli for P19's two
# missing knobs, found by writing the pre-registration before running it.  Every grid in
# `enumerate_candidates` is a bar COUNT, and `research mine` exposed none of them, so the daily
# experiment the Firewall opened was unrunnable as specified: `--max-lookback 58` alone leaves 33 of 267
# candidates - two families out of seven - while all 267 are still charged to `declared_trials`, which is
# paying for a search that did not happen.  `--baseline` had the same shape one level down: it read the
# registry and ignored `--params`, so `--baseline tsmom --interval 1d` would have measured every marginal
# against a two-year-horizon book.
#
# The keys are validated against the signature rather than splatted, and that check is the load-bearing
# half: a typo would have searched the defaults while the report's own `run.grids` named something else -
# an artefact that lies about its own space, which is what the run block was added to stop.  Both the
# grids and the baseline params are recorded there for the same reason.
#
# Recorded as a governance cost, not hidden: exposing the grids makes the search space tunable from the
# command line, which `search.py`'s docstring warns about - a fourth scale raises `declared_trials`
# without adding a hypothesis.  The ledger still charges it and the artefact now states it, so the cost
# is visible rather than prevented.
#
# Eight of the lines are a second guard the pre-registered run itself extracted: ``max_lookback`` is
# both a valid enumerator parameter and a CLI flag, so ``--grids`` setting it passed the same keyword
# twice and the first P19 attempt died on a ``TypeError`` - loud, but silent about which of the two to
# use.  An instruction the tool cannot obey is refused where it is written, not where it fails.
#
# Thirtieth raise, 2026-09-06: +13 in beidou_cli for a fourth layer of the same fault, surfaced the
# same way - by running the pre-registered experiment rather than by reading the code.  `_resolve_mined`
# re-derives a `mined_<hash>` id from a bare `enumerate_candidates()`, so a candidate mined at another
# interval's grids does not enumerate under the defaults and every command reports it gone: correct by
# that function's own contract, and useless to an operator holding the shortlist that had just produced
# it.  `--grids` therefore moves out of `mine` into `_common_options` and threads through `_entry`,
# because a mined id is addressable wherever a hand-written one is.  The root cause under all four
# layers is one thing: bar counts are scale-relative, and the tool treated the search space as a global
# constant.  The regression test picks a candidate proven to be outside the default space rather than
# the first one - `flow_windows` was not rescaled, so its family enumerates identically either way and
# `candidates[0]` would have made the gone-half pass for the wrong reason.
#
# 2026-09-06, +78 in beidou_alpha and +24 in beidou_cli: KILL-Q2/Q3, the two halves of "the ruler
# cannot tell a candidate from noise".  Q3 is one line of mathematics and its explanation: the D-028
# gate compared the OOS Sharpe against E[max of N nulls], and a single noise curve exceeds the
# EXPECTATION of the maximum about half the time - measured on the shipped report's own null, that
# gate admitted pure noise at 43.5%.  It is now the 1-alpha quantile (solve Phi(x)^N = 1-alpha), with
# `p_family` reported so a verdict can say how surprising the number is; `expected_max_sharpe` is left
# exactly as it was, because the DSR wants the expectation as a benchmark and only the GATE was wrong.
# N stays the raw ledger count rather than an effective number of independent trials: the ledger holds
# near-duplicates, so this is conservative, and inventing an N_eff estimator without a correlation
# structure would be guessing - recorded as owed, in the docstring, not silently assumed away.
# Q2 is annotation, not a new gate (the holdout half of the original prescription was withdrawn - it
# contradicted the operator's KILL-006 ruling): `oos_is_full_sample_tail` says when no fold had a
# choice to make, which is the shape every tsmom report has had since 2026-09-04, and
# `best_key_oos_sharpe` gives the shipped configuration its own walk-forward number instead of letting
# the fold-selected mixture stand in for it.  The lines are mostly the sentences that say why.
#
# 2026-09-06 B0, +52 in beidou_alpha and +4 in beidou_cli: the three debts the FWER commit shipped
# without and did not disclose (deep-analysis report 12.5, remediation plan DL-R1..R4).  Two of the
# three are subtractions dressed as additions.  DL-R1 DELETES a gate: the Newey-West t stops deciding
# anything and `VerdictThresholds` loses two fields, because on hourly returns the t is the Sharpe
# times the square root of years to within 0.1% - enforcing it counted the same evidence twice, and a
# "short-sample guard" that fails candidates is a second gate whatever the docstring calls it.  What
# survives is a completeness check (a report that never measured the t cannot pass), which is what
# keeps the 25 pre-D-020 archived reports out.  Pre-registered before the change and measured on all
# 47 archived reports: zero verdicts move.  The lines are the docstring saying why, and the test file
# holding the measurement.  DL-R2 puts `p_family` in the failure reason so a verdict can say how
# surprising a number is rather than only that it was below a line.  DL-R3 is the acceptance KILL-R25
# asked for and the FWER commit skipped: its own condition was "125 independent nulls clear the gate
# at most 5% of the time", which the threshold satisfies BY CONSTRUCTION - it solves Phi(x)^N = 1-alpha
# for exactly that null.  The test that means something uses correlated nulls, and establishes the
# property the docstring claims: with the ledger's duplication the gate is conservative, not merely
# correct.  `effective_trials` (Li & Ji) is reported and never substituted - an N_eff would LOWER the
# bar, and it is scoped to the run's own grid because the ledger stores Sharpes, not return series,
# which is the concrete reason a ledger-wide N_eff stays owed rather than guessed.
#
# 2026-09-06 B1 (DL-L2/L3), +105 in beidou_live and +18 in beidou_cli: the two halves of "an
# unattended loop's only output is its alerts".  DL-L3 deduplicates them - the repo's own logs held
# 36 identical FAIL lines over 36 hours, unhandled (KILL-R7), which is how the one alert that matters
# gets missed - and adds a second channel, because one URL is a single point of silence.  DL-L2 is a
# net DELETION of state: the breaker now alerts and exits 0, which under KeepAlive.SuccessfulExit=false
# is what stops launchd relaunching into the same wall every 60s, and `consecutive_errors` leaves the
# persisted LiveState entirely - carrying it across restarts is what made a tripped breaker trip again
# at once (L1-03 / KILL-R29).  No TRIPPED flag, no process-level backoff counter, no tests for either.
# The sequencing between them is load-bearing and enforced in code, not in prose: a clean exit is only
# taken when a channel actually accepted the alert; if none did, the original exception propagates and
# the loop fails loudly, because a book that vanishes silently is worse than one that hot-loops
# (KILL-P1).  Most of the lines are that decision and its reasons.
#
# 2026-09-06 B1 (DL-L1/L4/L5/L6/X1), the unattended-minimum batch.  The operator ruled on the
# governance question this raise exists to ask (plan Q2, 2026-09-06): raise and write the reason,
# rather than cut the batch, because no same-size deletion exists that does not remove a capability.
# What the lines buy, in the order the risk actually sits:
#
# DL-L1 is the one that matters today.  Seven worktrees on this machine, credentials sourced globally
# from ~/.zshrc, state_dir and kill switch on relative paths, and `live run` defaulting to non-dry-run:
# `beidou live run --allow-unvalidated` in any worktree was a second process trading the same account
# (KILL-R20).  An flock keyed to the API key FINGERPRINT - not the state directory, because every
# worktree has a different one and they all trade the same account - plus `--armed`, which makes real
# orders a sentence you write rather than one you must remember not to omit.  A refused instance exits
# 0: non-zero would have launchd relaunch it every ThrottleInterval and alert every time.
#
# DL-L4 splits what `--immediate` had fused.  Reconciliation always runs; only the REBALANCE is gated,
# on a window derived (grace + ThrottleInterval + measured startup) rather than picked - the 09-05
# draft's 120s would have turned a 143-second catch-up into a 57-minute stale book (KILL-R6).  A skip
# is recorded as its own cycle row, because L1-01's error was counting the fills that happened instead
# of the ones that should not have.
#
# DL-L5 stops the loop reaching for what is not its own: startup cancelled EVERY open order on the
# account (L1-09), the kill switch resolved against the working directory so a worktree CLI engaged a
# file the loop never read (L1-07), and `flatten` closed the book without taking the trading rights
# away, so the next cycle rebuilt it - the 2026-09-04 incident path (L1-06).
#
# DL-L6 is two small things that only matter while everything else is going wrong: fsync on the
# append-only ledgers, and a SIGTERM handler that finishes the cycle in flight (installed by the CLI,
# never by the library - signal handlers are process-global).
#
# DL-X1 came back from the A-P2 probe with the fact that shaped it: liquidationPrice is 0 for 14 of
# 14 longs and non-zero for 4 of 4 shorts, because a cross-margin long's liquidation price computes
# below zero.  0 means UNREACHABLE, not NOW; a distance metric that read it as a price would alarm on
# every long forever.  `min_liquidation_distance` therefore reports the closest measurable position
# AND how many have no reachable price - two facts that must never look like one missing number.  The
# margin-mode check asserts and refuses; it never sets.
#
# 2026-09-06 B2 (DL-A1), +~230 in beidou_alpha, all of it alpha: five expression nodes over columns
# the panel already carries, and one family each.  KILL-R28 drew the line they sit behind - a node
# needing a NEW panel field re-opens the "research panel superset of live panel" obligation and
# belongs in Phase B with the ingestion that feeds it - so these read close, quote_volume and trades,
# every one of which already flows to the live loop.  Abs gives magnitude without direction; Moment
# gives the skewness and kurtosis a mean and a variance cannot see; Semi lets "risk" mean the losing
# half only; Residual gives the part of a move the market did not explain; TradeSize is the one thing
# `trades` says that `quote_volume` does not - few large prints or many small.
#
# Residual is cross-sectional, so it takes its market from `panel.reference` exactly as
# CrossSectional does.  That is P1-01's contract inherited rather than re-opened, and it is the
# reason a cross-sectional node was allowed into this batch at all.
#
# The cost is stated rather than absorbed: the default space goes 267 -> 514 candidates (+45 surprise,
# +28 shape, +70 downside, +70 residual, +34 print size), so anything promoted after this pays a DSR
# denominator roughly twice as large.  The frozen 225 are untouched and every id already in the ledger
# still resolves - asserted, not assumed, by the hash regression in tests/alpha/test_panel_column_nodes.py
# and by the P14 space test, which now switches the new families off the same way it switches funding
# off.  Growth has to be a dimension that can be turned off, or a frozen space stops being one.
#
# 2026-09-07 DL-X1 the rest of it, +111 in beidou_live and +38 in beidou_exchange, and the sentence the
# rule requires has to start by correcting the entry above it.  The 2026-09-06 raise described what
# `min_liquidation_distance` and `margin_mode_problems` DO.  Both were true and neither was reachable:
# the two functions had zero call sites in production code, so `cycles.jsonl` carried no liquidation
# field, startup asserted nothing, and B4 was recorded in this session as "effectively complete" on the
# strength of a comment.  A tested function nobody calls buys observability the way a fire extinguisher
# in a locked cabinet buys safety.  These lines are the call sites: a per-cycle `min_liq_distance` in the
# record, the margin refusal next to the hedge-mode refusal in `startup()`, `force_orders()`, and
# `margin_mode()`.
#
# Three things the wiring found that the arithmetic could not.  (1) `marginType` comes back lowercase -
# `cross`, all 18 open positions, demo-fapi 2026-09-07 - so the obvious `== "CROSSED"` assertion would
# have matched nothing; the check reads the `isolated` boolean instead, because a spelling change is
# silent and a missing boolean is loud.  (2) `asset_vol` covers the universe, so a foreign position has
# no volatility estimate, and "no estimate" was being counted as "no reachable liquidation price" - the
# exact conflation the `unreachable` field was invented to prevent, reappearing one level up; there is
# now a third bucket, `unmeasurable`.  (3) positionRisk returns 736 rows against a universe of 18, so the
# refusal is scoped to symbols the book can actually trade, or it would refuse to start over a contract
# no order will ever be sent for.
#
# +38 in beidou_exchange is two endpoints and their docstrings.  A-P2's unprobed half is now probed:
# GET /fapi/v1/forceOrders answers on demo-fapi and returns [] for an account that has never been
# liquidated, which is the useful answer - it is what separates "no liquidations" from "we never
# looked", and the loop had been in the second state for its whole life.
#
# +32 of the beidou_live figure are M-Q06's failure action, and they are here rather than deferred for
# the reason the whole entry above exists: this session's finding was a tested function with no caller,
# and a threshold nothing evaluates is the same defect one layer up.  The plan left M-Q06's baseline
# "unmeasured"; it is measured now - the nearest reachable liquidation on the live account sits 242
# daily-vol units away (TUTUSDT, 2026-09-07), 24x the 10-unit floor - which is also what makes wiring
# the alert safe rather than a new hourly noise source.  It alerts and does not trade, the separation
# `risk_budget` already makes.
#
# 2026-09-07 B3, the accounting protocol mechanised: +83 in beidou_alpha, +103 in beidou_live, +277 in
# beidou_cli.  Three things the ledger could not do.
#
# DL-K1, the signature.  `(param_key, range_start, range_end, symbols)` cannot see the construction the
# weights were built under, the overlay applied to them, WHICH symbols those were rather than how many,
# or how wide the search a mined id came from - so P10 cell B moved the no-trade band, re-priced every
# weight in the book, and the ledger recorded a replay.  Four optional fields, defaulting to "" so the
# 145 archived rows still read; a legacy row never folds into a modern one, because folding would assert
# that the old run used today's construction, and a ledger claiming knowledge it does not have is worse
# than charging a trial twice (KILL-P5).  Extending `signature` without extending the exclusion set
# `dsr_inputs` builds by hand silently double-charges every replay - caught by a test written for it,
# which is the only reason it is not in the shipped code.
#
# DL-K1 also moved the ledger's address.  It was `Path(--out) / "trials.jsonl"`, and `--out` is a flag
# passed for ordinary reasons, so pointing the reports at a scratch directory charged the run to a fresh
# empty book.  Fixed now; moving it takes an environment variable whose only possible purpose is to not
# be charged, and the test suite sets that variable autouse - the failure mode of forgetting is silent
# and permanent, since an append-only ledger cannot have a bad row taken back out.  `backtest` and
# `overlay` join `validate` and `book` in paying: they evaluate configurations exactly as much, and a
# search whose exploratory arm is free is a search whose denominator is wrong in the flattering
# direction (E-15).
#
# DL-K2, the search pays for itself.  Every candidate a `mine` round keeps is charged under one key, so
# `--prior-trials 514` stops being a number retyped off a terminal - which is how P20's got there.  The
# space version is deliberately NOT stamped on those rows: a candidate examined in a 267-wide search and
# again in a 514-wide one is one hypothesis looked at twice, and stamping the width would charge 781 for
# a family of 514, inflating N in the direction that looks rigorous and is wrong.
#
# DL-K3, the ordering.  A pre-registration written after the result is not one, and nothing checked.
# Two orderings because there are two kinds of strategy: a hand-written one is registered by name, so
# the earliest log commit mentioning it must predate its report; a mined one CANNOT be, since its hash
# is derived from the search, so what must precede it is the mine that enumerated it.  Run against the
# archive it produced nine FAILs and not one was a finding, so it carries C-P6's boundary like the rest
# of this batch: it judges reports produced after it landed, states how many it declined to judge, and
# never skips silently.
#
# One hole was found while closing the other.  `reports/research/trials.jsonl` is a RELATIVE path, so
# fixing `--out` closed the loophole a flag opens and left the one a `cd` opens: run from
# `beidou_alpha/` and it names a file that does not exist, so the run is charged to a fresh empty book
# and reports `ledger_trials: 0` - KILL-Q5's exact state, reached by walking instead of typing, and the
# harder of the two to trip over deliberately.  The address is anchored to the checkout now (a
# worktree's `.git` is a file and still a root), which is the L1-07 fix applied one file over.
#
# 2026-09-07, same batch, two coverage holes of one shape found while using it.  `ledger_scope` reached
# `validate` and not `research book`, so a mined sleeve promoted through `book` did not pay for the
# search that found it while the same sleeve promoted through `validate` did; and DL-K3 globbed
# `*-validation-*.json` only, so a candidate taken through `book` skipped the ordering check entirely.
# Both are the same sentence: a protocol whose coverage depends on which command an operator happened
# to run is not a protocol.  Cheap to fix, and worth the lines because neither would have announced
# itself - the accounting would simply have been lighter down one path.
# `daily_alerts` moved the daily report's alert assembly out of the CLI and into `reports.py`, so the
# two ceilings move together: +45 on `beidou_live`, -20 on `beidou_cli`.  A relocation must not buy
# headroom - lowering the origin is what keeps the ratchet from drifting up one move at a time.  The
# net +25 is the docstring that now carries WHY a construction-cadence count no longer pages, which is
# the part of this change most likely to be undone by someone who only sees the code.
#
# Merged 2026-09-07 from two sessions working in parallel, and the arithmetic is worth one line
# because `beidou_cli` goes DOWN across the merge: 3,298 before either change, -20 for the
# relocation above, +13 for the coverage fix, 3,291.  Neither side bought headroom, which is what
# the relocation note asks for and what re-measuring the merged tree is the only way to confirm.
#
# 2026-09-07, L1-07 for real this time, +~35 in beidou_live and +~20 in beidou_cli.  DL-L5 fixed the
# half that lived inside one process - two call sites each building the path from the raw profile
# string - and its own docstring named the half it did not fix: `Path(relative).resolve()` anchors
# to `os.getcwd()`, so `beidou live kill-switch` from a worktree wrote a file the loop never reads,
# printed "kill switch engaged" and exited 0.  Measured against the running loop, not inferred:
# the loop reads <repo>/.beidou/live/KILL_SWITCH and the worktree CLI resolved to
# <worktree>/.beidou/live/KILL_SWITCH.
#
# The fix is DL-L1's sentence a second time: what must be unique per ACCOUNT cannot be addressed by
# a path relative to a working directory.  The switch now sits beside the instance lock under the
# same account fingerprint.  Both files are written on engage and both cleared on release, and the
# guard reads their union - an emergency stop may never get weaker, not even for the one restart it
# takes a running loop to learn the new path.
#
# 2026-09-07 AC-L4's other half, +~45 in beidou_live.  DL-L4 gave every restart two facts - how
# late the wake-up was relative to the bar close, and whether that cost a rebalance - wrote both
# into the cycle row, and the acceptance criterion asks for them in the DAILY REPORT, where an
# operator would see them.  Checked against the real report: neither key was in it.  RISK-P2
# ASSUMED a deployment restart costs late fills and a rebalance; these lines are what turn that
# assumption into a number.  The counter is a running total held on the engine and resets per
# process, so summing the column double-counts - max per stretch, summed across stretches, which
# is the part a later reader is most likely to "simplify" back into a sum.
#
# 2026-09-07, the operator ruled one alert channel rather than two, which makes `send()`'s return
# value the whole of RISK-P1 rather than half of it - and that value was `status_code < 300`
# against a Lark custom bot, which answers HTTP 200 with `{"code": 19001}` when it cannot read the
# payload.  Worse, the payload sent was Slack's flat `{"text": ...}` and Lark documents
# msg_type/content, so the one channel may have been silently dead since it was configured; the
# logs show no alert ever attempted, delivered or rejected, so nothing contradicted that.
#
# +~55 live for `payload_for` and `accepted` (believe the provider when it states a result, keep
# trusting the status when it does not - it can only turn a false success into a failure), +~60 cli
# for `beidou live alert-test`.  AC-L3 asks for a drill alert and there was no way to send one: the
# only path to the channel was a real failure, so the signal the breaker's clean exit depends on
# could not be exercised without first breaking something.  The drill redacts the URL to its host,
# because a webhook URL is a credential - whoever holds it posts as the bot.
#
# 2026-09-07, the L1-07 fix was itself incomplete, found by scanning the repo for a third copy of
# the alert bug rather than by anything failing.  THREE places ask "is the kill switch engaged":
# the engine's guard, `WriteGuard` at the HTTP layer, and the CLI.  The first fix changed one.
# `WriteGuard` is the one that refuses a risk-adding order at the wire, and `live flatten` - the
# command whose entire purpose is to stop - engaged through the single-path helper.  Widened all
# three, and the startup banner now names every path it reads, because that banner naming ONE file
# is what exposed the original defect.
#
# The scan itself is the lesson worth keeping: two of this repository's three "did it work?"
# judgements were already right (the venue client reads Binance's body `code` even on a 2xx; the
# archive verifies a checksum, not a status).  The wrong one was the newest.  Finding the third
# copy cost one grep; not finding it would have cost a stopped book nobody was told about.
#
# +~15 cli, the last thing the scan turned up and an adjacent family rather than the same one:
# DL-L6 flushed and fsynced `.beidou/live/*.jsonl` because "unlikely is not what an append-only
# ledger is for", and left `reports/research/trials.jsonl` - the same contract, and the more
# consequential file, because it IS the DSR denominator.  A row lost to a crash makes N smaller,
# and a smaller N flatters every verdict computed after it.
#
# 2026-09-07, two clauses of the plan that had never been delivered, found by reading §6.1 against
# the code rather than by anything failing.  +~40 live, +~15 cli.
#
# DL-L3 asks for dedup AND `run_check.sh` 同源去重.  KILL-R7's evidence was 36 identical FAIL lines
# over 36 hours, and those came from the HOURLY CHECK JOB - a fresh process each hour, whose
# in-memory dedup dict is empty every time and can suppress nothing.  The dedup shipped in B1 was
# therefore aimed away from the case that produced the finding.  There is now one dedup file beside
# the lock and the kill switch, shared by the loop, `report daily` and the check job.  It is a cache
# and never a ledger: every read and write of it may fail quietly and cost at most one duplicate,
# because it must never be the reason a message does not go out.
#
# DL-L5's last clause asks that a non-empty `foreign_positions` at startup 告警而非静默.  It logged.
# Those are open positions on the venue the loop has decided not to manage, and a line in a file
# nobody reads is exactly what "silent" means to an operator - L1-06's whole family.
# (+4 cli: the paragraph on why `alert-test` deliberately does NOT join that shared dedup file - a
# drill run twice in an hour must send twice, or the instrument reports a failure the second time
# and teaches the operator to distrust it.  Added after the ceiling above was measured, which is
# how a red tree reached main for one commit; the ratchet caught it on the next run.)
#
# 2026-09-07 AC-L5, +~15 live.  The FILTER has been right since DL-L5 and is now verified against
# the real venue: a hand-placed `manual-acl5-…` limit order survived a real `startup_reconcile`
# with cancel_stale_orders=True, and was cancelled cleanly afterwards.  Telling anyone was the
# missing half - the same shape as the foreign-POSITIONS clause fixed hours earlier, and found
# the same way.  A resting order the loop did not place is either the operator's or the leftover
# of something that crashed, and both are better heard at startup than discovered in a fill.
#
# 2026-09-07 DL-D2, the research half of the metrics ingestion: +~250 beidou_data, +~25 live,
# +~35 cli.  The order is the evidence's, not a preference - the entry probe found that the daily
# archive and the REST window carry the SAME numbers under DIFFERENT stamps (archive create_time
# == rest timestamp - 5min, 166/166 exact at that offset and 0/165 at any other), so a join on
# equal timestamps is a five-minute look-ahead in 100% of buckets, always favourable.  Hence
# `beidou_data/metrics.py` before any downloader: ONE canonical stamp, and the look-ahead made
# unrepresentable by deciding what a bar may read from the bucket's CLOSE.
#
# Which stamp is which was measured too, because guessing there would be the same mistake one
# level down: re-reading the three newest REST buckets six minutes apart returned byte-identical
# values, so the newest row is COMPLETE and its stamp is the close.  No partial-bucket hazard.
#
# The gate is why the ingestion is safe rather than a hazard.  Research eats the T+1 archive and
# live can read only the 30-day REST window, so ingesting and letting a signal use it is KILL-027
# in its purest form - a research panel strictly larger than the live one, arriving silently as a
# strategy that backtests well and trades on nothing.  `metrics_refusal` refuses to start any
# strategy declaring `needs_metrics` until a LIVE source covers the bars it needs.  It costs
# nothing to everything shipping today, because nothing shipping today declares it.
#
# 2026-09-07 DL-Q6, the same-source contract: the loop records the metrics IT could read, and the
# archive becomes something to check that recording against rather than something to trade on.
# +~110 data, +~45 live, +~5 cli, +3 alpha (a `needs_metrics` predicate on SignalSpec, the same
# shape `uses_funding` already had for exactly this question).
#
# The design decision worth the lines: there is NO separate five-minute daemon.  The granularity
# that matters belongs to the bucket, not the poll - the REST window is 30 days deep, so one poll
# an hour retrieves all twelve buckets of the past hour with nothing missed - and polling at the
# bar close does something a daemon cannot: it records exactly the buckets that had CLOSED when
# the loop decided, which is the set `align_to_bars` selects.  A daemon would have bought a second
# process, a second failure mode and a second thing to restart, in exchange for a superset of the
# same rows recorded at instants no decision was made at.
#
# Two directions chosen deliberately, because both could have gone the flattering way.  Parity
# reports `rate: None` when nothing overlaps rather than 1.0 - zero disagreements out of zero
# comparisons is not agreement, and calling it perfect is how a dead stream would look healthiest
# exactly while it stopped recording.  And coverage is counted from buckets HELD, not from
# first-to-last span: a span treats an outage as covered, and for a gate that is the wrong
# direction to be wrong in.
# Raise 2026-09-07, with the sentence the rule requires: +12 in beidou_alpha and +1 in beidou_live for
# `ExitParams.unit_mode` (EXP-EX3): the k-units of the exit overlay can now be measured in this bar's sigma
# instead of the entry bar's, which is the cheapest test of "adaptive" exits the operator asked for, and the
# construction fingerprint records which unit a cycle ran under so an adoption cannot be silent.  Measured
# rather than estimated, the alpha delta is +24, not +12: `_unit_price` carries a second docstring paragraph,
# beyond the plan's draft, stating why the denominator is always `entry_price` and never the current price,
# so a later reader cannot "simplify" the two reference points back together.  The live delta lands exactly
# on the plan's +1.
# Raise 2026-09-07, with the sentence the rule requires: +77 in beidou_live and +2 in beidou_cli for two
# observations the exits analysis found missing - a noise scale (design daily sigma in USDT, so a 65 U
# giveback reads as 0.4 sigma rather than as a feeling) and M-005's promised 24/72h counterfactual, shipped
# as monitoring with its own n-for-decision, because at 1.9 exits a week it cannot adjudicate in 30 days.
# Measured rather than estimated, the beidou_live delta is +149, not +77: `noise_scale`, `exit_counterfactuals`
# and their three module constants alone are 119 lines, the two new `daily_markdown` sections add another 22,
# and the `daily_payload` signature, its two new dict keys and the three import lines the fix needs are the
# remaining 8 - the brief's own shown implementation already summed to this much once copied through, so +77
# undercounted the code it specified rather than describing scope added during implementation.  The
# beidou_cli delta lands exactly on the brief's +2.
#
# Raise 2026-09-07, second one the same day, with the sentence the rule requires: +2 in beidou_live for
# a two-line comment.  Commit 25af442 fixed the review's Important finding on `exit_counterfactuals` -
# a dry-run cycle's `exit_events` were priced as real, because the function walked `cycles.jsonl` raw
# instead of through `_cycles`'s dry-run/no-equity filter - at zero net lines, reasoning in its commit
# message that the new regression test's docstring carries the "why" so the call site did not need to.
# The fix review that commissioned this raise asked for the reasoning at the call site as well, in this
# file's own convention of explaining WHY rather than leaving it to a test alone; this adds that comment
# without touching the already-shipped, already-tested filter condition itself.
#
# Raise 2026-09-07, third one the same day, with the sentence the rule requires: +16 in beidou_live, every
# line of it `ruff format` wrapping and none of it behaviour.  The DL-EX0/0b commit landed four over-long
# expressions unformatted - `noise_scale`'s exits sum, and three `_fmt_num` calls in the two new
# `daily_markdown` sections - and `ruff check` never said so, because the lint config ignores E501 and
# leaves line length to the formatter, which nothing had run over that commit.  Recorded rather than
# absorbed: the ceiling counts lines, not capability, so formatting the tree back to the config's own
# style has to cost a sentence like anything else, and the honest sentence is that these 16 lines bought
# consistency and nothing more.  The number is read off `_lines`, not computed as 5,329 + 16: both raises
# above estimated and both came in short (+77 measured +149, +12 measured +24).
#
# Raise 2026-09-07, fourth one the same day, with the sentence the rule requires: +19 in beidou_live and +2
# in beidou_alpha for this branch's one fix wave, and nearly every line of it is a comment or a docstring.
# The behaviour changes underneath are edits to expressions that already existed: `noise_scale` counts real
# exits over the evidence window instead of every `exit_events` row over a 720-bar tail, `exit_counterfactuals`
# walks its horizon from `as_of_ms`, `exit_and_pool_events` drops dry-run rows, and one `except` widens.  What
# costs the lines is the reasoning that stops each of them being "simplified" back: why COOLDOWN is not an
# exit (`exit_step` returns it once per *blocked* cycle, so at `cooldown_bars: 24` one take-profit reported as
# 25 against a rate built from take-profits and stops), why the two exit counts have to share one window (they
# agree only until the evidence window outgrows 30 days, which is the state K-EX14 is working toward), why the
# counterfactual anchors on the bar the price came from rather than the host clock D-025 caught an hour behind
# the venue, and why a truncated parquet must not stop a monitor that runs unattended.  The beidou_alpha +2 is
# one clause in the exit overlay's module docstring, which still called the k-unit the volatility "at entry"
# unconditionally after `unit_mode="current"` existed - in the docstring of the single source of truth that
# backtest and live share.  Measured with `_lines`, not estimated: alpha 5,834, live 5,364, and the other four
# packages did not move.  The two raises above this one on this branch both estimated and both came in short;
# this one asked the function.
# Merged 2026-09-07: two branches raised this dict in parallel and the resolution is neither side's
# numbers nor the larger of each - it is the merged tree re-measured, because a ceiling copied across a
# merge asserts a count nobody took.  Both raises above stand as written; only the dict below is new.
# Raise 2026-09-07, with the sentence the rule requires: +40 in beidou_live for L1-04.  M-Q08 is one of
# the two criteria the demo phase is judged by, and its slippage clause was measured by an instrument
# pointed at the venue mark - an index price sampled when the cycle woke - rather than at the price the
# backtest enters at, then compared against a 10 bps gate whose own comment sourced it from the 7 bps
# turnover cost (5 fee + 2 slippage) while the instrument excludes commission.  Fee-inclusive budget,
# fee-exclusive measurement, 2.5x the stated bar: the measured +4.3 bps was outside M-Q08's 4 and
# comfortably inside 10, so the gate reported green and could not have done otherwise.  Most of the 40
# lines are the docstrings and the config comment recording those two disagreements, and the refusal of
# the retired `max_slippage_bps` key - the parts that stop a later reader from "simplifying" the
# reference back to the mark or re-adding a standalone threshold.
# Raise 2026-09-07 (second today), with the sentence the rule requires: +54 beidou_live, +8
# beidou_exchange, +5 beidou_shared, for L1-13 and L1-10 - two findings the quality report recorded and
# nobody had contracted.  L1-13: `--dry-run` shared `paths.state_dir` with the real loop, so a rehearsal
# appended to the live cycles.jsonl and rewrote heartbeat.json, which `live status --check` reads to
# decide whether the loop is alive - a rehearsal could make a dead loop look fresh.  The directory is now
# derived (`<state_dir>-dry-run`) and `build_store`'s `dry_run` is REQUIRED, so a caller cannot forget it
# the way one had.  L1-10: `/fapi/v2/account` carries a per-asset breakdown that was parsed away, so on
# multi-assets margin nobody could tell how much of a drawdown reading was BTC collateral rather than the
# book.  It is recorded and printed, NOT subtracted from what the book sizes on - that denominator is a
# construction decision that would reset M-010's window, and it belongs to the operator, not to a fix.
# Raise 2026-09-07 (third today), with the sentence the rule requires: +97 in beidou_live for the edge
# decay rule the operator adopted from report 4.2 Ⅰ.  The report proposed a statistic and asked which one
# to use; the question sat unanswered, so nothing measured decay at all.  Most of the 97 lines are the
# docstrings pinning the three things that would otherwise loosen the rule after the fact - windows must
# not overlap, the comparison is to an empirical distribution rather than to oos_sharpe with a normal
# standard error, and a missing q10 is INSUFFICIENT_DATA rather than OK.  Written before any live window
# exists, which is the point: a decay rule authored after seeing the decay is not a rule.
# Raise 2026-09-07 (fourth today), with the sentence the rule requires: +60 beidou_alpha, and beidou_live
# comes DOWN 13.  `window_sharpes` moved out of beidou_live into beidou_alpha/validation/metrics.py,
# because the import direction forbids alpha depending on live and the evidence run is the right place to
# emit the decay rule's comparison distribution: a q10 written beside the registry by hand would go stale
# silently at the next construction change (the D-026 failure, twice already), while one emitted from
# `walk_forward.summary()` cannot describe a construction other than its own.  The move also puts a pure
# statistic in the package the effort target is measured on, which is the right direction for once.
# The window length is now a single constant, DECAY_WINDOW_DAYS: the first draft had the evidence side on
# `bars_per_year / 12` (730 hourly bars) and the live side on 30 days (720) - ten bars that read as
# rounding but sit on the two halves of one comparison, where windows of different lengths carry
# different Sharpe dispersion.  Caught by a test, not by reading.
#
# Raise 2026-09-07, with the sentence the rule requires: +47 in beidou_alpha and +16 in beidou_live for
# Task 7 / EXP-EX2, the regime-switched take-profit 2x2 (pre-registered as P22b).  `ExitParams` gains four
# fields (`regime_window`, `regime_er_cut`, `regime_tp_scale`, `regime_side`) and their `__post_init__`
# guard, plus two module functions: `efficiency_ratio` (Kaufman's net-move-over-path-length, the standard
# choppy-vs-trending measure) and `regime_tp_scale`, which turns that ratio into a per symbol-bar
# multiplier on `take_profit`; `exit_step` and `apply_exits` thread the multiplier through.  The reason
# this earns its own instrument rather than folding into an existing one: the 2x2 asks whether tightening
# take-profit ONLY when the market is inefficient beats tightening it always, and answering that requires
# research and the live loop to agree, bar for bar, on what "inefficient" means from the exact same
# efficiency ratio - so the cut is a pre-registered CONSTANT (168 bars, 0.05), never a rolling reference.
# A rolling median would make research (millions of historical bars) and live (~1,442 held bars) compute
# different regimes off the same configuration, which is KILL-027's shape; `regime_window: 0` stays the
# default for exactly that reason.  `beidou_live` carries the mirrored per-bar scale in `ExitOverlay.apply`
# (one `regime_tp_scale` call over the bar's own closes) and the four fingerprint keys
# `construction_fingerprint` now records, so a cycle can state which ER regime it ran under instead of
# leaving it implicit.  Measured with `_lines`, not the brief's drafted +45/+12: beidou_live lands 4 over
# the brief's own shown diff, all of it `construction_fingerprint`'s four new keys, which the brief's shown
# live diff did not total separately from the per-bar scale in `exits.py`.  beidou_alpha needed a second
# measurement after the first: `ruff format` wrapped the over-long `regime_er_cut` field comment and the
# `np.where(...)` call in `regime_tp_scale` onto extra lines the brief's inline draft did not show, which
# is the gap between the first reading (+43, 2 under the draft) and the number below (+47, 2 over it) -
# the same shape a raise above already recorded once (the one whose whole delta was "every line of it
# `ruff format` wrapping and none of it behaviour"): format before measuring the ratchet, not after.
# Merged again 2026-09-07 (P22b/P23 follow-up).  Same rule as the merge above: both raises stand as
# written, and the dict is the merged tree re-measured rather than either side's numbers.
# Raise 2026-09-07 (fifth today), with the sentence the rule requires: +68 in beidou_live for the
# construction-identity mechanisms the operator ruled on after restart #5.  The fingerprint's own field
# set grew TWICE in one day - P22's `exits.unit_mode`, then P23's four `exits.regime_*` - and each time
# the digest moved while every construction value was identical, so `evidence_window` read a changed
# ruler as a changed book and reset M-010's window to one bar.  Nothing failed, which is why it happened
# twice.  The lines buy three things: a `payload_version` reported OUTSIDE the hash (inside, introducing
# it would move the digest, which is the bug it explains), an alias table declaring which digests are
# the same book - in code, so the claim takes a commit that carries its proof - and the readers
# (`evidence_window`, `realised_vol`) comparing through it.  History is never rewritten: old rows keep
# the digest they were written with.
# 2026-09-08, the external backtest-guard audit's remediation: +99 alpha, +60 live, +103 cli.  All of it
# is one shape - a number that outlived the thing that produced it - and the lines are the four places
# the identity now travels WITH the number instead of beside it in prose.
#   cli (+103): `research validate` replays the book guards and applies the exit overlay, and records
#     both in the report.  Every `run_backtest` call site but one ran guard-free and every one but one
#     ran exit-free, neither of them in `validate`, so the artefact the registry cites and whose sha256
#     the startup gate pins described the signal layer of a four-layer book.  Measured on the shipped
#     configuration: OOS 1.7662 cited against 1.8492 held.  Also the two report blocks the same audit
#     asked for - a slippage stress that holds the taker fee fixed, and the other execution convention
#     priced as a comparator.
#   alpha (+99): `oos_selection` carries the NAME of the gate that produced its threshold and `decide`
#     refuses one it cannot name (KILL-Q3 replaced E[max] with the quantile three hours after the cited
#     report was written, and the stored 1.1446 is the retired gate's answer against today's 1.4684);
#     `construction_problems` compares the two overlay blocks; `slippage_levels` / `slippage_stress`;
#     and two conventions that were correct and unwritten - `sharpe`'s zero risk-free rate and the half
#     of "conservative" that `open_to_close` does not earn.
#   live (+60): `risk_budget_status` reports BLIND rather than OK when a criterion has no reading (the
#     2026-09-07 daily report said OK while M-Q08's slippage instrument had zero usable fills), the
#     ladder's drawdown carries the share of equity that is collateral rather than the book, and
#     `live_overlay_blocks` hands the gate the loop's own guards and exits.
# The ratchet caught all three, which is it working: none of this was budgeted for.
# 2026-09-08, the operator's ruling that the Lark channel is Chinese: +16 live.  The alert text itself
# is a rewrite, not growth - the 16 lines are `guards.REASON_ZH` and `describe_guard_reason`.  The guard
# REASON CODES stay English because `cycles.jsonl`, the report and these tests read them and a record
# that changes meaning between commits is worse than a bilingual alert; the gloss is the alternative to
# translating the codes themselves, and it lives beside them so a new code cannot be added without
# seeing that the operator's channel needs a word for it.
# 2026-09-08, M-015's limit re-derived with the probe book in place: +23 live, all of it prose.
# `RISK_COMPRESSION_LIMIT` moved 0.50 -> 0.76 and the three lines that MOVE are the constant, the
# alert's trailing clause and one docstring sentence; the rest say why, because the number they
# replace was declared in its own comment to be "not a derived threshold" and the next reader has to
# be able to tell a derivation from a second guess.  Deliberately NOT copied here in full: the method,
# the counterfactual and the 88-cycle table live in `scratchpad/m015_recalibrate_with_probe.py`, whose
# docstring was written before it ran, so a correction is made in one place.  What the prose buys that
# a pointer would not: the two anchors (0.581 working / 1.000 deleted), the pre-registered 1.5
# separability gate they passed, and the escalation rule - if this alerts again from the two books
# cancelling rather than from stage 1, replace the instrument instead of raising the number, which is
# the failure this file exists to stop.  The two raises above met in a merge, so this ceiling is the
# merged tree measured rather than either branch's number carried over.
# 2026-09-08, `research mine` saying what it charges: +23 cli, all of it the corrected docstring and one
# echo.  The docstring's first sentence was "Nothing here touches the trials ledger", which DL-K2 made
# false on 2026-09-07 - every kept candidate is a `mined` row - and the next day's P20 recovery re-ran
# the space over 24 more bars, appended 514 rows and moved the family's prior 514 -> 1,028 with nothing
# on the terminal saying so; that session had told the operator twice that `mine` was free, having read
# the docstring rather than the code.  The echo reads the family's distinct prior back off the ledger
# through `unique_trials` - the fold `research validate` applies - before and after the run, so the cost
# of a search is stated where it is incurred instead of discovered at the next validation.  The charging
# rule is untouched: rows fold on the full signature, data range included, and whether a replay's rows
# stay is a ruling (K-EX07's shape), not the command's.  Prose against the 90% target, like most of this
# page, buying a number that was true and unstated.
# 2026-09-08, the same number in the artefact: +3 cli.  The raise above put the family's prior on the
# terminal, and the terminal is gone when the window closes; the report is what the log cites, and the
# 2026-09-08 report recorded `charged: 514` - what the run did - with nothing about what the family cost
# afterwards, which is the number the next validation reads.  `ledger.family_prior` carries before and
# after through the same `unique_trials` fold.  Three lines, two of them the comment saying why.
# 2026-09-08, a new package arrives with its own ceiling: `beidou_governance` at 1,049 for Phase 0
# (policy 135, lifecycle 279, replay 615, __init__ 20) plus +112 in beidou_cli for `governance replay`.
# The sentence the rule requires, and the honest accounting for the 90% alpha target: none of this is
# signal.  It buys the subject of the sentence "and therefore it goes live" - until now every promotion
# was a person editing a YAML, and Q9 (2026-09-08) ruled that the machine does it from the first
# transaction.  `replay` is the biggest of the three and most of it is the exception register: seven
# operator rulings, each with the rule it breaks and why it should NOT become a rule.  That prose is
# the deliverable rather than commentary on it - a register that only recorded what happened would
# admit anything, and the "why not encoded" field is what a later reader has to argue with before
# turning a named exception into a general permission.  It is also what AC-G0 grades.
# The cli half is `governance replay` and the git reading behind it: which reports the registry has
# cited and when.  That lives in the cli rather than in the package on purpose - it is a fact about
# this checkout's history, and keeping `subprocess` out of `beidou_governance` is what lets the replay
# be tested against fixtures instead of against whatever `git log` prints today.
# Same day, +13 gov / +1 cli: the replay counted RAW construction digests and reported six changes where
# four happened - `CONSTRUCTION_ALIASES` already declares that `unit_mode` and the four `regime_*` moved
# the fingerprint without changing behaviour.  The alias map is passed in from the cli rather than
# imported, so `beidou_governance` still depends on alpha and shared only and `beidou_live` stays free to
# record the policy digest later without a cycle.  Worth naming the direction of the error: it made the
# operator's record look WORSE than it was, which is the direction a self-assessed audit is least likely
# to question.
# 2026-09-08, DL-G9 - the Phase 0 replay's own finding, fixed: +37 alpha, +54 live, +51 cli, +46 gov.
# The sentence the rule requires.  Two of `candidate -> validated`'s four conditions read fields that
# nothing wrote, so §3's state machine could not admit ANY candidate - not a strict gate, an inert one.
# `evidence_construction_digest` is one digest over exactly what `construction_problems` compares, and
# both sides now record it: `validate` over the blocks it replayed, the loop over the config it holds.
# Deliberately NOT `construction_fingerprint` - that covers throttle, leverage and `strategy_weights`,
# which a backtest has no opinion about, so a "matching digest" built from it would be a rule that
# always fails.  The live half also writes `construction_full` once per process, which is once per
# possible change: a construction can only move at startup, and the startup heartbeat that held the
# payload was overwritten by the next start, which is why the replay could attribute none of the six
# construction changes it found.  `--prereg` records the commit's OWN time, not the moment it was typed.
# Most of the alpha and live additions are the two docstrings saying why the digest is the intersection
# rather than the fingerprint; delete either and the next reader "simplifies" them into one.
# 2026-09-08, DL-C1 (KILL-A / KILL-Q12): +71 alpha, +11 live, +30 cli for the impact cost model.
# The sentence the rule requires.  Every number in this repository's backtests is scale-free, and that
# is arithmetic rather than a finding - gross P&L, turnover cost and funding are all linear in the
# weights, so scaling every weight by k leaves the Sharpe identical to the last digit.  The flat 7 bps
# is what makes it true, and it is the assumption real capital has been held out of scope on since
# 2026-09-05.  `ImpactModel` charges the square-root law on top, off by default (`capital: 0`), so every
# archived report still reproduces exactly - the flat model is this model's own limit rather than a
# separate branch, which is asserted rather than asserted-to-be-obvious.
# Most of the alpha addition is the docstring saying what the coefficient IS: an assumption from the
# equities literature, E5 for this venue, which this system cannot calibrate because its only fills are
# demo.  Delete that paragraph and the next reader reads a capacity curve as a cost forecast.
# Measured with it (scratchpad/impact_capacity_curve.py, coefficient 1.0, the four-layer book on the
# point-in-time universe): net Sharpe 1.9161 flat -> 1.8670 at 100k -> 1.7609 at 1M -> 1.4251 at 10M,
# with impact at 23% / 48% / 75% of total cost.  This one grows beidou_alpha, which moves the effort
# share the intended way.
# 2026-09-08, Phase 1's remaining four items: +32 alpha, +6 live, +64 cli, +178 gov.
# The sentence the rule requires, item by item.
# R0/DL-G1: the report carries BOTH trial calibers - the strategy bucket that gates, and the whole
# library that does not.  "Which N" was the most consequential open choice in the governance rules and
# an artefact carrying only the chosen one cannot be used to re-open it.  A test injects a hostile
# whole-library block into all 48 archived reports and asserts not one verdict moves.
# R2: `research mine` refuses a search space it has already enumerated, with `--reauthorize` as the
# named reopen path.  Explicitly NOT what the plan asked for - it said make `search_space_version`
# mandatory, and that field is deliberately empty on the `mined` bucket's rows because a candidate seen
# in a 267-wide search and again in a 514-wide one is one hypothesis looked at twice; stamping the
# width on would charge 267 + 514 for a family of 514.  The rule's intent needs the space recorded
# where the NEXT run can read it, so `SearchResult.space_digest` (the set of canonical hashes, not the
# count) goes in the shortlist report and the refusal happens before the scoring starts.
# R1/budget.py: the window's trial budget, read off the ledger rather than remembered - a counter in
# memory is already wrong once a parallel session's `validate` returns, which is the 2026-09-08 shape.
# state.py: `governance_state.json`, kept out of `lifecycle` so the state machine stays a pure function
# of state and event and the replay cannot accidentally read today's disk.
# R9: the loop records `policy_digest()` every cycle.  KILL-Q15 was a registry edited on disk while the
# loop held the old model for 96 cycles; a policy edited on disk is the same failure with promotions
# attached, and the difference is that nobody would be looking.
# 2026-09-08, the instrument audit question 3's ruling owes: +43 in beidou_live.  The operator ruled
# that the weights' denominator stays TOTAL equity - it is the venue's own margin basis, and under cross
# margin the collateral does absorb losses - which makes the pro-cyclical amplifier a named ACCEPTED
# risk rather than an oversight: every weight is a fraction of an equity that is 52% non-USDT, so
# collateral up 10% is every target notional up 5.2%, and the backtest models no collateral at all.
# D.8 asks an accepted residual risk to carry a compensating control, and this page's own recurring
# lesson is blunter: a threshold nothing measures is just a sentence.  `collateral_drift` splits the
# window's equity change into the book's attributed P&L and collateral repricing - measured on the
# first 23 cycles that carried a reading, 73% of a -60.11 move was repricing.  Reported, never
# subtracted, and it does not page: subtracting it would silently produce the USDT-denominator book the
# operator did not choose, and paging on it would page on a standing fact about the account.
# 2026-09-08, Phase 2 (scheduler + transaction + canary): +395 gov, +183 cli.  The sentence the rule
# requires, and the honest note that none of it is signal - it is the machinery that makes a promotion
# something other than a person editing a YAML, which is what Q9 ruled the machine must do instead.
# `promote.py` departs from the plan's ordering on purpose: the plan said write, restart, let the
# startup gate refuse, roll back; this asks the gate IN PROCESS before any restart, so a bad write
# never reaches a running loop.  That is only safe because it is the SAME function startup calls - the
# gate is injected and the caller passes `registry_evidence_problems` - and DRILL-G1 proves it by
# feeding the real shipped registry a report whose sha256 does not match.  The rollback restores the
# ORIGINAL BYTES rather than a re-serialisation: a YAML round-trip would drop the comments carrying the
# D-029 acknowledgement and the stress-coverage record, which would be a second silent change made by
# the undo of the first.
# `canary.py` is a deployment health check and NOT an alpha filter (KILL-AR-04); the first test in its
# file is the negative one - a perfectly healthy, perfectly unprofitable shadow must pass.
# The cli half is `governance status/plan/apply/transactions/enable/disable` plus `live run
# --state-dir/--registry`.  Both new live flags refuse to run armed, and both refusals sit above the
# dataset gate: a safety check that runs after it can be bypassed by deleting data.  `apply` is refused
# while the autonomy switch is off, and that switch is the one human confirmation point Q9 did NOT
# remove - enabling asks, disabling never does, because a stop that needs confirming arrives late.
# 2026-09-08, DL-S51 - 缠论, the 51st strategy and the one the operator named first: +397 in
# beidou_alpha.  The sentence the rule requires, and for once it is the good kind: this is signal, and
# it moves the alpha share the intended way.  Chan theory is a FAMILY of formalisations rather than one
# algorithm, so §7 of the governance plan pre-registered exactly one - merge rule, fractal confirmation
# bar, pen minimum, segment overlap, centre definition, which point types trade and which only close -
# before this module existed, and "whichever variant scores better" is not available.  Most of the
# addition is the docstring recording that choice, because a later reader who improves one rule has
# silently run a second experiment on the same data.
# One thing measured rather than assumed: the first implementation rebuilt every structure from bar 0
# at each step, which is causally safe (a rebuild sees a prefix) and quadratic - 0.20s / 0.83s / 3.49s
# at 2k / 4k / 8k bars, extrapolating to ~132s per symbol on the 49,096-bar archive and about 7.5 hours
# for ONE backtest of the 205-symbol universe.  Every layer here is a left-to-right accumulation, so
# the single-pass version produces the same structures in 0.30s per symbol; the equivalence is held by
# a prefix test rather than by the argument.
# The +18 in tests/ fixes KILL-AR-15 at the same time: the shared causality test sized its panel from a
# constant (800 bars, cutoff 600), which made it VACUOUS for any signal whose warmup exceeds 600 - the
# comparison was NaN against NaN.  It now sizes from `warmup_for`, and asserts the pre-cutoff scores are
# not all NaN so the emptiness fails instead of passing.
# 2026-09-08, DL-D4 (metrics -> Panel): +153 alpha, +41 live, +24 gov.  The sentence the rule requires.
# The alpha half is `Panel.metrics` plus two mining leaves, and it is signal rather than plumbing: it
# is the first time a candidate expression can read anything other than price, volume and funding, and
# it is what block 2's remaining leaves and block 1's basis/liquidation columns all sit on.
# One design decision carries most of the prose.  `Panel` takes ALREADY ALIGNED frames and REFUSES
# anything else rather than reindexing helpfully, because the alignment rule - a bar may read the
# latest bucket that had CLOSED by the bar's own close - lives in `beidou_data.metrics.align_to_bars`
# with the measurement that justifies it (archive `create_time` == rest `timestamp` - 5 minutes,
# 166/166 exact; one bucket of open interest moves a median 0.090% and always favourably).
# `beidou_alpha` may not import `beidou_data`, so a copy here would be a SECOND implementation of a
# look-ahead rule and the two would drift; a helpful reindex would restore the five minutes outright.
# Both leaves are relative rather than level - a change in open interest, a deviation of the long/short
# ratio from its own mean - because a level leaf ranks symbols by contract size or by how retail-heavy
# their book is and calls it a signal.  `reads_metrics` is a METHOD like `reads_funding`, so no
# existing expression hash moves, and `to_signal` propagates it into `needs_metrics` - without that,
# `metrics_refusal` has nothing to refuse on and a candidate scored on a T+1 archive would start
# against a 30-day REST window (KILL-027, one column over).
# The live half finally CALLS `metrics_parity`, which has existed since DL-D2 with nothing reading it:
# M-011 now lands in the daily report, folded to the worst symbol rather than the mean, because a book
# trades a universe.
# 2026-09-08, policy 0.1.0 -> 0.2.0: +52 gov.  The sentence the rule requires, and it is a RULE change
# rather than an implementation one, so R10's version bump and the re-pinned digest are the point.
# R1's budget was anchored on "one mine round cost 514 rows" and then set to 500 a quarter, which Q3's
# monthly window cut to 170 - a THIRD of a single round - while `refusals` refuses whole rather than
# truncating (a truncated search reports a `declared_trials` counting candidates nobody scored).  R1 +
# R2 + the scheduler therefore made `research mine` impossible to run in ANY window, forever, and that
# was found by trying to run one rather than by reading the rules.
# The fix is not a bigger number: R1 bounds how many SELECTIONS a window makes, and one mine round is
# one selection - enumerate the space, take the top k by marginal - however wide the space was.  That
# is exactly why `ledger_scope` files them into one shared bucket.  A test holds the part that would be
# dangerous to get wrong: the DSR denominator still counts all 514, because "best of how many" really
# is 514.  R1 stopped charging them to a BUDGET; nothing stopped counting them as trials.
# 2026-09-09, the metrics ingest made runnable: +84 data, +15 cli.  The sentence the rule requires,
# and the honest one is that this is a correction rather than a feature.
# The estimate that preceded it said 7.1 hours and was wrong twice, both times because a ten-day probe
# hides the terms that matter.  It assumed 10 requests a second from the file size, when the path is
# latency bound - measured 0.06s of CPU against 0.58s of waiting per symbol-day.  And it measured on a
# near-empty store, while `sync_metrics` called `MetricsStore.append` ONCE PER DAY - and that function
# reads the symbol's whole parquet, concatenates, sorts and writes it back, so day k rewrote k x 288
# rows and a 2,077-day symbol would have written about 621 million rows to store 598 thousand.
# So: gather a symbol's days and append once (linear), and fetch several SYMBOLS at a time - by symbol
# because a symbol's days must be gathered before its single append, which also puts every write on
# one thread so two workers can never touch one parquet.  Measured 8 symbols x 60 days in 39.1s
# against 33.6s for one, i.e. eight times the work for sixteen percent more time.
# The retry is in for a reason that contradicted its own premise: the operator had retired the local
# proxy that used to answer 503 in bursts, so a 503 should not have been possible - and 16 concurrent
# symbols produced one anyway, from the CDN, while 8 ran clean.  Concurrency-dependent throttling, so
# backing off works; a 404 is never retried because an unpublished day is the normal case every morning.
# 2026-09-09, DL-D4 part two - the columns reach a candidate: +81 alpha, +36 live, +45 cli.  Signal,
# and the sentence the rule requires.  Part one built `Panel.metrics` and two leaves; nothing loaded
# them, so `oi()` and `lsr()` raised on every panel in the repository.  Now `load_panel` aligns them
# through `beidou_data.metrics.align_to_bars` - the one place the five minutes can be got wrong, which
# is why it is the one place that knows the rule - and `_positioning_family` puts them in the search.
# `metrics` is opt-in on `_load` and defaults OFF: loading it reads a parquet per symbol and aligns
# every bucket, which is real work for a run whose signals read none, and a run that does not need the
# columns is better off not carrying the only look-ahead this data has.  `validate` turns it on when
# the strategy declares `needs_metrics`; `mine` always, because the candidates it is about to enumerate
# are what decides the answer and enumeration happens after the panel exists.
# The family is 90 candidates - 514 -> 604 - and the frozen-hash guard went RED on the first run, which
# is it doing its job: growth has to be a dimension that can be turned off (`include_metrics`) or a
# frozen space stops being one.  Every one of the old 514 hashes survives, asserted, because
# `_resolve_mined` re-derives an id by enumerating and an id that no longer enumerates cannot be named.
# 2026-09-09, +27 in beidou_live: `portfolio.min_history_bars` joins the construction fingerprint.
# The sentence the rule requires, and the way it was found is the interesting half.  Block 3's
# new-listing strategy (#27) turns out to be unimplementable as the book stands - `AlphaModel.eligible`
# excludes any symbol with fewer than `min_history_bars` observed bars, "new listings are excluded" in
# its own words - so it needs that number lowered, which is a construction change.  Asking where that
# change would show up found that it would show up NOWHERE: the number decides WHICH SYMBOLS may be
# held, and it was outside the digest.  P10 cell B's shape, one field over.
# Payload version 3 -> 4, and the alias is declared before it is ever written, exactly as v3 was and on
# the same proof: recomputed against the shipped config the value is 720 on both sides, so only the
# shape of what is hashed moved.  Without the alias the next restart resets M-010's 30-day window -
# unbroken since 2026-09-04T15:02Z - for a book that is byte-identical.
# Two pinned field sets went red and told me exactly what to do, in their own error messages.
# 2026-09-09, P28 - vol-matched pairs (#6): +144 in beidou_alpha.  Signal, and the sentence the rule
# requires.  The feasibility check changed the design rather than blocking it, which is worth the lines
# on its own: stage 1 of the portfolio is `w1 = target * vol_target / asset_vol`, so a signal emitting
# `+1` and `-beta` has its beta divided away by the two legs' own volatilities.  Defining the spread on
# vol-normalised returns makes that division the CORRECT normalisation - `+s` and `-s` become two legs
# of equal risk contribution - so the hedge comes free from the construction instead of fighting it.
# Written down because switching to an OLS beta later would need the portfolio changed, not the signal.
# Mutual nearest neighbours, not one-sided: with a one-sided rule the market's most correlated name
# becomes everybody's partner and the book is one leveraged bet, not a set of pairs.  And every refit
# sees a PREFIX - selecting pairs on the full sample is the classic look-ahead of this family, held by
# a truncation test rather than by this paragraph.
# 2026-09-09, +5 alpha +6 cli: `impact_model` restored to the validation report.  The sentence the
# rule requires, and it is an admission: DL-C1 claimed this field on 2026-09-08 and did not ship it.
# It was eaten when the surrounding edit was replayed, nothing asked for it, and the next report went
# out without saying what capital its verdict assumed - which is precisely the silence DL-C1 exists to
# end.  `test_a_validation_report_names_the_size_it_assumed` now asks for all three of the assumption
# fields together, because they were added for one reason: a verdict has to carry the assumptions that
# produced it, not just its number.
# 2026-09-09, +11 cli: `--state-dir` is honoured under `--paper` too.  The sentence the rule requires,
# and it is a defect in a flag this session added two commits earlier.  The paper branch applied
# `with_name("paper")` to whatever the profile said, which is right for the DEFAULT - it makes the
# paper directory a SIBLING of the live one - and silently threw the flag away the moment somebody
# named a directory: `--paper --state-dir .beidou/live-shadow` wrote to `.beidou/paper`.  A flag whose
# entire purpose is isolation delivered none, and two paper canaries would have shared one state.json
# without either of them saying so.  Found by running the DRILL rather than by reading the code.
# 2026-09-09, +15 cli: a canary no longer re-ranks the shared universe.  The sentence the rule
# requires, and this is the third isolation - the one `--state-dir` cannot give, because
# `universe.json` lives under the DATA root and no state flag reaches it.  Every enabled strategy's
# cited evidence records the universe fingerprint it was produced under, so a shadow that re-ranks the
# pool invalidates the ARMED loop's evidence and `registry_dataset_problems` refuses its next start.
# Measured, by causing it: a shadow at 2026-09-08T18:00Z moved the fingerprint d47dbc7c -> 788ade10 and
# an armed restart went from clean to blocked.  The armed loop refreshes daily at about 01:00Z on its
# own, so restartability was going to expire that night anyway - the shadow brought it forward by seven
# hours, which is the whole harm and is also exactly enough to matter during an incident.
# 2026-09-09, 选 3: the traded universe moves into the registry.  +13 alpha, +31 live, +2 cli.  The
# sentence the rule requires.  `universe.json` was the DECISION and the loop rewrote it daily at about
# 01:00Z; every cited report records the universe fingerprint it was produced under, so each re-rank
# made the evidence stop describing the traded population and the dataset gate refused the next armed
# start.  Restartability expired every day and nothing said so until a canary triggered it early.
# Two designs were possible and they differ by a month.  Making the refresh CADENCE a construction
# parameter would have put it in `construction_fingerprint`, which resets M-010's 30-day window - the
# clock the entire governance line is waiting on, unbroken since 2026-09-04T15:02Z.  Pinning the
# population in the REGISTRY instead moves `registry_fingerprint`, which M-010 does not key on, and
# makes a re-rank a governed transaction with a log and a rollback.  Same intent, no reset.
# The re-rank still runs and is still recorded; it stops deciding.  Observation kept, decision removed
# - the split this plan already applies to every other construction change, applied to the population.
# 2026-09-09, +13 alpha +14 live: the pinned universe reaches the digest the LOOP reports.  The
# sentence the rule requires, and it is a correction of the change two commits earlier.
# `registry_fingerprint` (what a research report records) and `registry_digest` (what the loop reports
# every cycle, and what `live verify` / M-Q10 compare) are two DIFFERENT payloads, and the first draft
# put the universe only in the research one - so a loop holding one universe while the file named
# another would have been invisible in the running record, which is KILL-Q15's exact shape.
# Both are conditional on the universe being non-empty, for the reason `CONSTRUCTION_PAYLOAD_VERSION`
# exists one fingerprint over: adding the KEY unconditionally moves the digest of every registry that
# pins nothing, and `live verify` would report the running loop as diverged from the file it loaded.
# Pinned to the measurement: the shipped registry's loop digest must stay 16671c63a12e, which is what
# the running process reports.
# 2026-09-09, +19 live +16 cli: the pin has to BIND, and only the account's own process may re-rank.
# The sentence the rule requires, and it is the second correction in a row to the same change - the
# first `governance apply` of a pinned universe was inert.  `state.universe` held the last daily
# re-rank and won at startup, so pinning moved `registry_digest` and not one symbol the loop held: a
# process reporting a universe it was not trading, which reads as agreement and is therefore worse
# than KILL-Q15 itself.  +19 in beidou_live is the precedence plus routing the names the pin drops
# through `leaving`, so they leave reduce-only instead of being orphaned holding a position.
# +16 in beidou_cli is `may_rerank_shared_pool`.  Its predecessor was an inline `if state_dir or
# registry_override:` asserted by a test that grepped for that literal string - so the test passed
# while a bare `--paper` re-ranked the shared pool at 18:00Z on 2026-09-08 and wrote PUMPUSDT over
# CYSUSDT.  The pin proposed the next morning was built from that file and named a symbol the armed
# loop had never held.  A source assertion can check that a rule is wired, never that it is right;
# the rule is now a predicate with a per-flag truth table.
# Also un-pinned from the shipped registry: the loop digest frozen at 16671c63a12e two commits back
# keyed on `config/alpha_registry.yaml` pinning NOTHING, so an intentional `governance apply` failed
# a unit test and taught whoever hit it to edit the constant.  The frozen hash is now a synthetic
# unpinned registry; whether the shipped file matches the running loop is a runtime fact and
# `live status --check` compares them every inspection.
# 2026-09-09, +262 governance +67 cli: DL-G6′, the half of the time rule that was never built.
# The sentence the rule requires.  `lifecycle.py` shipped knowing what `WINDOW_SURVIVED` and
# `PNL_STOP` mean and NOTHING produced either, so the state machine was complete and unreachable: no
# probe could reach main and no main sleeve could be sent back without a person typing the event -
# which is the hand-driven promotion §3's "all machine-driven" exists to remove.  `tenure.py` reads
# `cycles.jsonl` (append-only, and it already carries `at`, each probe's `stop`, and
# `external_flows`) and returns the events that record implies.  It decides nothing; `evaluate` and
# `apply` still hold every rule.
# The +67 in beidou_cli is `governance tenure`, split from `apply` for the reason `plan` is: probe ->
# main is a registry transaction and a restart, and the operator should read what the record says
# before either.  It also reports the edge this does NOT close - `probes_from_registry` excludes the
# main book, so no cycle reports a stop for a main sleeve and `main -> probe` stays unreachable.
# Closing that changes what can halt the live main book, which is a decision, not a side effect of
# adding a reader.
# 2026-09-09, +10 governance: a docstring that asserted a safety property the code does not have.
# The sentence the rule requires.  `state.load` said an empty book was safe because "every rule that
# could act on it refuses for want of a candidate" - true only of the candidate the CALLER supplies.
# Every rule that reads the book for CONSTRAINTS reads empty as headroom, and with no state file a
# second 1/3 probe was ALLOWED on top of the flow probe already running live: 2/3 against a 1/3 cap,
# with no rule anywhere saying a word.  Ten lines is what it costs to say that where the next reader
# will be standing, and the seeded state plus its test are what actually close it.
# 2026-09-09, +10 governance +4 cli: DL-G6′ shipped with its tests UNCOMMITTED, and the fix.
# The sentence the rule requires.  `git add -A ':!.venv'` staged three files and silently left the
# new test file out; the commit passed CI because CI ran the tests that were there.  Restored, and
# with it two cases the reconstruction surfaced that the first version did not have: a window the
# loop spent DOWN is not a window the sleeve survived, and a sleeve that entered mid-window does not
# get that window.  The +10/+4 is `Tenure.strategy` and the lookup that uses it: the record keys
# probes on the BOOK ("flow_short") and `governance_state.json` on the entry id ("flow"), so the
# "state holds N" line - the only thing that would show the derivation disagreeing with the stored
# count - was keyed on the wrong name and never printed.
# 2026-09-09, +11 live +12 cli: the SECOND shared record a shadow was writing, found while about to
# start the L3 paper soak - i.e. found by being about to cause it.  The sentence the rule requires.
# `MetricsStore.append` is read-modify-write through one `.parquet.tmp` per symbol, so two writers do
# not merely lose rows: one can publish a file the other was still writing.  And DL-Q6 says that
# store holds "the metrics the loop could read" - the ARMED loop; rows a paper process added would
# make M-011 compare the live decision against data no live decision was made on.  A shadow still
# READS it, because coverage gating needs that the moment a metrics-using strategy is enabled.
# The predicate is renamed `trades_the_account`: it was named after one of its consequences
# (re-ranking the pool) and there are now two, which is how the first one got the condition wrong.
# 2026-09-09, +26 governance: DL-K3 was comparing two ISO timestamps as STRINGS.  The sentence the
# rule requires.  `git` writes `preregistration.committed_at` with the COMMITTER's local offset and a
# report writes `generated_at` in UTC, so the comparison was about spelling.  It failed both ways:
# measured on the pointer the live registry cites it REFUSED valid evidence (prereg 211s earlier,
# text says later); turn the offset around and it ACCEPTS a forgery - a "pre-registration" committed
# 30 minutes AFTER a report sorts before it as text.  DL-K3 is the rule that stops a result being
# registered once it is known, and a `<` on strings is not that rule.
# It was also masking a stale fixture: while it refused that report, "the rules say no" and "history
# never adopted it" agreed, and the agreement hid an adoption list that had not been updated when the
# registry changed pointers on 2026-09-09.  One bug covering another is why AC-G0 read as passing.
# 2026-09-09, +35 live: the main book had no P&L stop and nothing computed one.  The sentence the
# rule requires, and it is an operator ruling ("主账本必须要有止损"), assessed before it was built.
# `probes_from_registry` excluded `MAIN_BOOK` unconditionally with no comment and no test, so the
# book carrying the whole `fraction` had its 30-day trailing attributed P&L not merely un-acted-on
# but never CALCULATED - what a rule cannot see it cannot bound.
# The +35 is `ProbeParams.halts` and the branch behind it, because §3 gives the two transitions
# different channels: `probe -> retired` is "快：stopped_books" and `main -> probe` is not.  Halting
# a probe IS the control; halting a fraction-1.0 book is switching the strategy off, and §3 says a
# firing main sleeve is DEMOTED and recounts.  So main's stop computes, records and alerts every
# cycle and moves the lifecycle - it does not empty the book.  Making it a halt is one registry
# field taken through a transaction by a person, which is the shape a decision that size deserves.
# 2026-09-09, +25 governance: R1's budget opened by OPERATOR ruling, policy 0.2.0 -> 0.3.0.
# The sentence the rule requires.  170 -> 1700 rows and 1 -> 4 mine rounds per 30-day window, and
# the lines are the reasoning, written where the next reader will stand: R1's provenance is E5 - a
# judgement call anchored on "one mine round cost 514 rows" - and it is a RATE limit, not the
# multiple-testing control.  R0's quantile gate is that, every trial still enters the per-strategy N,
# and the gate rises monotonically with N (measured at fixed Sharpe variance: 146 -> 0.1072,
# 677 -> 0.1198, 3000 -> 0.1310), so searching more raises the bar by itself.  Opening R1 costs
# statistical power and compute; it does not open a hole.  Finite rather than removed, because AC-G8
# asks that an exhausted budget stop `validate`.
# 2026-09-09, +115 alpha: #8 hour-of-day, the one block-2 node that needs no new data feed.
# The sentence the rule requires, and it is ALPHA growth against the 90% target rather than more
# governance plumbing.  `HourOfDay` is a symbol's own mean return in this hour of the UTC day, over
# its last `days` occurrences, plus `_seasonality_family` behind `include_seasonality`.
# The estimate steps back ONE OCCURRENCE inside the hour group, which is stronger than the causality
# contract asks: a mean including bar t's own return would obey "no future data" and still be part
# `ret(1)` wearing a seasonality label - and the family multiplies this leaf BY momentum, so without
# the shift that interaction would be partly a squared return, i.e. a volatility estimate.
# The grid is {14, 30, 56}, not {14, 30, 60}: `hod(60)` reserves 1464 against `max_lookback` 1400 and
# the first draft dropped all eighteen of its shapes as `too_long`, silently, because a rejection
# tally is not a failure.  A grid reporting three windows while searching two is a pre-registration
# that is not true.  The frozen-hash guard caught the family the same run - the candidate list is
# sorted BY HASH, so a new expression sorting into the middle shifts every index after it.
# 2026-09-09, +31 live: the number that STOPS A BOOK was in no digest at all.  The sentence the rule
# requires, and it was found by verifying the restart that armed the main book's stop - the expected
# digest came back byte-identical to the one before the stop existed.  Measured across all three
# hashers: the shipped registry, the same registry with tsmom's stop deleted, and the same registry
# with `max_loss` tightened sixty-fold gave IDENTICAL digests in `registry_digest`,
# `construction_fingerprint` and `registry_fingerprint`.  So a probe's stop could be relaxed,
# tightened until it fired daily, or removed, and `live status --check` would keep reporting
# "registry：与正在运行的循环一致".  KILL-Q15's shape on a risk control, and worse than on the
# universe: that one decides WHAT is traded, this one decides whether a book gets halted at all.
# Built through `ProbeParams` so the digest covers exactly the fields the loop acts on (prose stays
# out, `accepted_on` stays in - it decides where the trailing window starts), conditional so an
# unprobed registry keeps its digest, and NOT in the construction fingerprint, because reviewing a
# threshold must not reset M-010's 30-day clock.
# 2026-09-09, +244 live / +15 cli: DL-G7 / R8 - the ladder had no caller and the digest had no reader.
# `Policy.throttle_scalar` and `drawdown_grace_cycles` were written, versioned, hashed into
# `policy_digest()` and recorded every cycle, and nothing in the tree consulted them: the fifth
# instance today of a thing that looks like a control and controls nothing.  The growth is the wiring
# plus `attributed_drawdown_state`, which is the whole point of the rule - `drawdown_state` reads venue
# equity, 52% of this account is non-USDT collateral and 73% of its measured equity change was
# repricing, so de-risking on that reading pays a real cost for a number that was never about the book.
# On the live record the two rulers differ by 5.6x on the same days (equity -1.30%, attributed -0.23%),
# which is why this is not a cosmetic choice.  `value` is the CURRENT distance below the running peak,
# not the deepest ever: a ladder pinned to the worst hour the account ever had would never come back up.
# The cli line is `live status --check` comparing the recorded governance digest against the module on
# disk - R9 put the number in every row to make a rule edit visible and nothing read it back.  It found
# a real one on its first run: policy 0.3.0 landed 04:48Z, the loop kept reporting 0.2.0's
# `753638a519ac`, and the restart that closed the gap at 05:08Z was for an unrelated reason.
# 2026-09-09, +2 live / +26 cli / +15 gov: four MORE policy fields that nothing read.  A sweep of every
# `Policy` field against the production tree - written after the R8 ladder turned out to have no caller -
# found `gate_scope`, `report_whole_library_n`, `record_digest_every_cycle` and `no_decision_on_rebaseline`
# in the same state.  None of them was a behaving bug: each hard-coded value happened to match its
# declared one.  The hazard is what `policy_digest()` PROMISES - change a field and the record shows it -
# which for these four was false in the worst direction: the digest moved, the operator read a rule
# version bump, and the machine did exactly what it did before.  Now all four are consulted, and
# `tests/governance/test_every_threshold_has_a_consumer.py` is the general guard, which is the part
# worth the lines: it would have caught the ladder, and it catches the next one.
# The cli growth also carries DL-C1's stress fix (`impact=impact` in `cost_stress`/`slippage_stress`) and
# the gov growth the replay's capacity-arm attribution, so a report priced at 100k against an 11k book is
# read as the sensitivity run it is rather than as a promotion the operator skipped.
# 2026-09-09, +23 alpha: `ensemble.turnover_penalty` is parsed, hashed into `registry_fingerprint`, and
# implemented by nothing - `combine_targets` takes no such parameter and `AlphaModel` does not carry it.
# Found by mutating every leaf of the shipped registry and asking which ones move a digest, which is the
# sweep `tests/live/test_the_digest_sees_every_live_knob.py` now runs every time.  Setting it would move
# the research fingerprint, make the change look adopted, and leave the loop combining targets exactly as
# before: KILL-Q15 with the digest on the wrong side, moving when nothing else does.  `parse_registry`
# now refuses any value but zero; zero stays parseable so archived fingerprints still reproduce, and
# wiring it is a construction change that needs its own evidence.
# 2026-09-09, +14 alpha / +25 live: D-031 could shrink a PINNED universe behind the digest.  The sweep
# above, pointed at the profile instead of the registry, surfaced `pool.quarantine_after` as a live knob
# outside `construction_fingerprint`; reading what it does found that `_quarantine` removed a symbol from
# `self.universe` and left `self.model` alone.  Measured: one quarantine, 17 of 18 pinned symbols traded,
# `registry_digest` byte-identical at `abe21f7a8edf`, and `live status --check` still reporting agreement.
# The pin shipped the same morning is what made it matter - before it the digest carried no universe to
# be wrong about.  `AlphaModel.without_symbols` mirrors `without_books`, and the pinned case alerts,
# because under a pin the daily re-rank adopts nothing and the symbol does not come back without a
# restart: a machine departing from a governed decision may not do it quietly.
# 2026-09-09, +6 live: self-review of the ladder.  Attribution landing on a bar no priced cycle covers
# would have vanished from the path, which understates the drawdown - the permissive direction.  Zero on
# today's record, so this reports rather than fixes; "it is zero today" is not a property, and the day it
# stops being zero has to be visible.
# 2026-09-09, +11 cli: the impact model joined the ledger's trial signature, conditionally.  DL-C1's two
# impact-priced tsmom runs folded onto the flat rows as replays - `window_spend().spent` never moved off
# 169 - so the DSR denominator did not count them, while §19 had written down that adopting the cost
# model would charge rows.  One configuration under two cost models, keeping whichever passes, is the
# selection DSR exists to expose.  Conditional on `enabled` so an archived flat row keeps its signature;
# unconditional would charge every genuine replay as a new trial, the same defect mirrored.
# 2026-09-09, +3 cli: `governance tenure`'s "main -> probe cannot fire from the record" note was stale
# for hours after the main book got a stop and started appearing in the record.  `books_in` is keyed on
# the BOOK ("main") and `governance_state.json` on the entry id ("tsmom"), so comparing one namespace
# against the other read a sleeve that WAS in the record as absent from it - the same book-vs-strategy
# mismatch this command got wrong once already, in the other direction.
# 2026-09-09, +596 data: the public liquidation column (#19), conventions and ingest, no mining leaf.
# Two modules and a store, and the length is almost entirely the four measurements that shaped them.
# The first one cancels the column: `futures/um/monthly/liquidationSnapshot/` - the path §6 named as
# this column's history - lists ZERO keys, and so does the daily one.  The only liquidationSnapshot
# Binance publishes is COIN-margined daily, 2023-06-25 .. 2024-10-14, discontinued.  So there is no
# USDⓈ-M history to research on and, the archive having stopped 23 months before the live period, no
# bar any live stream could share with it: the same-source obligation §6 attaches to every block-1
# column is not unmet here, it is unmeetable, and RISK-G3's failure action ("该列不进实盘") is reached
# by measurement rather than by judgement.  The code is still worth its lines because the second
# measurement is the kind this ratchet exists to keep visible: the archive writes every row EXACTLY
# TWICE (6,438 rows, 17 symbol-days, zero singletons, zero odd groups), so anyone summing the file as
# it ships gets precisely double the notional with nothing raised anywhere - and the next person to
# reach for liquidation data will reach for that file.  `parse_archive_csv` halves each group and
# refuses an odd one; `LiquidationStore` keeps one parquet per symbol-DAY so the file's existence is
# the coverage record, which is the only way "no liquidations" and "never downloaded" stay different
# facts; and a 404 writes nothing while a published-but-empty day writes an empty file, which is the
# one place this downloader had to be the inverse of `sync_metrics` rather than a copy of it.
# 2026-09-09, +12 alpha: `Panel._map` dropped `metrics`, so slice / tail / select returned a panel with
# no DL-D4 columns and every open-interest or long-short leaf raised there.  Measured on the mine run
# immediately after the 202/205 metrics backfill - the backfill existed FOR those leaves - the report
# says `outcomes.errored = 90` and the 90 are exactly the 54 `oi` plus 36 `lsr` candidates.  Zero
# metrics candidates have ever been scored, in any round.  `_required_metric` raises loudly and the
# miner counts the raise; the count is all that reached the report, so a family that could not run at
# all read as a family that ran and lost.  The lines are the docstring recording that, because the
# one-word fix is not the part a later reader needs.
# 2026-09-09, +5 more alpha (merge of the VWAP work): DL-C1's retraction of its own pre-run estimate
# had reached the scratchpad and the log but not `ImpactModel`'s docstring, `costs.yaml`, or the impact
# tests - so a day later the stale "order/ADV about 1e-8" came back as the premise for closing #42.
# Measured on all 121 fills instead: order/ADV median 4.34e-07, notional-weighted impact 0.481 bps, two
# orders of magnitude off the premise.  These lines are the correction written into the three places a
# reader actually looks.
# 2026-09-09, +384 data: the event-time contract (RISK-G3, block 5), written before the downloader it
# guards - the DL-D2 order - and it found something on its first run against the production stores.
# The mechanism is the cheap half: a column declares what each source's own stamp MEANS, as a signed
# offset from the event time, and `verify_stamp_offset` holds that declaration against two samples.
# What the lines buy is the refusals.  A check that only CONFIRMS the declared offset passes on any
# column whose values barely move, so the rivals - the naive join, and one bucket either side - have to
# be refuted by the same sample or the answer is UNVERIFIABLE.  That is what the 2026-09-07 measurement
# actually was: 166/166 at -5min is evidence only beside 0/165 at 0 and +-10min.  Re-run on the stores
# today: 2,798/2,798 rows at the declared offset against 0/5,599 at the rivals, 18 symbols PASS,
# PUMPUSDT UNVERIFIABLE for having no overlap yet.  Mutation-checked, because a guard that cannot fail
# is the thing being guarded against: eight mutants, all caught, and the first pass caught only seven -
# the NaN rule had no test until one was written for it.
# The find is the fourth refusal, which nothing predicted.  `snapshot_metrics` polls
# `/futures/data/openInterestHist`, which returns open interest and nothing else, while
# `REST_TO_ARCHIVE` maps three ratio fields that only OTHER endpoints serve - so four of the six metrics
# columns are NaN in every snapshot row, on all 19 symbols.  `metrics_parity` folds columns into a ROW
# verdict and skips NaN pairs, so it reads `differing: 0, rate: 0.0` over data it never compared and the
# M-011 gate calls that parity met; `_required_metric` does not catch it either, because the column is
# present and merely carries nothing.  So a verification is admitted PER COLUMN now, and
# `admits_live_signal("count_long_short_ratio", ...)` refuses on today's data - that being the #17
# 多空比 leaf, the exact KILL-Q11 shape this gate exists for.  Nothing trades it today.
# Stated plainly rather than left to be discovered: no production code consults `admits_live_signal`
# yet, so this raise buys a mechanism and a finding, not an enforced gate.  That is the ladder-with-no-
# caller shape three raises above, and it is a debt this commit opens rather than closes.
# The three parts a later reader will want to "simplify" away, all deliberate: the rival-refutation
# requirement (looks redundant beside a passing check), the relative tolerance (the absolute 1e-6 next
# door reads zero today only because BTCUSDT's 8.515e9 notional sits 0.9% below 2^33, where one float64
# ULP becomes 1.9e-6 - and one ULP is exactly the disagreement the stores do show, on 29 of ADAUSDT's
# 155 buckets), and the per-column list (looks like reporting detail; it is the only field that can
# express "this column was never compared").
# 2026-09-09, +384 data (merge): `beidou_data/alignment.py`.  Block 5 asked for an event-time contract
# and said "without it nothing done later counts"; RISK-G3's failure action is that an unverified column
# does not reach live.  The design decision worth the lines: a confirmation is not a check.  The declared
# offset matching is only half of what 2026-09-07 collected - the sample must also REFUTE the rivals (the
# naive join, and one bucket either side), and when it cannot the answer is UNVERIFIABLE, never PASS.
# Measured on the 19 symbols present in both stores: 2798/2798 rows agree at the declared offset and
# 0/5599 at the rivals.  Twelve mutants caught; the first pass caught seven, and the NaN rule had no test
# until the mutation forced one.  Admitted debt, written here because it is this repository's own
# recurring shape: `admits_live_signal` has no production caller yet - contract precedes downloader.
# 2026-09-09, +169 alpha / +111 cli: the pairs signal's own search was charged nothing.  `research mine`
# charges every candidate it kept (DL-K2) because a free search is a DSR denominator wrong in the one
# direction that flatters it; one level down, `pairs` chose its partners out of every pair its formation
# window could form and the shipped report recorded `n_trials: 4`.  Measured on that report's own panel:
# 69 refits, 52-175 pairable symbols each, 19,578 DISTINCT pairs examined and 183 ever traded - so the
# grid was 0.02% of the denominator.  The lines are a census the signal returns (it has to walk the loop
# that trades, or it drifts silently toward under-charging), a `SignalSpec` field that declares which
# shared ledger bucket the candidates are charged to, and the `validate` plumbing that writes them BEFORE
# the run reads its own denominator.  Most of the count is the reasoning: which digests these rows must
# NOT carry, and why the bucket is shared rather than per-strategy, are the two decisions a future reader
# would otherwise have to re-derive from the ledger's fold rule.
# 2026-09-09, +417 data, +75 cli, +38 live, +33 alpha for DL-D5's spot ingest, with the sentence the rule
# requires - and with the overrun stated first: §9 budgeted "data +=250" and this is 417.  The extra 167
# is almost entirely the module docstring of `beidou_data/spot.py`, which records nine measurements taken
# against the venue that day, and the cost of NOT writing them down is the thing DL-D2 already paid.
# Three of the nine are why the code has the shape it has.  (a) The spot monthly archive switched to
# MICROSECOND stamps at 2025-01 while the futures archive is still milliseconds, so one market's bars
# would land tens of thousands of years from the other's and the join would return an all-NaN column
# indistinguishable from "nobody downloaded this".  (b) `/api/v3/klines?limit=1500` answers HTTP 200
# with 1000 rows instead of erroring, and `klines_range` decides a range is exhausted when a page comes
# back short of the limit - so a spot client inheriting the futures 1500 would have ended every REST tail
# 1000 bars in, silently; that is why `_page_limit` moved from a module constant onto the class, which is
# most of the diff in `binance_public.py`.  (c) XMRUSDT's spot listing has been halted since 2024-02-20 at
# 118.70 while its perpetual trades at 503.83, so the "latest value that had closed" rule `metrics` uses -
# right there, for open interest - would have priced a +324% basis off a dead listing and held it for two
# years.  Hence same-bar-or-NaN, and hence a test per failure rather than a comment.
# Where the non-data lines went: cli is `beidou data spot`, which resolves the perp -> spot mapping
# against the venue's listing, writes it, syncs, and prints the alignment measurement every run rather
# than behind a flag - a contract only ever checked against fixtures is a contract about fixtures.  live
# is `_spot_columns`, the one place the alignment rule is applied, mirroring DL-D4's `_metrics_columns`.
# alpha is `Panel.spot` plus `spot_field`, and it is the one raise here that moves the alpha share the
# right way; the refusal that makes "aligned by the caller" safe is now shared with `metrics` instead of
# copied, so that part is smaller than it was.  The honest note: 362 of 528 perpetuals have a spot leg,
# so a third of these columns will be legitimately all-NaN, and no signal reads any of them yet - the
# basis leaf is a separate piece of work.  This raise buys hypothesis space that is not yet spent.
# 2026-09-09, +256 alpha, and the sentence the rule requires: block 4's two construction options, P28's
# GARCH(1,1) divisor (#35) in `features` and the HRP budget (#48) in `portfolio`, both default-off.  The
# raise lands on `beidou_alpha`, which is the side the 90% effort target wants, and it buys the ability to
# MEASURE two candidates rather than argue about them - `vol_target` sat unexamined for a year because
# nothing could price the alternative.  Two thirds of the addition is docstring, and specifically the parts
# a later reader would delete as ceremony: why the GARCH fit is a hand-rolled 2-parameter grid rather than
# an `arch` dependency (`test_import_rules` pins this package to numpy and pandas); why the forecast is
# NaN before the first re-fit boundary instead of being filtered with block 0's parameters; why HRP is
# expressed as a TILT on an inverse-variance control arm instead of as a budget of its own (it changes two
# things at once and one number cannot say which paid); and why `_hrp_tilt` rebuilds the covariance
# diagonal from the floored `asset_vol` - the raw EWMA diagonal contains symbols whose variance rounds to
# zero, which handed one name ~100% of the inverse-variance budget and produced tilts of 1e86.  Deleting
# any of those four comments restores a bug that looks like a simplification.
# 2026-09-09, +8 cli: `research mine` loaded its panel WITHOUT metrics while enumerating the DL-D4
# leaves, so every `oi` and `lsr` candidate has raised `ExprError` in every round since DL-D4 shipped -
# `outcomes.errored = 90` on two consecutive rounds, and the 90 are exactly 54 `oi` plus 36 `lsr`.  One
# missing keyword: `research decompose` has `metrics=True` with a comment saying why it must be
# unconditional, and the command that actually enumerates them was written without it.  The lines are
# that comment, moved to where the mistake was.  Note what did NOT find this: I first blamed
# `Panel._map` (a real, separate bug, fixed in 3b49af8) and re-ran the whole round on that diagnosis -
# 658 more ledger rows for nothing.  The per-row `error` field had said the true reason all along.
# 2026-09-09, +12 cli: the miner's failure summary printed a count and the phrase "see the report",
# and that is what let the DL-D4 gap survive two full rounds.  Every failed row carried its own reason
# the whole time; 90 of 658 is a plausible number for a search rejecting malformed combinations, which
# this search legitimately does, so nothing looked wrong.  Now it groups the distinct reasons and prints
# them with counts - the difference between "these are illegal combinations" and "an entire family
# cannot see its column" is one glance instead of one JSON file.
# 2026-09-09, +307 data: `beidou_data/index_price.py` - #29, the first feed to enter the event-time
# contract registry after metrics, which closes the debt `alignment.py` opened ("no production caller
# yet").  Measured against the venue, not assumed: the daily index archive and /fapi/v1/indexPriceKlines
# agree on 24/24 buckets of BTC/ETH/SOL for 2026-09-05 at a stamp offset of ZERO, and on 0/23 one bucket
# either way - the opposite of the metrics feed's -1 bucket, which is exactly why each feed declares its
# own offset instead of inheriting one.  The four volume/taker columns are identically zero and match
# 23/23 at BOTH rivals, so they cannot refute anything and are deliberately NOT declared: a column that
# passes every offset verifies nothing.  Columns are namespaced (`index_close`, not `close`) because
# CONTRACTS is a flat global table and a bare name would hand the index contract to the perpetual's own
# close.
# 2026-09-09, +262 alpha, +62 live, +76 data, +22 cli: DL-D5 block 2, the `basis` leaf and its family -
# the first hypothesis that reads `panel.spot`, and the first thing that reads across two markets at all.
# The alpha lines are mostly the two docstrings, and they are the deliverable rather than the packaging:
# a basis can be written four ways (spread, simple ratio, annualised, net of funding) and three of them
# are wrong for reasons that are not obvious - a spread is a price, the simple ratio breaks the mirror
# symmetry every short arm in this module relies on, a perpetual has no maturity to annualise over, and
# the residual would silently become pure carry on the 166 of 528 perpetuals with no spot leg because
# `Sum` adds with `fill_value=0.0`.  The choice is recorded where the next reader will be tempted to
# change it.  The measured numbers are there too, and they refuted the premise this family was written
# under: funding and basis are the two ends of one arbitrage relation, so near-collinearity was expected,
# and on 20 liquid perpetuals x 14,592 hourly bars the R^2 of basis/vol on funding/vol is 0.064 and the
# per-bar cross-sectional Spearman of the two ranked shapes is +0.045.
#
# The live and data lines are the correction to the inherited WIP rather than new ground.  It had put the
# RISK-G3 refusal in `composition._spot_columns`, where a spot frame becomes a panel field - which reads
# right and is wrong, because `load_panel` builds the RESEARCH panel too, so the gate made the family
# unminable rather than untradeable and turned two already-passing spot tests red.  RISK-G3's sentence is
# "该列不进实盘".  The refusal is now `engine.spot_refusal` at startup, which also gives
# `Expr.reads_spot` the caller the WIP never wrote: it defined the method and nothing asked it, which is
# the same shape as `uses_funding` hardcoded to False (T-P17-06) and that one reached live.
#
# The cli lines are the second narrowing, in the shape the first one already had: `mine` searches what the
# PANEL can answer (`Panel.spot_symbols`), never what a flag asked for, and says in the artefact and on
# stdout that it narrowed.  Without that line the report would carry no `basis` rows for a reason it never
# gives, which is not distinguishable from a family that ran and lost - the exact confusion that let DL-D4
# survive two rounds.
# 2026-09-09, +579 data, and the sentence the rule requires: `beidou_data/onchain.py`, block 1's #31.
# Two of the five external-API columns were researched first, and the raise buys only one of them - #28
# (token unlocks) ends as "不可得" with no code at all, for #19's reason and with its own measurement:
# the only free unlock schedule is one continuously-overwritten document whose ALREADY-PAST values were
# found changing by 5.75x between the two dated captures that exist anywhere.  Not writing that
# downloader is the larger part of this entry.
# What the 579 buy is the first thing in this repository that consults an event-time contract in
# production, and one refusal `alignment.py` could not have predicted.  `verify_stamp_offset` settles
# the stamp convention - Coin Metrics and blockchain.info are two independent computations of BTC daily
# transactions, 355/355 at the declared day offset against 97/354 and 97/355 one day either side, at a
# tolerance that is itself measured (2% fails on the declared offset, so 5% is a number and not a
# preference).  It cannot see the defect this feed actually has: Coin Metrics publishes a
# `<metric>-status-time` per CELL, and BTC's exchange inflow for 2024-03-01 carries 2026-04-09 while
# ETH's for the same day carries 2024-03-02.  Same metric, same day, two assets, availability 769 days
# apart - so a verified offset and correct values are still unreadable-at-the-time, and the fifth
# refusal has to be separate from the four.  That is what makes the flow columns refused and the
# counting columns admitted, per column, on the source's own testimony rather than on judgement.
# Most of the addition is docstring, and these are the parts a later reader will want to delete: why
# `available_offset_ms` is TWO days and not one (`AssetEODCompletionTime` puts the vendor's own
# completion at 26.3-29.8 h after the day OPEN on 32 asset-days, so "+1 day" is a boundary the source
# has never once met and declaring it carries 2.3-5.8 h of look-ahead every day); why the registry here
# is separate from `alignment.CONTRACTS` rather than added to it (adding would break
# `test_every_metrics_column_that_can_reach_the_panel_is_declared`'s "no strays" clause, and mutating
# it at import time would make a governance verdict depend on import order - the copy that costs is
# guarded by `test_the_two_registries_refuse_for_the_same_reasons`); why the witness is fetched at all
# when nothing stores it; and why `align_daily_to_bars` keys on availability rather than on the day's
# close.  Honest note on the target: this lands on `beidou_data`, so it moves the alpha share the wrong
# way, and the column it enables covers 49 of 528 perpetuals - 9.3% against spot's 69%.  The coverage
# number is the finding, not a disappointment: a fifth of the board is what any #31 leaf can rank, and
# that is worth knowing before a leaf is written rather than after it is backtested.
# 2026-09-09, +606 data: `beidou_data/onchain.py` - #31, and the module docstring is a measurement
# record rather than an explanation, which is where most of the count goes.  The criterion it distilled
# is worth more than the feed: **whether the source tells you when a value was written**.  Both #28 and
# #31 have a free API, so "is there a free source" separates nothing.  Coin Metrics stamps every cell
# with `-status-time` - BTC's 2024-03-01 inflow was written 2026-04-09, 769 days after the day it
# describes, while ETH's same day was written the next morning - so backfill latency is a PER-CELL
# property and no single `available_offset_ms` can be right.  The flow columns are therefore refused and
# the count columns admitted, on the source's own testimony.  The declared availability is +2 days, not
# the arithmetic +1: measured completion runs 2.3-5.8h past day close, so +1 is a bound the source has
# never once met and declaring it would buy a daily lookahead.
# 2026-09-09, +281 governance / +96 cli, and the sentence the rule requires: the admission gate.
# `governance apply` wrote the registry after asking two questions - is autonomy on, does the startup
# gate accept the result - and the startup gate is a PER-ENTRY evidence check.  It cannot see a sum, a
# count, a calendar or a history, which is exactly what R3, R4, R5, R7 and K-EX14 are.  So every
# precondition §3 lists for `queued -> probe` was decorative on the only path that promotes, and §0's
# acceptance of AR-18 (no human confirmation point, because "R6 回滚 + Canary + R3 预算 + P&L stop"
# carries it) was resting on two controls with no caller anywhere in the tree.  `admission.py` is the
# layer that asks them, `governance canary` is what makes `canary.evaluate` reachable at all, and
# `--actor` exists because `promote.py` defaulted it to "machine" while its own docstring said a log
# that cannot tell a machine write from a person's is not evidence - which made AC-L5 unfalsifiable.
# Measured before: 1,846 production lines could not be reached from any `beidou` command, 337 of them
# the governance package's own budget, scheduler and canary.
# 2026-09-09, +844 data: `beidou_data/macro.py` - #32, whose adjudication was "挂起，等一次操作者动作"
# until the operator registered a FRED key.  The raise buys the first column in this repository whose
# VALUES change after the fact, and most of it is the measurement record that makes that checkable.
# The one number that justifies the feed: January 2024 payrolls was published as 157,700 on 2024-02-02
# and reads 157,032 today - 668,000 lower, after four revisions, the last written 2026-02-11, which is
# 739 days after a February 2024 bar would have read it.  Across 2019-2026, 91 of 92 PAYEMS
# observations differ from their first print and 8 of 90 month-over-month changes flip SIGN, so a
# naive backfill does not merely shift a level, it reverses the direction of the move on one month in
# eleven.
# The structural addition is a third stamp column that `EventTimeContract` has no field for.  Reference
# period and first availability it can carry; the REVISION HISTORY it cannot, because until now no
# column's value at a stamp ever changed.  So availability is per RELEASE rather than per contract -
# measured lag runs 31/34/80 days for PAYEMS and 37/41/78 for the CPI pair, and a single arithmetic
# offset would have to be 80 days to be safe and would blank the two newest months on every bar.
# `available_offset_ms` stays as the FLOOR the data is checked against (the reference month has ended)
# and `revision_leak` is the check the whole column exists for: it asks the ledger, not the aligner,
# whether each value had been published by its bar.
# These are the parts a later reader will want to delete, and the reason each is not deletable: why
# `period_ms` is 31 days and not 30 or 30.44 (a +-31-day rival lands on a real month open only for the
# seven 31-day months, so 31 draws 16-19 comparisons where 30 would draw zero and the check would
# answer UNVERIFIABLE having refuted nothing); why availability rounds to TWO whole UTC days
# (`realtime_start` is a calendar date with no time and no zone, so one day is unsafe by up to eight
# hours, and the series steps monthly so the two days cost nothing); why the witness is BLS rather than
# a second FRED query (FRED redistributes BLS, so FRED cannot answer whether FRED's own stamp names the
# reference month); why UNRATE is downloaded by nobody despite being in the same release (four
# measurements agree - 33/92 revised, 0/58 sign flips, rivals only 65% refuted, and 27 of 39 distinct
# values recur so the leak check is nearly blind on it); and why the forward hold across ~730 hourly
# bars is bounded at 120 days (the 2025 shutdown left PAYEMS 76 days between prints while the numbers
# stayed correct, so a tighter cap blanks a healthy series and no cap resurrects a dead one).
# Honest notes on the target.  This lands on `beidou_data`, so it moves the alpha share the wrong way,
# and it buys no leaf: nothing is mined here and the pre-registration in `docs/RESEARCH_LOG.md` is
# explicit that a macro column is a market-wide SINGLE SERIES, so every cross-sectional operator in
# `beidou_alpha` returns a degenerate result on it and only a time-series or interaction term can use
# it at all.  That is a property of the data, it is asserted in a test rather than described, and it is
# worth knowing before a leaf is written rather than after it is backtested.
# 2026-09-09, +13 governance: `governance canary` shipped broken on its first real run -
# `load_jsonl` takes text and it was handed a Path - and `canary.evaluate` counted RAW construction
# digests, so a renamed field would have failed a candidate for a deployment that did not change
# (6 distinct raw vs 3 canonical on the armed record).  Both are the same defect this whole day is
# about, arriving in the fix for it: a command added to make a module reachable, and then not
# exercised.  The lines are the alias parameter and the test that drives the command through the CLI.
# 2026-09-09, +190 governance / +45 cli: `family_gate.py` and `governance gate` - §3's third condition
# on `probe -> main`, which had a definition, a handler, and no producer.  The `WINDOW_SURVIVED` branch
# checked R4 and stopped, so a probe reached main on two of the three conditions §3 names, and the
# missing one is the only one that can turn against a sleeve while the sleeve does nothing: the D-028
# gate is a function of N, and N grows whenever anybody searches in the family.  Searching more retires
# your own incumbents.  Holding everything but N at the report's values - including the annualisation
# scale, backed out of the report's own threshold/quantile pair - is what makes any movement
# attributable to the denominator alone.  N is NOT the bucket count: `dsr_inputs` builds it as ledger +
# grid + declared prior, so tsmom's 183 is 86 + 2 + 95 while its bucket reads 88, and only the first
# term can move after the fact.  First real run: tsmom PASS 1.8087 vs 1.5149 at N=185, and flow
# UNREADABLE - its registry evidence is a BOOK report, which carries no `oos_selection`, so §3's third
# condition is not computable for the only probe running.  That is a finding, not a bug in the reader.
# 2026-09-09, +146 live / +31 cli: `soak.py` and `live soak`.  §5 states L3's criterion twice and the
# two disagree - the L3 row asks for '7 天无 ERROR 相', and two paragraphs later the same section says
# an ERROR phase produces no governance decision because the path to the venue crosses a proxy that
# 503s in bursts.  One counts ERROR cycles, the other is a claim about their consequences.  Nothing
# computed either: the soak had been running since 03:28 against a rule that lived only in prose.
# Measured on the armed record: 2 ERROR in 6.29 days (0.318/day), longest clean run 4.92 days, and
# BOTH ERROR cycles decided nothing - no orders, nothing leaving, nothing quarantined, ladder untouched.
# A Poisson fit puts seven consecutive clean days at 10.8%, fourteen at 1.2%, thirty at 0.03%, so the
# literal reading is not a demanding criterion but a mostly unreachable one, and unreachable for the
# reason §5 names itself.  Both readings are computed and printed; neither is ruled on here.  The lines
# are the second reading and the ERROR-streak number that neither covers - one 503 is the proxy
# blinking, six hours of them is the venue being gone, and only the second says anything about this
# deployment.
# 2026-09-10, +227 governance / +95 cli: `verdicts.py` and `governance verdicts|review|divergence`.
# M-G05 is Pre-A′'s ONLY falsifier - the plan names 'written rules can replace a person's runtime
# judgement' as its highest-risk assumption and gives it exactly one way to be refuted - and it had no
# instrument of any kind: a row in §11, a cell in §13, and nothing that wrote, read or computed it.
# It is not `governance replay` wearing a different hat.  Replay asks whether the rules REPRODUCE
# decisions already taken (M-G02, backward, fixed record, currently 0 unattributed).  A rule set fitted
# to a history can reproduce all of it and be wrong about the next one; M-G05 asks people about the
# rulings the machine is making NOW.
# Two lines carry most of the count and neither is decoration.  The gate records its own verdict, so
# the sample is not selected by which decisions somebody found memorable - and the memorable ones are
# the surprising ones.  And an unreviewed ruling is `pending`, never absent, so the denominator cannot
# shrink toward agreement; below a quorum of ten the rate is None rather than 0%, because zero of three
# reviews is three reviews, not a clean record.  Directions are kept apart per §11: a machine that
# refuses too much and one that admits too much need different fixes, and one averaged rate can look
# healthy while both are large.
# 2026-09-10, +11 live / +7 cli / +3 governance: ruff's own output, plus one real bug it caught.
# `ruff check` and `ruff format --check` were RED on this session's own files, which CI installs the
# latest ruff to run - so the day's work would have failed CI on style while its subject was code that
# looks correct and is not.  The one substantive fix: `soak.score` fell back to a NAIVE
# `datetime.fromtimestamp(0)` when the first cycle's timestamp would not parse, and subtracting that
# from the aware stamps around it raises - so a torn first row turned a criterion about seven days into
# a TypeError.  It now refuses to measure a window whose start it cannot read, which is the same rule
# every other reading in this session follows: could-not-compute is not passed.
# 2026-09-10, +243 cli and +518 governance: the governance line's WRITE side, which is §9A 1 and 2 of
# the plan-vs-code audit and the heaviest thing left in it.  Two commands.
# `governance next` assembles `scheduler.Context` out of the reports, the ledger and the state, and is
# what takes `scheduler` (115 lines) and `budget` (112) out of the reachability guard's EXEMPT list -
# 227 lines holding R1 and DL-G8 that no `beidou` command could reach, so AC-G8 could not be attempted
# and R1 had no production reader at all.  `governance advance` folds the events `tenure` already
# derived from `cycles.jsonl` through `lifecycle.apply` and writes the result, which is the first
# production caller either that function or `state.write` has ever had: `governance_state.json` was
# maintained by hand, one commit in `git log`, so R4's `promotions_this_window`, R5's
# `consecutive_probe_stops` and R7's `probe_entries` were typed numbers no record had to agree with.
# Where the governance lines went: 303 in `assemble.py`, 180 in `family_gate.py`, 35 across
# `lifecycle`/`state`/`admission`.  The 180 are NOT new work - `family_gate.py` and the `lifecycle`
# branch that reads it are the parallel session's, copied byte-identical from its tree because
# `advance` has to supply `Facts.family_gate_still_passes` and reimplementing it would put a second
# arithmetic into a number whose whole job is to isolate one variable.  Whoever merges the two should
# expect this line to conflict and should count those 180 once.
# The largest single piece is `assemble.py`, and most of it is about the fields it CANNOT read.  Every
# count in `Context` has a `> 0` branch, so an unreadable field defaulted to 0 does not error - it
# answers, one step further down the pipeline than the evidence supports, which is the empty-book
# failure wearing a scheduler's clothes.  So a field carries its source, an unreadable one carries why,
# and `load_bearing` asks whether the answer would have differed had it read the other way.  Measured
# on this repository today: `search_space_digest` is unreadable, because every `mine-shortlist-*.json`
# in the tree predates `run.include_basis` and a guessed knob and a widened space produce the same
# disagreement - so R2 cannot be asked at all until the next round writes that field.
# The honest note on the target: this lands on `beidou_cli` and `beidou_governance`, so it moves the
# alpha share the wrong way and buys no leaf.  What it buys is that §3's rules now have a writer as
# well as a refuser, which is the half AR-18's compensating controls were resting on.
# 2026-09-09, +184 alpha / +203 cli / +46 governance: §3's three limits stop being a literal `True`.
# `validated -> booked` has four conditions and `beidou_governance/replay.py` supplied three of them -
# `slippage_stress_pass`, `max_correlation_with_running`, `turnover_ratio_to_main` - as `True / 0.0 /
# 0.0` since Phase 0, because NO BOOK REPORT CARRIED THE FIELDS: `slippage_stress` was in seven
# validation reports and zero book reports, and `research correlate` wrote a separate artefact with
# nothing linking it back to a book.  So the only thing an artefact could fail at that transition was
# D-018's verdict, and `lifecycle`'s own "absent knowledge is False" described nothing that ran.
# Where the lines went, and why none of it is decoration.  `beidou_alpha/validation/book_limits.py` is
# the arithmetic (pure, so each of the three is testable on a hand-built panel rather than on a
# two-hour book run) plus the paragraph explaining why the turnover ratio uses the operator's own
# pre-registered normalisation - raw `turnover_units`, candidate alone and unscaled - when a
# per-gross reading would have been 3.7x stricter on the shipped book: reporting a number nobody has
# ever judged against is not the same as enforcing a limit.  `beidou_cli` is `research book` measuring
# them (nine backtests over ALREADY-DECIDED weights - no model is re-fitted) and enumerating the
# running books from the registry.  `beidou_governance` is the replay reading the block instead of
# inventing it, and suspending PER CONDITION when it is absent, so the six archived reports keep
# passing while the artefact says which three rules were not applied to them.
# Honest note on the target: this lands 184 lines on `beidou_alpha` against 249 outside it, so it
# moves the alpha share the wrong way, and it buys no leaf - it is governance plumbing wearing an
# alpha module's address because that is where the arithmetic belongs.
# 2026-09-10, +236 beidou_cli: `beidou data onchain`, `data index` and `data macro`, the three ingest
# commands §9A item 6 asks for.  1,757 lines of tested `beidou_data` - #31's Coin Metrics downloader,
# #29's index client, #32's release ledger - could not be reached from any command, so three modules
# with contracts, verifications and their own test files were sitting in the reachability guard's
# EXEMPT list waiting for exactly this.  232 lines of entry point against 1,757 lines of data code that
# nothing could run is the trade, and the ratio is the argument: 7.6 lines of data code reached per
# line of entry point.  What the lines are NOT is a fourth store or a refactor of the three modules -
# each command reuses `MetricsStore` (macro stores nothing, deliberately), and the largest single block
# is the docstrings recording why each command prints a RISK-G3 refusal rather than treating an ingest
# as admission.  The counterweight is that this grows non-alpha again, which the plan's budget already
# records as breached; the alternative was leaving three modules unreachable, which is the state this
# whole day's work exists to end.
# 2026-09-09, +169 data / +125 cli / +14 live, and the sentence the rule requires: DL-D5's two open
# ends, joined.  Nothing new was invented here - the store, the ingest command, the mapping file, the
# event-time contract, the `basis` leaf, `load_panel`'s `spot_store` parameter and `mine`'s narrowing on
# `Panel.spot_symbols` all already existed, and the feed was dead at both ends anyway.  That is the
# finding, and it is a different shape from a missing feature: EVERY PART OF A PATH CAN EXIST AND THE
# PATH STILL NOT EXIST.  Both ends failed silently and in the direction that reads as success.
# The research end: `research_cmd._load` built a `KlineStore`, a `FundingStore` and a `MetricsStore`,
# and no spot store.  So `Panel.spot_symbols` was 0 on every panel `mine` could construct,
# `searched_basis = panel.spot_symbols > 0` was False on every run, and the 18 basis shapes were never
# enumerated - while the artefact recorded `include_basis: false` truthfully.  A reader cannot tell that
# from a family that ran and lost, which is exactly the confusion DL-D4 took two rounds to escape, and
# it is why this one survived a round longer than DL-D4 did: the metrics leaves at least ERRORED 90
# times per round.  The cli lines are that wiring plus `_wants_spot`, the `needs_spot` twin of
# `_wants_metrics`, so `validate` carries the columns for a mined basis id and only for one.
# The live end: `engine.spot_refusal` asked `admits_live_signal` at startup and `self.spot_verification`
# was assigned `None` in the constructor with nothing able to set it, so the gate could not be opened by
# any measurement, right or wrong.  The data lines are `verify_spot_contract` (the SPOT contract held
# against two independent renderings - measured against the venue today on BTCUSDT 2026-08: 744/744 at
# the declared offset, 0/743 and 0/744 one bar either way, all five panel columns compared) and the
# record it writes.  `read_spot_verification` is the part worth defending against a later simplifier:
# it RE-DERIVES the verdict from the record's own counts instead of parsing the one written beside them,
# because `spot_alignment.json` sits in a directory an operator can edit and `LiveEngine.__init__` says
# no edit may open this gate.  Deleting that re-derivation turns a measurement back into a config key.
# What the raise does NOT buy, stated because the next reader will assume otherwise from the gate being
# open: the live panel still carries no spot column at all.  `AlphaModel.targets` builds its panel from
# the market-data port's bars and nothing supplies spot to it, so a basis strategy that passed this gate
# would raise `ExprError` from `_required_spot` on its first cycle.  Loud rather than silent, and at the
# first cycle rather than at some later bar, which is why the refusal was left to be about the offset
# alone.  Two open ends became one.
# 2026-09-09, +369 live: three metrics that were computed and read by nobody got readers.  The audit's
# §8 table is the justification - it lists what each metric has and what consumes it - and this is the
# only kind of growth that shrinks the gap between what this system measures and what it knows.
#   * `collateral_drift` (RISK-G11) reached `reports/daily/*.json` and no further.  It is now rendered,
#     and it carries a `direction` classification, because the reading is not a level: the "73%" pinned
#     in its docstring, in the plan's §12 and in a test's `assert 0.72 < share < 0.74` reads 106.8% on
#     the live record today.  ~55 lines of instrument, ~30 of renderer.
#   * `restart_cost` (M-Q03) was rendered and compared to nothing, while its thresholds sat in the
#     2026-09-06 plan ("<= 5% / 0").  Wiring them meant first fixing the count: reconstructing the
#     engine's per-process running total read 1 on a day the record holds three miss rows.  ~50 lines.
#   * M-G06 (§19 Q2's lagging criterion) was zero code, the audit's starkest row.  ~110 lines, and it
#     will answer INSUFFICIENT_DATA until 2028-03-04, which is what it is for.
# The alpha share moves the wrong way and that is stated rather than hidden; the alternative was three
# more sentences in a plan, which is the thing this repository keeps finding it already has too many of.
#
# 2026-09-10, `beidou_live` 7,180 -> 7,218 (+38).  M-Q03 charged an operator restart with a missed
# rebalance on a bar that had already been rebalanced.  Measured that morning: the 05:00 bar was
# rebalanced at 06:00:27Z with 18 fills, the operator restarted at 06:01:28Z, the new process woke at
# 06:02:05Z outside its 71.6s window and wrote a miss row - and the same day's OTHER restart, at 04:52,
# woke 3135s after a close whose rebalance never happened, from a proxy 503 killing `exchangeInfo`.
# One number covered both, so M-Q03's `查重启原因` fired on a restart with nothing to look into, which
# is KILL-R6's lesson in its loud form: an alarm that always fires is the same defect as one that never
# does.  The +38 is a two-reason predicate in `scheduler` (a leaf, so both sides can import it, and it
# also removes the duplicated reason literal that had one copy in the engine and one in the reporter),
# the branch in `engine.run`, and the count in `restart_cost`.  The LATE half is untouched on purpose:
# whether a startup reconciliation belongs in that denominator is a question about M-Q03's caliber, and
# re-calibrating a metric because today's reading was inconvenient is the move the pre-registrations
# forbid.  This changes one thing: a bar that was already rebalanced cannot have had its rebalance
# missed.
#
# 2026-09-10 (second), `beidou_live` 7,218 -> 7,328 (+110), `beidou_cli` 5,565 -> 5,574 (+9).  The
# operator ruled the four questions the audit had left with them, and two of the four are code:
#   * §5 L3's contradiction.  The gate is now the no-decision reading AND a bar on the longest ERROR
#     streak, and the bar is `STUCK_IN_ERROR_STREAK` - the same 3 `live status --check` already pages
#     on, moved into `health` so L3 and the hourly check cannot drift apart about what "stuck" means.
#     ~35 lines across `soak`, `health` and the command.
#   * M-Q03's late half.  It could only ever see restarts: `late_seconds` is written in exactly one
#     place, `_record_missed_rebalance`, so a metric named 迟到周期占比 was reporting restart frequency
#     over a denominator of every cycle - it FELL when the loop ran more, and a genuinely late wake-up
#     could not raise it at all (the audit counted 33 such cycles in the record; none were in it).  The
#     share is now over scheduled wake-ups, computed from the `at` and `bar_open_ms` every row has
#     always carried, against the window the ENGINE states - which it now writes onto every cycle row
#     instead of computing at startup and throwing away.  ~75 lines across `engine` and `reports`.
# Both are the same defect this ratchet's earlier entries keep paying for: the fact was recorded and
# nothing read it.  Neither raises a threshold; one resolves a contradiction that predates every
# measurement, the other fixes a denominator.
#
# 2026-09-10 (third), `beidou_cli` 5,574 -> 5,599 (+25), `beidou_governance` 3,182 -> 3,215 (+33).
# R1's mine-round limit was advisory: `mine_refusals` existed, `governance next` printed "4/4 mine
# rounds", `scheduler.next_action` consumed the refusal - and `research mine`, the only command that
# can spend a round, imported none of it and had never asked.  R2 has refused in that same function
# all along, which is the contrast: one rule was a gate and the neighbouring one was a printout.  The
# +25 in `cli` is the check, placed before the panel loads rather than beside R2's (R2 needs the space
# digest; R1 needs only the ledger, and a run that may not happen should not first score the space it
# may not score).  The +33 in `governance` is 0.3.1's note and the two constants that let a dated
# reversion be a test instead of a promise.
#
# 2026-09-10 (fourth), `beidou_cli` 5,599 -> 5,661 (+62), `beidou_governance` 3,215 -> 3,383 (+168).
# The reopen conditions got a reader.  `RESEARCH_LOG.md` carries `REFUTED` 69 times and thirteen blocks
# headed 「重开条件」, each naming what would have to become true for a closed hypothesis to be looked
# at again - and a full-tree grep for `REFUTED`/`reopen` across the governance package returned nothing.
# So a condition that came true would never be noticed and the hypothesis would stay closed by neglect
# rather than by evidence.  Measured the moment the reader existed: regime (#47) was closed needing
# "块 1 的数据宽度（OI / 多空比 / 基差 / 清算流）" and three of those four have landed since.
# Nine of the thirteen cannot be asked of a machine and are reported as NEEDS A PERSON, counted in the
# summary every time; giving each a checkable proxy so the list looked complete is the move this whole
# ratchet's history is a record of not making.
# 2026-09-12: +193 live, +14 cli, +6 governance.  Three alarms that were each firing on a mixture of
# two populations, none of which could be split without one new fact - which book carries a symbol:
# `books` on the cycle row (engine, 1 line), `books_by_symbol` + the per-book/error-bar split in
# `slippage_bps` (M-Q08 read 5.47 bps combined; 0.80 main-only, 16.18 on the probe's four names), the
# single-book/combined split in `risk_adaptation` (0.96 combined, 0.1195 single-book), and the
# offender list on `compare_targets` so a KILL-027 failure survives `tail -n 3`.  cli: persisting the
# verify payload the pager truncates.  governance: the reopen summary counting the entries it hides.
#
# Second raise the same day, +38 live / +31 governance, and the same defect twice more: M-002/M-010/
# M-G06 annualising an attributed series at 8760 bars a year that had 81% of its hours deleted (a row
# is written only by a cycle with income), and M-G05's ledger keying idempotence on the DATE, so ten
# quiet days of running `governance gate` would have carried Pre-A′'s only falsifier to its quorum of
# 10 on ten re-readings of one ruling.  Both are docstrings carrying the measurement, not new machinery.
#
# Third raise, +43 live: the 2026-09-12 operator rulings.  M-Q08's bar moves to the main book (which
# reads BLIND at 19/30 fills, not PASS - the ruling was taken with that cost stated) and keeps the
# combined reading printed; and M-014's correlation is rendered onto the probe's own 30-day review
# line, because the one moment it decides anything is that review and it had no reader at all.
#
# Fourth raise, +63 live / +6 cli: KILL-027's cause, measured.  Six failures in 48h all came from
# cycles that read the bar within 14s of its close, while 0 of the 22 that waited longer failed, and
# re-fetching a settled bar at +15/+60/+300/+600s returned byte-identical fields.  `SETTLE_SECONDS`
# carries both measurements and puts the sentence into the alert; it gates nothing and widens no
# tolerance, and `last_scored_cycle` keeps the lag off the restart rows that have no contributions.
#
# Fifth raise, +16 live / +14 cli / +77 data: the 2026-09-12 operator rulings again.  "One machine
# only" unblocked L4, and starting the soak for the first time found `run_shadow.sh` writing
# `.beidou/live-shadow-dry-run` while all three of its readers defaulted to `.beidou/live-shadow` -
# so `store_directory` is now the single definition both sides use.  And #17's "research yes, live no"
# turned out to be plumbing: the recorder polled one of the five endpoints that serve the six metrics
# columns, leaving four of them NaN in every row since the store existed, with `longAccount` mapped to
# a ratio column it is not.  `REST_SOURCES` is per endpoint, the poll is bounded-concurrent (4.7s for
# 18 symbols against 28.6s serial, on a cycle that takes twenty).
#
# Sixth raise, +23 alpha / +133 live: the probe stop's caliber (operator ruling 2026-09-12, option C).
# The stop reads realised attributed income (30-day sigma 0.137% of equity) while the sleeve's evidence
# is a mark-to-market P&L (3.239%, recomputed over the validated panel), so -2% sits at 14.6 sigma of
# one and 0.62 of the other.  `book_weights` and `closes` on the cycle row make the second caliber
# computable at all; `marked_pnl` computes it; both are printed.  The GATE does not move today and the
# lines say why: `max_loss` is inside `construction_fingerprint`, so changing it clears M-010's window.
CEILING = {
    # Merge note, 2026-09-15.  The seven entries below (Twenty-fifth..Thirty-first OF THIS TABLE) are
    # one session's; they were renumbered twice to get out of the way of another session's, and then
    # the renumbering was STOPPED.  Saying why, because the next person to merge will face the same
    # choice.
    #
    # The number is not a key and has not been for a while.  There are two series in this file - "Nth
    # raise" (2026-09-04..06) and "Nth raise OF THIS TABLE" (2026-09-14..15) - and both run past
    # Thirtieth, so every number above the twentieth now names two or three entries.  A third session
    # raised this table the same day and took Twenty-fifth and Twenty-sixth, dropping "OF THIS TABLE"
    # while keeping the new dates, which collides with the older series as well.
    #
    # Chasing uniqueness across three sessions writing concurrently costs a renumber per merge and
    # buys nothing: what identifies an entry is its DATE plus what it says, which is how the
    # Thirteenth entry's own collision was handled and what the session holding Twenty-fourth chose
    # deliberately.  So the collisions are recorded rather than resolved, here and in their entries.
    #
    # What is NOT left to convention: the VALUES.  Every merge re-measures them against the merged
    # tree, because no side's count describes code that now includes the others'.
    #
    # Thirty-second raise OF THIS TABLE, 2026-09-15: +17 beidou_data, +8 beidou_live - the dataset
    # gate stopped checking pit reports against a file they never open.
    #
    # `research_cmd._resolve_symbols` calls `read_universe` only under `static`; a `universe_mode: "pit"`
    # report's population is the union of `membership.parquet`, which blocks unconditionally one field
    # over.  So the universe block was refusing armed starts over `universe.json` for reports that never
    # read it - and the 2026-09-09 fix for THAT was to freeze the traded pool, which held the live
    # universe at 16 names from 09-09 to 09-15 while the cited evidence re-ranked every 24 hours
    # (`median_gap_hours: 24.0`, `union: 211`).  The pin is being removed in the same branch.
    #
    # Most of the added lines are the prose: the exemption is a one-line predicate, and what costs lines
    # is writing down WHY only an explicit `pit` is exempt.  That paragraph is the thing a future reader
    # needs, because the obvious "simplification" - exempt anything that is not `static` - would silently
    # loosen the gate for every report written before `universe_mode` was recorded.
    # Corrected 2026-09-15: this said "the two docstrings".  It is one docstring (`_universe_drift_blocks`)
    # plus one inline comment (`registry_dataset_problems`), and a review caught the slip.  Corrected
    # rather than deleted, because a ledger entry that quietly rewrites itself is worth less than one
    # that shows where it was wrong.
    #
    # Design: docs/analysis/2026-09-15-universe-unpin-and-dataset-gate-design.md
    #
    # Thirty-third raise OF THIS TABLE, 2026-09-15: +11 beidou_data - narrowing a claim the entry below
    # made too broadly, after a review found a counter-example that ships in this very registry.
    #
    # `_universe_drift_blocks` said "a pit result's population is the union of `membership.parquet`", and
    # that is not true of every pit report: `research book --universe pit --robustness static` reads
    # `universe.json` at `research_cmd.py:2470` to build its sensitivity arm, and the registry cites
    # `book-tsmom-flow-20260908T105322Z.json`, which is `universe_mode: "pit"` with
    # `robustness_universe: "static"` - it really did read the file.  Harmless today only because `book`
    # writes no `dataset` block, so that report reaches `manifest_check` as `None`.
    #
    # Eleven lines to narrow one sentence is the trade this table exists to make visible, and it is worth
    # taking: the gate's exemption is now described by something that is true of every report it applies
    # to, and the ONE condition keeping the counter-example harmless is written next to it rather than
    # rediscovered by whoever gives `book` a manifest.
    # Thirty-first raise OF THIS TABLE, 2026-09-15: +129 beidou_live, +13 beidou_cli, +9 beidou_alpha -
    # the five "this symbol cannot be priced" rules written down, and NOT merged.
    #
    # They live in three packages, carry three numbers, and two bind on only one side: research carries
    # a position for `stale_carry_bars=2` bars with the stop live, live zeroes the target and re-opens
    # next cycle with the trailing anchor gone.  Two books.
    #
    # The values are unchanged, and that is the decision rather than the shortfall.  `exits.py` already
    # said reusing the loop's number was the point ("research and live disagreeing ... is KILL-027's
    # shape") and then admitted it binds in research only; the live half is `dropped_after`, and
    # `construction.py`'s v6 note records the operator's ruling in as many words - raising it "stays a
    # priced decision rather than a side effect of deploying a fix".  Making it bind here, from the
    # other direction, would be exactly that side effect.
    #
    # So what was bought is that the disagreement is data with a test behind it instead of prose.  That
    # is not a preference: the same comment still claimed `stale_carry_bars` was "not yet in
    # `construction_fingerprint`" two days after v6 put it there.  Prose about five settings in three
    # packages goes stale; a table joined to `__dataclass_fields__` fails instead.
    #
    # Priced, so the flip is a decision and not an argument: over the 322 live cycles on record
    # `inputs.dropped` is non-empty in ZERO of them.  The five PIT members with internal gaps are none
    # of them in the pinned universe, which is why research binds and live never has.  The divergence
    # is latent.
    #
    # The +13 in `beidou_cli` is the entry point the reachability guard demanded - correctly: a module
    # nothing can run is not a control.  `live status` now prints which rules bind, in Chinese, because
    # that command's output IS the hourly alert body and `test_alerts_are_chinese` allows three English
    # tokens in it.  Both guards caught this commit and both were right.
    #
    # Thirtieth raise OF THIS TABLE, 2026-09-15: +155 beidou_live - the cycle record given a
    # declaration, and its ledger reader stopped re-parsing from byte zero.
    #
    # `cycles.jsonl` is the widest interface in this package: ~42 keys written by three methods, read by
    # six modules under 192 distinct `.get()` names, declared nowhere.  Every defect found in this
    # package over the last two days was the same shape - one writer and one reader disagreeing about a
    # key, with nowhere for the disagreement to surface.  `cycle_record.KEYS` is the declaration and
    # `test_the_cycle_record_is_declared.py` is the join, run against the ENGINE rather than a
    # hand-built dict, because a hand-built row is exactly what let the L3 test pass while the
    # composition was wrong.  It earned its keep immediately: `crowding` and `metrics_snapshot` were
    # written by `run_cycle` and described in no declaration this commit's author had written.
    #
    # It is NOT a typed row, and that is a decision rather than an omission.  A field's absence in an
    # old row means "written before this field existed", so the readers' `.get()` plus `isinstance` is
    # correct handling; a dataclass with required fields would refuse to read the history the file
    # exists to keep.  What was missing was a place to compare, not types.
    #
    # The reader: `run_cycle` reads `cycles.jsonl` and `attribution.jsonl` twice each per cycle and
    # `report daily` reads cycles eleven times, and each read re-parsed the whole file - ~7 MB per cycle
    # at 319 rows, and the ledger grows ~48 MB a year, so ~190 MB re-parsed per cycle after a year.
    # `_append` writes whole lines and never rewrites one, so the parse now resumes from the cached
    # byte offset, with a full re-read whenever the file disagrees with the cache in ANY way (shrink,
    # rewrite, mtime backwards, or an offset that is not on a row boundary).  Eight tests, one of which
    # counts `json.loads` calls rather than trusting a clock.
    #
    # What this commit deliberately did NOT do: collapse the "seven near-identical backwards scans" the
    # review counted.  Reading them, they are not near-identical - `verify`'s three carry three
    # different predicates and one spends a paragraph on why it cannot be another, and
    # `reports.evidence_window`'s is not a lookup but a walk back to where the current construction run
    # began.  Folding those into a helper would delete the explanations, which is the opposite of
    # concentrating anything.  `latest()` takes the one question that really was asked twice.
    #
    # Twenty-ninth raise OF THIS TABLE, 2026-09-15: +178 beidou_alpha, and beidou_live ratcheted DOWN
    # by 67 - R8's state machine lifted out of the engine.
    #
    # `_risk_ladder` was an `async` private method reading `self.store`, `self.state`, `self.config` and
    # `self.alerts`, so nothing could evaluate the ladder without an engine.  Everything that needed to
    # know what it does therefore rewrote it: four replays in `scratchpad/` (`p32b`/`p32c`/`p32d`/`p32e`),
    # a second unread rung comparison in `risk_budget`, and no twin at all in `beidou_alpha`.  They had
    # drifted - one omits the `min(1.0, raw)` clamp, one divides by a module constant instead of the
    # running target, none replays the blind-reading hold - and D-035 cites 20.5pp, 0.4pp and q95 -66.5%
    # produced by them.  D-036 already requires one semantics per book-level guard, satisfied for
    # `daily_loss_pause` and `GROSS_CAPPED` via `exposure.py` and not for the larger scalar.
    #
    # The rule goes to `beidou_alpha` rather than anywhere in `beidou_live` because the backtest has to
    # replay it and `beidou_alpha` may import no `beidou_*` - so `ladder_step` takes rungs and grace as
    # plain values and `Policy` keeps the numbers and delegates.  One implementation, three readers.
    #
    # Behaviour-preserving, and not on assertion: the old body was kept as `_dead_risk_ladder` and both
    # were run over 128 combinations of standing rung x base x drawdown, comparing the cycle block, the
    # persisted rung AND the operator messages - the messages because paging is deduplicated on their
    # text, so a silent rewording is a real regression.  Identical on all 128; the old body was deleted
    # in this commit.  D-033's technique, for D-033's reason.
    #
    # Down 67 rather than left at the old number: a ratchet that only goes up would hand the package
    # free headroom every time something moves out of it.
    #
    # Twenty-eighth raise OF THIS TABLE, 2026-09-15: +34 beidou_live, which is three module headers -
    # `health.py` split into the four concepts it was holding.
    #
    # It held M-001 cycle health, DL-X1 liquidation distance, KILL-R19 margin mode and D-026 construction
    # identity, under a name that predicts one of them, with `__all__ = ["CycleHealth", "cycle_health"]`
    # naming two and four more public functions defined after it.  Understanding "did the construction
    # change?" meant bouncing between five modules, and the alias table sat in `health.py` for one
    # stated reason: `engine.py` would have imported it circularly.  That is a module chosen by an
    # import graph rather than by a concept.
    #
    # Now `health.py` (cycle health), `liquidation.py`, `account_shape.py`, `construction.py`.  Each
    # keeps its own direct importers - the split deliberately does NOT follow the review's suggestion to
    # move these into `engine.py`, which is already the largest module in the package and would have
    # turned four tested functions into private ones.  `construction.py` has no cycle to dodge: nothing
    # in it imports the engine.
    #
    # The 34 lines are the price of separability, not new behaviour: three docstrings and three import
    # blocks, no logic added or removed.  Pure moves otherwise, with every importer repointed.
    #
    # Twenty-seventh raise OF THIS TABLE, 2026-09-15: +60 beidou_live, so the venue and market ports
    # describe what the engine reads off them.
    #
    # `ports.py` already carries this lesson in its own words, about `SignalModel`: *a protocol that
    # omits what the caller actually reads has stopped describing the contract*, written after mypy saw
    # one of four accesses and said nothing about the other three.  `Venue` and `MarketData` had the
    # same defect and it ran in both directions at once.  Seven members the engine probes -
    # `sync_clock`, `hedge_mode`, `margin_mode`, `leverage_brackets`, `mark`, `server_time_ms`,
    # `client` - appeared nowhere in the file.  And two that DID appear, `venue_time_ms` and
    # `user_trades`, were declared REQUIRED while every call site probed them and fell back: to the
    # host clock, which is D-030's silent hour, and to full attribution, which is D-032's.
    #
    # `getattr(obj, "name", None)` is invisible to a type checker by construction, so the guard cannot
    # be mypy and the declaration alone would rot. `VenueProbes` / `MarketDataProbes` hold the optional
    # surface, and `test_the_ports_describe_what_the_engine_reads.py` compares the set of names the
    # engine probes against the set the ports declare - in both directions, so a tenth probe fails and
    # a stale declaration fails too.  `funding_history` is the one exemption and it is named with its
    # reason: that probe REFUSES (D-023 closing KILL-027) rather than falling back, which is what makes
    # it required.
    #
    # The test cost the omission was hiding, now visible: `FakeMarketData` had no `server_time_ms`, so
    # every engine test built on it took `_clock_skew`'s all-None branch and D-025's skew/alignment/jump
    # instrumentation was reachable only through one bespoke subclass.  It is now a setting on the
    # shared fake.  Absence stays the DEFAULT on purpose - flipping it changed what two hundred existing
    # tests measure, and broke two of them, one of which says `# no server_time_ms` in its own body.
    #
    # Twenty-sixth raise OF THIS TABLE, 2026-09-14: +40 beidou_live, +9 beidou_cli, so that L3's gate
    # can fail.
    #
    # `soak.passes` gates `live soak --check` on whether any ERROR cycle DECIDED anything, reading five
    # keys off the cycle row.  `guarded_cycle` built that row from scratch in its exception handler,
    # with `orders` hardcoded to `[]` and the other four absent - so `_decided` returned `()` on every
    # engine-produced record and the no-decision half could not fail, whatever the loop had done.  The
    # reachable case is ordinary: `run_cycle` executes orders and only then runs `_quarantine`,
    # `_summarize` and `_finish_cycle`, so a raise in any of those three leaves fills on the venue and
    # writes a row saying none were placed.  `test_l3s_criterion_is_ruled` asserted the gate worked by
    # hand-building a row shape the writer could not emit: the test passed, the composition did not.
    #
    # Most of the 40 is the half that is not the fix.  Carrying the keys across made two of them
    # readable for the first time, and for two of them emptiness is the WRONG question:
    # `_risk_ladder` returns ten keys on the quietest cycle and is never `{}`, and a pinned universe
    # writes `adopted: False` once a day - a proposal recorded and deliberately not taken.  Left as an
    # emptiness test, this commit would have made every failed cycle look like a decision and let the
    # loop measuring itself retire a book, which is KILL-AR-20 pointing the other way.  `_acted` asks
    # `acting` and `adopted` instead, and four tests hold the distinction.
    #
    # `no_decisions()` is one function both halves name, so a sixth key cannot reach the reader without
    # the writer gaining it.  The 9 in `beidou_cli` pass `Policy.no_decision_phases` into `score`,
    # which R10 always meant to be the phase list's only home and which no call site read.
    #
    # Twenty-fifth raise OF THIS TABLE, 2026-09-14: +4 beidou_live, for one assignment and the three
    # lines that say why it is there.
    #
    # `startup` filters the universe to what the venue will actually trade and did not write the result
    # back to `state.universe`.  The other three mutation sites all pair the two lists; this one was
    # the exception, and `store.save` at the end of `startup` then persisted the PRE-filter list.
    #
    # Four lines rather than one because the failure is invisible where it is caused.  `live verify`
    # (M-011) reads only `state.universe`, so the reproduction ranked and demeaned over a population
    # strictly larger than the one the cycle scored - which moves EVERY cross-sectional contribution,
    # not the dropped symbol's.  A reader who finds this assignment and deletes it as redundant gets a
    # globally red monitor with nothing in the record able to name the cause, which is KILL-027's shape
    # one floor down from where KILL-027 was closed.  The comment names D-042 so the contract it
    # implements is one grep away.
    #
    # It does not self-heal under the shipped profile: `alpha_registry.yaml` pins 17 symbols, and a
    # pinned universe makes `_maybe_refresh_universe` return before it touches state.  Only a restart
    # with every pinned symbol tradable clears it.
    # Twenty-third raise OF THIS TABLE, 2026-09-14: +91 beidou_governance for Phase 0's fifth
    # attribution route, ruled by the operator after this session declined to write it itself.
    #
    # The decline is the reason the comment is this long.  The route excuses the artefacts of whoever
    # proposes it - this session produced the two reports it attributes - and a rule written in that
    # position is the shape RESEARCH_LOG's Q4c ruling had just refused: loosening a gate in the
    # direction that flatters the incumbent.  So the lines here are not spent on the happy path, which
    # is four lines; they are spent on the three conditions that make it useless as a blank cheque,
    # and on saying out loud which one is load-bearing.
    #
    # Identical run settings (`ARM_SETTINGS`) says a pair is a design rather than a coincidence.
    # Exactly one differing construction key says it measures a knob rather than proposing a book.
    # And the anchor - one arm must agree with the ADOPTED pointer on every shared key - is the one
    # that matters: without it two novel constructions could be paired with each other and both walk.
    # Four of the seven tests are that loophole, not the feature.
    #
    # `_construction_agrees` compares shared keys only, on `construction_problems`' rule and for its
    # reason: the pointer adopted 2026-09-13 predates `flat_inside_band`, and comparing key SETS would
    # let every newly added construction key quietly disqualify the incumbent from anchoring anything.
    #
    # Twenty-second raise OF THIS TABLE, 2026-09-14: +66 beidou_live, +26 beidou_alpha for two knobs,
    # both shipped OFF, that name the two ways the band manufactures a position it cannot close.
    # Roughly three quarters of both is comment, and the comments are the deliverable here: each knob
    # is one branch, and what has to survive is WHY False is the current book and what turning it on
    # would cost.
    #
    # `exempt_crossings` is not a new idea, it is a divergence.  `beidou_alpha.portfolio`'s
    # `apply_no_trade_band` docstring reads "Exits to exactly zero, entries from zero and sign flips
    # are always executed; only same-direction resizing is suppressed"; `plan_rebalance` applies the
    # absolute band to all of them.  `model.py`'s D-033 moved the band from the model layer to the
    # rebalancer on the stated ground that the rebalancer "applies the identical rule".  It does not,
    # and nothing in the tree said so - the claim sat in a docstring on one side and was contradicted
    # by a branch on the other.  The consequence is not academic: the exit overlay fires by setting the
    # weight to 0 (`exits.py:72`) and then goes through the band, so a stop-loss on a sub-band position
    # plans no order at all.  ENAUSDT spent 45 cycles in that state.
    #
    # `flat_inside_band` is the other half: the stub does not appear, it is manufactured.  A reduction
    # is sized to the model's target and no rule forbids that target landing inside the band; 7 of 170
    # armed fills (4%) landed there.  The invariant is "never hold a position smaller than the absolute
    # band", which covers reductions, sign flips and sub-band entries in one sentence - and it has to,
    # because the first live stub (2026-09-10, +212.56 -> -51.10) was made by a flip, which a
    # reduction-only rule would not have caught.
    #
    # Both are declared in `construction_fingerprint` rather than left as bare code, because a knob the
    # record cannot see is the other half of D-036; CONSTRUCTION_ALIASES v7 carries the proof that
    # False on both sides is byte-identical.  Turning either ON is a construction change and belongs to
    # the operator after the 2026-10-13 freeze.
    #
    # Twenty-first raise OF THIS TABLE, 2026-09-14: +13 beidou_live so the band names the positions it
    # cannot close.  Twelve of the thirteen are the comment; the code is one predicate and one field.
    #
    # `reports.plan_gaps` has always documented `blocked_exit` as "a position smaller than the band can
    # never be closed to zero", which is a statement about the POSITION.  The rebalancer tested the
    # TARGET instead - `abs(target_notional) < 1e-9` - and a decaying model target essentially never
    # lands on exactly zero, so in 320 armed cycles the label fired zero times while the condition it
    # names held for 44.  ENAUSDT sat at -79 contracts (-11.19 USDT against a 54.32 USDT band) for
    # 45 cycles and `report daily` printed `blocked_exit: []`; the operator found it by eye in the
    # venue UI.  The predicate now reads `abs(current_notional) < threshold`, which is the same
    # sentence the docstring was already making, and the row carries `current_notional` so the reader
    # can re-derive the verdict instead of trusting the label.
    #
    # No order changes - all three band outcomes were already "no order" and still are.  The lines
    # buy the difference between a stuck position and a symbol that did not need trading, which is
    # exactly what no instrument in the system could tell apart on 2026-09-14.
    #
    # Twenty-third raise OF THIS TABLE, 2026-09-14: caliber ④, the operator's ruling on Q4c -
    # `range_end` folds to a policy-set granularity before two ledger rows are compared.
    #
    # Q4c measured the half that needed measuring.  The same 676 expressions scored on both universes
    # gave a joint `N_exact` of 641 against a single-universe 337 - ratio 1.90, so **the second
    # universe is very nearly a full second look and cross-universe re-charges stay charged**.  Only
    # `range_end` collapses, and that half is arithmetic: a signal reads only data up to bar t, so the
    # same expression over the same start, symbols and construction produces an IDENTICAL stream on the
    # shared index when the range ends a few days later.  Zero added independence, not an estimate.
    #
    # The argument does not say "drop the field" - it holds for two years later too, and that IS a
    # second look.  So the fold needs a granularity, a granularity is a threshold, and R10 puts those in
    # `Policy` (0.3.4 -> 0.3.5).  `beidou_alpha` is the lower layer and cannot import it, so the value
    # is threaded through as a REQUIRED keyword.  That is most of the line count and most of the churn:
    # 49 call sites, none of which may default.  A default here would let a caller quietly get the
    # pre-ruling rule while believing it had the new one - the invisible-default shape this repo was
    # bitten by twice on 2026-09-14 alone.
    #
    # The trap this could have shipped is in `dsr_inputs`: its `exclude` set was assembled by hand in
    # the shape of a signature.  Quantise the fold and leave the exclusion literal, and every replay
    # stops matching and is charged a second time - silently, in the direction that looks rigorous.
    # Both now go through `fold_key`, and a test fails if they ever disagree.
    #
    # What it costs the incumbents, measured on the real ledger AFTER the change rather than estimated
    # before it: `tsmom` 111 -> 105 and `flow` 45 -> 43 folded trials, which LOWERS the bar they face.
    # `mined` goes 2,073 -> 1,559.  An earlier note in this session put tsmom at "137 -> 101"; that
    # compared RAW ROWS against folded trials and overstated the incumbent's exposure by six times.
    # `unique_trials` already dropped exact copies before caliber ④ existed, so the honest before is the
    # folded before.  The granularity itself is not load-bearing here - 7, 14 and 30 days all give the
    # same counts - so 7 is taken as the one that folds least among those that fold the case at all.
    # Twentieth raise OF THIS TABLE, 2026-09-14: +15 beidou_alpha, +33 beidou_cli so a measurement can
    # reproduce its own headline number.
    #
    # The first `--measure` run cost 45 minutes and wrote one scalar, `effective_trials: 193.0`.
    # Everything that could be used to disagree with it - the correlation structure it came from - died
    # with the process, so validating Li & Ji against this ledger meant spending the 45 minutes again.
    # That is a defect in the ARTEFACT, not a limit of the estimator, and it is this round's own lesson
    # arriving one floor down: a number nobody can argue with.
    #
    # Two changes, and the second is what makes the first mean anything.  `effective_trials` splits
    # into a returns-shaped front door over `effective_trials_from_correlation`, so the estimator has
    # one implementation and a persisted matrix feeds the same code the run used.  And `--measure`
    # writes the real 676x676 correlation beside the report, with its sha256 and shape - then computes
    # the reported count FROM that file, so the two cannot drift.  A same-spectrum surrogate would not
    # do: Li & Ji reads only eigenvalues, but the distribution of the MAXIMUM reads the eigenvectors
    # too, so validating against a surrogate validates a different family.
    #
    # One line of it is a claim that was made out loud before it was checked.  "193.0 is not a rounding
    # artefact, the count must be an integer" is true of the estimator (trace == n, floors are
    # integers) and false of the artefact: `eigvalsh` is not exact, 40 independent columns return
    # 39.99999999999999, and the run reported 193.00000000000009.  The test now asserts integrality to
    # 1e-9 and says why - the looser claim would have passed a test written to match it.
    # Twentieth raise OF THIS TABLE, 2026-09-14: +21 beidou_cli, all docstring, and the second half of
    # a fix two sessions made at the same time.
    #
    # The eighteenth raise below corrected `gate_cmd`'s "Read-only" after a session ran it as a read-only
    # check and it appended a verdict row.  Two things it did not reach.  The MODULE header still opened
    # with "Phase 0 is read-only: replay the rules, write nothing" - the same claim one level up, met
    # first, and false for four commands (`gate`/`recheck` append verdicts, `enable` writes a flag,
    # `replay --out` writes an artefact).  And neither docstring said where the write's duplicate problem
    # went.
    #
    # The second one is why this is worth 21 lines rather than 3.  A reader who now learns the command
    # writes will reach for the obvious repair - delete the write, make the word true - and that repair
    # was already rejected on 2026-09-12 in favour of keying idempotence on the ruling
    # `(gate, subject, call, reasons)` instead of on the date.  The pointer to
    # `tests/governance/test_a_reread_of_a_ruling_is_not_a_second_verdict.py` is what stops the next
    # correct-looking change from undoing a decided one; that file also calls `gate` "a read-only
    # command" while fixing exactly this, which is where the phrase both sessions just corrected came
    # from.  Documenting the drift's SOURCE costs more lines than documenting the drift.
    #
    # Duplicated work recorded rather than hidden: this session wrote its own `gate_cmd` correction,
    # found the eighteenth raise on merging, and discarded its branch rather than resolving a conflict
    # into redundant prose.  Two sessions reached the same defect within the hour from opposite ends -
    # one ran the command, one read the docstring.
    #
    # Nineteenth raise OF THIS TABLE, 2026-09-14: beidou_cli for the operator's Q4 ruling - `research
    # mine --measure`, a run that scores the space to COUNT it rather than to search it.
    #
    # `effective_trials` (Li & Ji) has been reported-never-substituted since it was written, and the
    # debt is documented as owed rather than guessed for a concrete reason: the ledger stores Sharpes,
    # not return series, so a ledger-wide N_eff cannot be computed from anything the gate is given.
    # Computing it needs the space re-scored with the streams kept - and under R1/R2 that is a mine
    # round, which charges 676 rows and raises the very bar the number is about.  The measurement could
    # not be bought with the thing it measures.
    #
    # Three properties, and the middle one is what keeps this from being the loophole it resembles:
    # it charges nothing (no row reaches the ledger, asserted rather than described - 2026-09-08's
    # command claimed not to charge while charging 514, and nobody found out until someone counted);
    # it RANKS nothing (the artefact carries distribution-level readings and no candidate rows, because
    # "free to look at 676 and then declare one" is a multiple-testing hole with a flag on it, while
    # "free to count" is not); and it is not an enumeration for R2, so measuring does not spend the
    # space a later real round needs.
    #
    # It does not adopt anything.  R0 still reads the raw ledger count, the test that pins
    # `effective_trials` out of the gate is untouched, and substituting it is a separate ruling this
    # only prices.
    # Eighteenth raise OF THIS TABLE, 2026-09-14: +8 beidou_cli, and all eight are a docstring saying
    # what its own function does.  `governance gate`'s first line was "Read-only, and it stays read-only
    # ...", and thirty lines below it the same function appends a `family_gate` row to
    # `governance/verdicts.jsonl` for every PASS/FAIL.  A session read the first line, ran the command
    # as a check, and appended a row it had not intended to write.  Exactly 2026-09-08's shape ("a
    # command that claims not to charge is charging"), and that entry's ruling applies here too: the
    # contradiction is the defect, not the writing.  The writing is correct - a gate that decided and
    # left no record is a gate nobody can audit - so the line moved, not the behaviour.
    # Seventeenth raise OF THIS TABLE, 2026-09-14: +49 beidou_alpha, +9 beidou_cli for the operator's
    # Q3 ruling - D-018's drawdown clause now compares two books at the same risk.
    #
    # The clause subtracts the main book's out-of-sample drawdown from the total book's, and those two
    # do not carry the same risk.  Measured 2026-09-07 on the `594a12f9` sleeve at fraction 1/3: a
    # stream correlated 0.236 took the book from 32.24% vol to 35.66%, +10.6%.  Any sleeve that can add
    # Sharpe adds vol and therefore adds drawdown, so the 1pp allowance was charged against a bigger
    # book than the one it was written for - against SCALE rather than against the sleeve.  Scaling the
    # total back to the main book's vol keeps the Sharpe gain to the digit (+0.288) and moves the cost
    # from +4.27pp to +1.87pp.
    #
    # Later candidates only, enforced structurally rather than by a date: the equal-risk number exists
    # only in reports that carry it, `marginal_checks` reads it when present and the raw difference when
    # not, so no archived verdict moves and there is no cut-off for anyone to remember.  The raw number
    # stays beside the new one - a ruling that deletes its own evidence cannot be argued with later.
    #
    # It does not rescue the candidate it came from: +1.87pp against a 1pp allowance still fails, which
    # is exactly why the ruling could be made on its own terms rather than as that candidate's appeal.
    # The scaling is applied to the RETURNS and the drawdown recomputed, not to the drawdown itself - a
    # drawdown is a path statistic and the vol-ratio shortcut is an approximation that would need a
    # caveat this way does not.
    # Sixteenth raise OF THIS TABLE, 2026-09-14: +40 beidou_governance for the operator's Q1 ruling -
    # a report that predates `gate` may be recomputed, but only if it proves its own rule.
    #
    # KILL-Q3 added `gate` so a stored threshold could not outlive the rule that made it, and
    # `read_gate` refused any block without it.  Correct, and total: all seven mined validations
    # predate the field, so the production recheck had no opinion about any of them - including the
    # only candidate this pipeline has ever passed, whose admissibility is an open ruling.
    #
    # The identity may stand in for the label because it is FALSIFIABLE.  `threshold_annual` is the raw
    # quantile times `sqrt(bars_per_year)`, so `threshold / max_sharpe_quantile(n, variance, alpha)`
    # recovers the annualisation, and a threshold produced by any other rule leaves a different number
    # there.  Measured on the seven before the rule was written: 594a12f9 implies 93.594872 = sqrt(8760)
    # to 0.000e+00; the other six imply 0.805x that, which is `E[max] / quantile(0.95)` - the
    # expectation KILL-Q3 replaced because it "admitted pure noise at 43.5%".
    #
    # So this admits ONE report and refuses SIX.  That is the point and it is why the lines are worth
    # it: the check is not a way past the refusal, it is the refusal's own criterion recovered from the
    # numbers, and it dates six reports the field would have dated for us.  An explicit wrong label
    # still refuses - a statement beats an inference - and a report with no readable `interval` refuses
    # too, because without one there is no `sqrt(bars_per_year)` to hold the identity against.
    # Fifteenth raise OF THIS TABLE, 2026-09-14: +100 beidou_governance, +10 beidou_cli for R2b, the
    # first reason the research loop has ever had to stop mining.
    #
    # `next_action` returned MINE whenever R2 held (the space digest moved) and R1 still had a round.
    # Nothing in it asked whether another round COULD produce an admissible candidate.  It could not,
    # and had not been able to since 2026-09-09: the `mined` bucket's D-028 threshold rises
    # monotonically in N, and that day it rose past the out-of-sample Sharpe of the best candidate the
    # space has ever produced (1.7862, `mined_594a12f9307a15d9`).  Two more rounds ran after it, each
    # charging its rows and raising the bar further.
    #
    # Why it may live in `scheduler`, which holds no thresholds of its own: it IS no threshold.  Both
    # sides are measurements already on disk - the best mined validation's own `oos_selection` block
    # (its OOS Sharpe, its variance, and the `threshold_annual`/`n_trials` pair that pins the
    # annualisation exactly, so nothing assumes a bars-per-year) against the bucket's N today.  Filed
    # under R2's provenance for the same reason: it answers R2's question from a second measurement,
    # not a new question.
    #
    # It refuses SPENDING and nothing else.  No verdict moves, `n_trials` is untouched, and nothing
    # about what may reach the book changes; an append-only ledger is what makes not-spending the
    # reversible direction.  Mining resumes by itself when either number moves.
    #
    # ~35 of the 100 are `gate_has_passed_the_space` and its docstring; the rest is the assembler half,
    # which is where the expensive part is.  A `Context` field nothing fills is inert - the shape that
    # silently forked 96 cycles in 2026-09 - so the field arrives with its provenance sentence and a
    # `known` flag that is false when the CALLER did not supply the bucket, not when the bucket is
    # empty.  An empty bucket is a reading; a forgotten argument is not, and conflating them would
    # fail permissive: a round spent on the strength of an argument nobody passed.
    # Thirteenth raise OF THIS TABLE, 2026-09-14 - the number collides with the 2026-09-04 entry at the
    # top of the file, which is a different series and already records that this happens.  With the
    # sentence the rule requires: +18 beidou_live for one number nobody could see, `equity_over_peak`
    # on R8's reading.
    #
    # R8 divides by `peak`, the running max of the book's own P&L path, which moves only when the book
    # makes a new high.  The loss in that numerator comes from positions sized `weight x CURRENT
    # equity`, and 52.65% of this account is BTC collateral.  So a move of x% of the book reads as
    # `x% * equity / peak`, and while the book sits under its own high-water mark the factor has no
    # path back to 1: `peak` is pinned and equity keeps floating.  Measured over 287 live cycles the
    # same day - 1.000 at the baseline, 1.0154 eleven days later, above 1 on 261 of them.
    #
    # Why it is worth 18 lines TODAY, when the factor is 1.5%: until 2026-09-13T22:00Z the ladder read
    # an income-only ruler that never reached its first rung in five years and seven months, so a
    # distortion of where that rung sits cost nothing.  The ruler changed at 22:00Z and the rungs became
    # reachable; from that cycle on this factor moves WHEN the book gets throttled.  Extrapolated to a
    # factor of 2.0 the -35% rung fires at a -17.5% book move - which is, by a completely different
    # route, the -17% rescaling that the same day's analysis had just REFUTED as a deliberate proposal.
    # The difference is that a rescaling is one line of YAML someone argues about, and this one answers
    # to nobody's decision and appears in no reading.  That asymmetry is the whole case for the lines.
    #
    # Reported, never applied: dividing by current equity would change when the ladder fires, which is
    # a risk decision.  Most of the 18 lines are the two docstrings saying so, because the obvious
    # "simplification" for a later reader is to use the number instead of printing it.
    # Fourteenth raise OF THIS TABLE, 2026-09-14 - the second of the day.  The entry above is the
    # thirteenth, raised by a parallel session within the hour for unrelated work; the two are
    # sequential in this table's own series, which is the series the entry above is numbering.  With
    # the sentence the rule requires: +63 beidou_alpha, +59 beidou_cli, bought by a misreading that no
    # artefact could have contradicted.  The `mined` bucket held 2,731 rows and 676 distinct
    # `param_key`s - 4.04 rows per hypothesis, deliberately, because
    # `signature` folds on the data range and the construction too (KILL-Q5, conservative by design).
    # Every number the reports carried counted ROWS: `charged`, `candidates`, `declared_remainder`,
    # `family_prior.before/after`, `ledger_rows`, `ledger_trials`, `duplicate_rows`, `replayed_rows`.
    # Eight fields, one quantity.  So an analysis read `family_prior.after` as a candidate count,
    # concluded the space had been searched 2,731 ways, and judged the miner on it; nothing on disk
    # said otherwise.  `distinct_hypotheses` is the ninth field and the first that counts something
    # else.  It is REPORTED and never gated - `n_trials` is untouched and so is the folding rule, both
    # pinned by tests - for the reason `all_trials` exists: a number nobody can see is a number nobody
    # can argue with.
    #
    # The second half is `scoring_reproduction`, and it is the more expensive one because it needed a
    # comparison that did not exist.  R2 refuses a re-run of an enumerated space unless `--reauthorize`
    # carries a reason, and the reasons have been specific and checkable.  Nothing has ever recorded
    # whether one came true.  On 2026-09-09 one did not: 09:50Z charged 658 rows under "3b49af8 fixed
    # the 90 metrics errors, re-run them" and returned all 658 Sharpes identical to 08:29Z, the same 90
    # errors included, because that fix had not reached this path (`f4ea9de`, that evening, is what
    # collected them at 17:25Z).  A round whose authorisation did not come true, charged in full, and
    # the only way to learn it was to diff two reports by hand.  Whether such rows stay charged is
    # Q7's kind of ruling and nothing here makes it - `bought_nothing` is printed and filed, not
    # enforced.
    #
    # The cheapest of the three is `ledger_redirection`, ~14 lines: `resolve_ledger_path` calls
    # `BEIDOU_TRIALS_LEDGER` a variable "whose only possible purpose is to not be charged" and then
    # returned the path with nothing saying it had been overridden, so a run that charged the shared
    # book and a run that charged a scratch file printed the same thing.  Reported, never refused -
    # every test that writes a ledger redirects it, and a guard would break them.
    #
    # Most of the 122 lines are docstrings, and that is the point of them: each of the three states
    # the incident it exists for, because the next reader's failure mode is the one this entry is
    # about - reading a number without knowing what it counts.
    # Twelfth raise, 2026-09-14, and the sentence the rule requires: +33 beidou_live, +3 beidou_cli for
    # M-007's bar.  Two numbers governed one quantity and the looser one was doing the judging: this
    # metric compared realized standing margin against the PLAN's 50%, while the profile declares
    # `margin_cap` 0.40 and D-016 checks that only at startup, against the CONFIG, never against the
    # reading.  So realized margin could sit anywhere in 40-50% - above the declared policy - and M-007
    # read OK.  That is the `max_slippage_bps` shape the profile already records: a bar 1.25x looser
    # than the policy it stands for cannot fail before the policy is already breached.
    #
    # The second half is why it would not have been read anyway.  `over_budget` was computed, rendered
    # into the daily markdown, and never routed - `daily_alerts` had no margin branch at all, so the
    # finding reached neither list.  D-038's shape, a third time this week.  It is a NOTICE and not an
    # alert, by the test the other entries there use: a breach means gross/equity drifted past
    # `max_gross` between rebalances, and stage 3 re-clips it at the next one, so there is nothing to
    # do inside the hour.
    #
    # Most of the 33 lines are the docstring and that routing comment.  The plan's 50% is kept as
    # `plan_budget` rather than deleted, for the reason this file exists: what a corrected ruler was
    # wrong ABOUT is the part a later reader needs.
    #
    # One line of the 33 is a bug this nearly shipped.  The budget started life as a local named `bar`,
    # and the rejection loop thirty lines below rebinds `bar` to `bar_open_ms` - so with any trade row
    # present, `over_budget` compared a margin share against a millisecond timestamp and was always
    # False.  A silent pass, and the first four tests could not see it because their fixtures record no
    # trades.  mypy caught it, not the suite; the test that now pins it was written to fail first and
    # did (`budget` read 1788000000000).  Worth the line count: a guard that cannot fail is the exact
    # thing this whole entry is about, and it almost got re-introduced in the commit fixing it.
    # Tenth raise, 2026-09-14, and the sentence the rule requires: +50 beidou_live, +6 beidou_cli,
    # bought by the same operator question for the FIFTH time - why is every order at 5x.  D-038 is the
    # entry that answered it the third time, and its diagnosis was not "explain better" but "a correct
    # fact no instrument states is indistinguishable from an unproven one".  It built M-015 and wired it
    # into the daily report, where it renders well.  The question came back anyway, because the command
    # an operator actually reaches for is `live status`, and that one dumps `state.to_dict()` - in which
    # the only per-symbol number is `leverage_set`, eighteen identical 5s, with nothing beside it.  The
    # same hole D-038 measured, one command over, and it went unnoticed because D-038 looked where the
    # question had been answered rather than where it gets asked.
    #
    # The lines are `latest_risk_adaptation` (the day comes from `_day_of`, the ruler `risk_adaptation`
    # already buckets by, so the command needs no day argument and 00:30Z does not read as "no data")
    # and `risk_adaptation_headline`, most of which is the docstring recording two decisions a later
    # reader would otherwise undo: the venue half is carried on refusals, because a missing reading does
    # not make the operator's question go away; and the share reuses `1 - compression` rather than a
    # second expression of the same quantity, which is how two rulers end up in one file.
    #
    # What was deliberately NOT bought, so that nobody adds it later as an oversight: the OK reading does
    # not go onto the hourly webhook.  That path is `daily_alerts`' `alerts` list, it fires every hour,
    # and a healthy-state number repeated hourly is KILL-R7's measured shape - 36 identical lines over 36
    # hours, unhandled.  A daily carrier for healthy readings may be right; it is not priced, so it is
    # not here.  Non-alpha growth against the 90% target, and this one's excuse is on the record: four
    # prior rounds of analysis were more expensive than these 56 lines.
    #
    # Ninth raise, 2026-09-13, and the sentence the rule requires.  One commit, seven packages, because
    # nine parallel workers all landed against a table with ZERO margin in every row - and that is worth
    # recording as a finding rather than only as an inconvenience: a ratchet with no headroom stops being
    # a ratchet and becomes a tax on the first honest change, paid in deleted comments.  The first worker
    # to hit it had already golfed its two files from +16/+6 down to +10/+4 before anyone noticed the
    # table was the binding constraint on all nine.  Nothing was shaved after that.
    #
    # +455 beidou_alpha.  The larger half (~251) is `overlays/exits.py`: a bar with no close used to fail
    # the `price > 0` at the end of the `held != 0` condition, fall PAST the whole exit block and land in
    # `_enter`, which anchored a new ExitState at the NaN price and erased the entry - so one missing bar
    # silently disarmed the stop for the rest of that episode (reproduced at 100 -> 89 -> 87 with
    # stop_loss 6 and sigma_1d 0.02: 6.5 units adverse, no fire).  The lines are that reproduction, the
    # `stale_carry_bars` bound that decides when "a gap" becomes "the symbol is gone", the sweep table
    # behind the default of 2, and a second, vectorised engine that is asserted bit-identical to
    # `exit_step` rather than trusted (10x: 13.8s -> 1.4s on 49,937 x 205, which is what `research
    # overlay`'s eleven cells were paying eleven times).  The rest: +156 for calendar-anchored re-fit
    # boundaries in `features`/`portfolio` - the GARCH and HRP options re-fitted on `t % refit_bars`, so
    # the same calendar bar read differently depending on where the panel was sliced, D-033's lesson in a
    # place nobody had looked - plus `flow`'s warmup-fill knob and its measurement; +33 for `cpcv_splits`'
    # docstring, which records that purge and embargo block opposite sides of a test block and that CPCV,
    # unlike walk-forward, has both live; +15 for the participation replay's `exempt_reductions`.
    "beidou_alpha": 9_011,
    # +694 beidou_live, the biggest raise on this page and the one that buys the least alpha.  It is the
    # cost of the 2026-09-13 review's second finding: `state.json` is the ONLY copy of the income
    # watermark, the equity high-water mark, the exit anchors and the D-005 hold seeds, and `load()`
    # answered a corrupt file with a fresh `LiveState` - no raise, no alert, no row.  M-010 is the only
    # clean out-of-sample series this system has (KILL-006), and that path could take a stretch out of it
    # that nothing afterwards could detect.  ~348 for the refusal, the fsync `_atomic_write` never had
    # while `_append` six lines away had written six lines of comment about needing one, the bounded
    # dropped-input rule (one bad REST answer used to flatten a symbol and re-open it next cycle), the
    # backoff writing the bars it slept through so M-Q03's threshold of zero can see them, and bounded
    # order concurrency that is bit-identical at its default of 1.  ~202 for `attribution_coverage` and
    # the three protocol members `engine` had been reading past `ports.SignalModel` to get at.  ~57 for
    # the flip's margin arithmetic and the snapshot's overlapped reads; ~69 for the merge itself - the
    # seam that lets `live flatten` run without a readable state file, because an emergency exit a
    # half-written file can block is a worse failure than the one the refusal prevents, and the file is
    # most likely half-written exactly when someone reaches for flatten.
    # +27 more on the merge itself: `dropped_after` moved onto `LiveConfig` so the fingerprint can
    # see it, the `inputs` block that carries it, and `restart_cost` learning that a backoff row is
    # a missed rebalance but not a restart - a six-hour outage was about to read as six restarts.
    # +15 more, found by performing the restart rather than reasoning about it: the SKIPPED heartbeat
    # a restart writes carried no `registry`, so `live status --check` answered from the PREVIOUS
    # process's cycle row and reported a disagreement the restart had just resolved - non-zero from
    # 11:03Z until the next bar closed at 12:00Z, with the hourly job firing at :10 inside that
    # window every time.  The instrument could not answer at the one moment its answer had changed.
    # Eleventh raise, 2026-09-14, and the sentence the rule requires: +31 net in beidou_live for R8's
    # first rung, which could not place an order.  At vol_target 0.30 - the k the loop ran from R8's
    # wiring on 2026-09-09 until the 0.60 restart - the rung multiplies every weight by 0.225/0.30 =
    # 0.75, so a book standing at its target needs an order of 0.25 x |current|, inside
    # `no_trade_rel_band`'s 0.40 x |current|.  Between -35% and -50% attributed drawdown the ladder
    # therefore de-escalated nothing, and the record could not say so: `risk_ladder.acting` read true
    # and `skipped[]` read NO_TRADE_BAND, each correct alone.  Of the 33 lines added, 11 are the fix and
    # its signature docstring and 22 are the two comment blocks carrying the arithmetic - including why
    # the rule must not rest on k, since 96d659ae's O-3 put the first rung back at 0.75 while restating
    # the ladder against a wider budget, so the coincidence recurs at the next re-scale.  Two existing
    # lines were replaced, hence 33 added against 31 net.
    # +54 more beidou_live (8_683 -> 8_737), 2026-09-14, and the sentence the rule requires: R8's ruler
    # now carries the book's UNREALISED P&L, and `_risk_ladder` clamps a scalar that would add size.
    # The measurement that bought them: over 2021-2026 at k=0.60 the mark-to-market ruler spends 785
    # bars past the first rung and the income-only ruler spends 0.  Five years and seven months, zero
    # firings - a control that cannot be distinguished from its own absence.  Taking collateral
    # repricing out of the numerator is what KILL-AR-05 asked for and is unchanged; taking the book's
    # own open positions out was never part of it and is what made the ladder inert.
    #
    # The clamp is a second defect the first one hid: `throttle_scalar` returns an ABSOLUTE vol target
    # and the engine divides by the running one, so rungs calibrated for a larger k give a scalar above
    # 1 at a smaller one - an amplifier that fires when the book is already down.  Latent since R8 was
    # wired; it could never be observed while the ladder never fired.
    #
    # Most of the lines are those two paragraphs in the source, plus `marked_from` / `marked_rows` and
    # the `ruler` label, which exists because a row written before `unrealized` and a row written after
    # are not comparable and only a name can say so after the fact.
    #
    # Twenty-first raise OF THIS TABLE, 2026-09-14: +48 beidou_live, the alert dedup cache's clock.
    # `WebhookAlerts` deduplicates on `time.monotonic()` and PERSISTS those numbers so the hourly check
    # job - a fresh process every hour - can suppress a standing problem down to one line per hour
    # (KILL-R7's fix).  Monotonic counts from the machine's last boot, and the file outlives boots.
    # Measured on the live machine: after the 2026-09-10 reboot `check-verify` held 630465.2 against an
    # uptime of 374798.0, so `clock() - last` was -255667 - below any window, with no path back inside
    # the boot.  24 of that key's 28 FAIL lines were never delivered and it had 71 hours left to run.
    #
    # The lines are two readings per row instead of one, and the paragraphs saying why the obvious
    # repairs are wrong.  Comparing wall clocks INSTEAD would put every in-process decision at the mercy
    # of an NTP step; dropping only rows that are in the future leaves the mirror-image hole, where a
    # previous boot SHORTER than this one writes a number that reads as a recent delivery.  A row is
    # kept only when both clocks agree it is recent, and everything else is dropped - the module's own
    # invariant, three lines above the defect: "it may never be the reason a message does not go out,
    # and costs at most one duplicate."  A well-formed file broke what a malformed one was tested for.
    #
    # Two lines of it are a second, older defect the first one was hiding: the window was measured from
    # the moment the PROVIDER answered, while a caller's cadence runs from when it asked.  An 8-second
    # delivery at 11:10Z put the 12:10Z run 3592s later, inside a 3600s window - and that dropped FAIL
    # was the stale-governance-digest line, the one alert that day that needed the operator.  The window
    # default also drops to 3540s: equal to the caller's period it is a coin flip, and the loop's cycles
    # start a few seconds apart each hour, which is the losing side of it.
    #
    # Twenty-second raise OF THIS TABLE, 2026-09-14: +28 beidou_live, and it is the bill for the raise
    # above.  `compare_targets` diffed the reproduction against `state.last_contributions` over the
    # UNION of their keys, and that field is a MEMORY, not a record of what the cycle scored - the
    # engine merges each cycle into the previous ones on purpose, because D-005's hold seed needs a
    # departed symbol's last contribution the way the backtest's forward-fill does.  So every symbol
    # that leaves the universe reads as an unreproducible contribution, hourly, with no way back:
    # TRUMPUSDT left at 2026-09-13T21:13Z and `live verify --check` was red for the next 16 runs with
    # `max_target_diff` 0.0 throughout.  Nothing real was missed - the one non-TRUMP diff buried in
    # those 16 was float noise at 2.4e-08 - but M-011 could no longer distinguish a real failure from
    # its own, and the raise above had just made it audible once an hour.
    #
    # The lines are the population helper and the paragraph naming which set it is.  `leaving` is a
    # member here and not in the ranking population (P1-01, one field over in the same file, whose
    # docstring already warned that disagreeing about the population "would make this monitor's own
    # output the noisiest thing about it"): the engine passes universe+leaving to the model and
    # withholds the exits only from the cross-section, so an exiting symbol IS scored.  A state that
    # declares no universe is compared on everything rather than on nothing - an undeclared population
    # is not a licence to check less, the same direction the alert cache now fails in.
    #
    # Twenty-third raise OF THIS TABLE, 2026-09-14: +2 beidou_live, and they are an erratum on the
    # twenty-first.  Moving the dedup default to 3540 left four places in the tree reasoning from
    # "`dedup_window_seconds` equals the job's period", and the shipped profile plus a second hardcoded
    # fallback kept the LOOP on 3600 while the check job moved - two windows against one shared state
    # file, under a comment that says "One dedup window across every process that can alert".  Each
    # conclusion survived (a standing fact must leave the paging path, and that is true at any window);
    # the stated fact did not, so the two in `reports.py` now argue from what is invariant instead.
    #
    # Twenty-fourth raise OF THIS TABLE, 2026-09-14 - and the series has a duplicate in it: a parallel
    # session numbered its `+15 beidou_alpha, +33 beidou_cli` entry "Twentieth" within the hour,
    # which is the second entry to carry that number.  Recorded rather than renumbered, per the
    # thirteenth entry's own precedent - it is already pushed, and a count nobody can reconstruct is
    # worse than a count with its collisions written down.  This one keeps counting from the entries
    # that were in the file when it was written.  +17 beidou_live, all of it the paragraph beside
    # `grace_seconds`, which went 5.0 -> 20.0.  `SETTLE_SECONDS` is 15.0 and was MEASURED (the venue
    # finalises `taker_buy_quote` / `quote_volume` last, and `flow` is the only book that reads them),
    # so a loop waking 5s after the close read every bar mid-aggregation - every one, not the 56% that
    # measuring the cycle's row-WRITE time suggests; the fetch is the first thing the cycle does.
    #
    # The paragraph is worth more than the number because the number looks like tuning and is not.
    # It records what the change does NOT buy: the largest target difference an unsettled read ever
    # produced was 441x below the no-trade band, so it has never changed an order and could not have.
    # What it buys is M-011's signal-to-noise, and it buys it by removing the noise SOURCE rather than
    # by widening the tolerance the monitor fires on - which is the move that would have been cheaper
    # in lines and wrong.  It also carries the price: the book acts 15s later on a 3600s bar, and
    # DL-L4's rebalance window widens by the same 15s because it is derived from this.
    #
    # Twenty-fifth raise, 2026-09-15.  +40 beidou_live, no other package moves (`live_cmd.py` passes one
    # argument it already had in hand).  Two thirds of it is `beidou_live/verify.py`: the two digest
    # readers used to answer "what is the loop running?" by scanning `cycles.jsonl`, which is append-only
    # and carries NO process identity - so after a restart the newest row is the dead process's, and the
    # check reported a divergence the restart had just resolved.  Measured 2026-09-14 (loop up 19:10:55Z
    # holding the 17:50Z `policy.py`, the 19:11:19Z bar SKIPPED and recording no digest, the check
    # quoting 18:00:29Z's `75764f646ca6` for the rest of the hour); `com.beidou.check` fires at :10,
    # inside that window every time, and `state.restarts` is past 43.
    #
    # The lines are the scoping (`restarted_at` as the takeover boundary, which `startup` already
    # persists before its first heartbeat) plus the heartbeat as the reading of first resort, and they
    # buy back an instrument that was not only noisy but SILENT in the one direction that matters:
    # bc986ec3 put these digests on the restart heartbeat for exactly this window and changed no reader,
    # so DL-Q0's registry check had the same blind spot, still open, with a passing test that asserted
    # the write half by source inspection.  The remaining ~8 lines are R9's digest and a `dry_run` flag
    # on the two pre-cycle heartbeats, without which the governance half stays blind where the registry
    # half now sees.  Nothing here touches `construction_fingerprint`.
    #
    # Twenty-sixth raise, 2026-09-15, +9 beidou_live and nothing else - the same defect as the
    # twenty-fifth, found by running that fix in production twelve minutes after it shipped.  The
    # 08:49:16Z restart wrote a heartbeat that correctly said `1db80a06f281` / `d62ac59fa95c`; the
    # 09:00Z cycle then died on a proxy 503 (the fourth since 09-08, environmental, nothing to do with
    # the change) and the ERROR heartbeat replaced it with four keys, none of them a digest.  Both
    # instruments fell to "还没有任何周期记录过 digest" - honest under the new readers, where the old
    # ones would have quoted a dead process, and still blind in the one window DL-Q0 / R9 exist for.
    # Six of the nine lines are the paragraph saying that, because the cheap reading of this diff is
    # "three more fields on a heartbeat" and the expensive one is "an outage is a reason to want the
    # answer, not a reason to lose it".  Asserted through the engine, not by grepping the source.
    #
    # Twenty-fourth raise, 2026-09-15: +60 beidou_live, no other package moves.  The number is a collision
    # and is recorded rather than renumbered, on the thirteenth entry's own precedent.  A parallel session
    # declared 25-31 for its entries and left 20-24 to this branch, and all five of those were already
    # spent - twenty-fourth once, the other four twice or more - so this takes the least-spent one instead
    # of continuing at twenty-seventh into a range another session is writing into right now.
    #
    # DL-GB0, the giveback ruler.  `noise_scale`'s `peak_giveback_u` measures inside the UTC day and its
    # own docstring said so; the operator on 2026-09-15 was asking about a fall that started the previous
    # evening.  The report answered 145.2 U / 0.43 design daily sigma to a question whose answer was
    # 430.1 U / 1.28, and no instrument on the page could produce the second number.  The new keys read the
    # high-water mark the LOOP wrote - `throttle.equity_hwm`, the field D-015's throttle acts on - rather
    # than recomputing a high off the equity path, because the two can disagree and only one of them is
    # what the account is actually being throttled against.
    #
    # About two thirds of the 60 lines is docstring, and two paragraphs of it are the deliverable because
    # a schema cannot carry them.  First: the mark's age is when it became VISIBLE, not when it happened.
    # The 2026-09-14 high was set inside the 20:24Z restart gap, whose two cycles wrote heartbeat rows with
    # no equity field at all, so the earliest row carrying the mark is the 21:00Z bar and the true age can
    # be a bar older.  Second: the three drawdowns now printed together (3.86% equity against the loop's
    # high, -1.92% R8's attributed ladder, 0.43 sigma day-inside) point the same way with OPPOSITE signs,
    # and the ladder's is copied exactly as the loop writes it - flipping a sign to make a table tidy would
    # stop the field matching the record it came from.  `drawdown_vs_hwm_pct` is computed as giveback/HWM
    # and reproduces `throttle.drawdown` to the last digit, which is the check that the rulers share a book.
    #
    # One number in the delivery contract does not survive being computed: it asked for 1.89 sigma_11h, and
    # a single clock gives 10.0 hours and 1.98.  The 11 came from dating the mark by its bar (09-14T21:00Z)
    # and "now" by the row's WRITE time (09-15T08:00Z), which is D-025's two clocks one subtraction apart.
    # Both timestamps here are `as_of_ms`, `_day_of`'s own preference, so the hours are the data's.
    #
    # Deliberately NOT bought: the ROE line the analysis asks for stays out until the operator says which
    # of the venue's percentages they read (A-GB01), and nothing here touches `construction_fingerprint` -
    # this is a report-layer change inside the KILL-006 holdout, and the frozen-construction test agrees.
    #
    # Twenty-fourth raise, 2026-09-15, again: +45 beidou_live, no other package moves.  The number collides
    # with the entry directly above - written by this same branch one commit earlier - and is recorded
    # rather than renumbered, on the thirteenth entry's precedent: three sessions are writing into 20-31
    # concurrently, and a unique number bought by re-reading the whole file is worth less than a correct
    # +45.  Read the two twenty-fourths as one pair; this is the second half of DL-GB0.
    #
    # A-GB01, the question the entry above left open, is answered: the operator reads the venue's USDT
    # equity.  4,932.05 at 09-13T22:00Z to 5,311.89 at 09-14T19:00Z is +7.70% - the "涨了 8 个点" the same
    # window scored as +3.78% on equity (10,607.99 to 11,008.79).  The delivery contract dated those two
    # readings 23:00Z and 20:00Z; a single clock puts them an hour earlier, and 09-14T20:00Z has no cycle
    # at all - it is the restart gap the entry above dates the high from.  So the three new keys RESTATE
    # numbers the page already computes: `design_daily_sigma_in_usdt_pct` and
    # `giveback_since_hwm_in_usdt_pct` keep their numerators on total equity and only divide by
    # `collateral.usdt_equity`.  No new high-water mark is computed on the USDT series, deliberately: this
    # page's own defect is that it carries three drawdown rulers, and a fourth anchor would be that defect
    # again rather than a fix for it.
    #
    # Most of the 45 lines is the paragraph a schema cannot carry, and it says something counterintuitive
    # enough that leaving it out would invite a later reader to "correct" the operator: USDT equity is
    # CLOSER to the book's P&L than total equity is.  Trades settle in USDT; BTC collateral repricing moves
    # total equity and not USDT equity.  Measured on this event - low to peak +400.80 equity against
    # +379.84 USDT (20.96 of repricing), peak to now -207.35 against -187.41 (-19.94) - which is the same
    # quantity `## Collateral repricing (RISK-G11)` reports at 41% of an equity move.  The ruler is not
    # wrong; it has a smaller denominator (share 0.5256 collateral, so about 2.11x against equity) and it
    # excludes collateral noise.  It is still not the book - flows, commissions and funding move it directly
    # - and `risk_ladder.drawdown` stays the attributed ruler.  Four of the lines are a late correction
    # earned by actually rendering the page: `drawdown_vs_hwm_pct`, the percentage that now sits directly
    # above the new one, divides by the HIGH-WATER MARK and not by equity, so the two adjacent lines differ
    # by 2.18x and not by the collateral share's 2.11x.  Stating "about 2.1x" and leaving a reader to
    # divide 6.75 by 3.11 would have been an instrument that fails its own arithmetic check.
    #
    # Deliberately NOT bought, and now for a reason rather than for a missing answer: ROE.  The operator
    # named one of the venue's percentages, and printing the other two would put a fourth and fifth number
    # on a page whose whole problem was that a reader had to choose among the ones already there.
    #
    # MERGE RE-MEASURE, same day: the branch measured 9_083 against a tree without the health.py split,
    # and main had reached 9_393 through it.  Neither side's number describes the merged tree, so this is
    # the merged tree's own count - 9_438 - taken from the failing assertion rather than from 9_393 + 45,
    # which is the repository's rule for exactly this conflict and the third time today it has been hit.
    "beidou_live": 9_446,
    # +61 beidou_cli: `--embargo` as a knob of its own on `validate` and `book`, the fallback that keeps
    # it bit-identical while unset, and the report field - absence has to read as "embargo == purge",
    # which is a sentence a later reader needs and a schema cannot carry.
    # +44 more beidou_cli (5_829 -> 5_873), and the sentence the rule requires: three caveats that
    # already existed in the tree were being quoted without themselves.  The 2026-09-14 backtest-guard
    # audit found the registry note and the commit message for `tsmom-validation-20260913T182325Z`
    # citing CPCV's `fraction_negative`, `liquidation_touches: 0`, and a PBO move as evidence - while
    # the caveat for each one lived one file over, in `cpcv_splits`' docstring, in `backtest.py`'s
    # margin-buffer paragraph, and in `verdict.decide`'s own "not enforced below four" branch.  The
    # lines buy `_MARGIN_BUFFER_NOTE`, `_embargo_note` and `_pbo_note`, which print beside the numbers
    # they qualify, so the next reader of an artefact does not have to already know.
    #
    # Why lines rather than a doc: a document describing the caveat is the thing that was already there
    # and did not travel.  D-038's shape - "a correct fact no instrument states is indistinguishable
    # from an unproven one" - applied to a caveat instead of a fact.
    #
    # What was deliberately NOT bought: the JSON payload is untouched.  Putting the notes in `report`
    # would move every future `report_digest` and make the archived sha256 the registry cites
    # incomparable with a re-run, which is a real cost for a string no rule reads.
    # +55 more beidou_cli (5_873 -> 5_928), and the sentence the rule requires: `_stressed_oos_gate`,
    # which re-asks D-028's gate on the stressed cost series instead of leaving a reader to subtract a
    # full-sample Sharpe from an out-of-sample threshold.  The audit caught that subtraction being made
    # with the evidence's own numbers - a +0.0426 gate margin weighed against a -0.1175 cost-doubling
    # effect measured on 49,240 bars rather than the gate's 45,240 - and the report had no way to answer
    # "does it still clear the gate if costs double", which is the question an operator asks before real
    # capital.  Same folds, same N, stressed series; x1 reproduces `best_key_oos_sharpe` exactly (NOT the
    # fold-selected mixture the headline gate reads - the docstring names the difference) and a test
    # asserts that, so the new number cannot drift away from the one it is supposed to extend.
    #
    # Unlike the caveats above this one DOES go into the JSON.  The rule those followed was "markdown
    # only, because no rule reads a string"; this is a number a rule could read, and a governance number
    # that lives only in prose is the D-038 failure the caveats commit cites.  Future reports carry the
    # key, archived ones do not, and `verdict.decide` already handles reports written before a field
    # existed - that is the established shape here, not a new risk.
    #
    # Part of the same +55: most of the last lines record why the stressed series is
    # reindexed to `common_index` before the folds cut it.  `fold_list` is sized against that index and a
    # fresh `run_backtest` returns a longer one, so slicing the raw series reads different bars - it
    # surfaced as an x1 cell of 6.90 against `best_key_oos_sharpe`'s 6.02 on the fixture.  Silent
    # misalignment between two indices that both look right is the failure this file keeps paying to
    # document rather than rediscover.
    # +17 beidou_cli (5_928 -> 5_945), 2026-09-13: `live kill-switch` treated "this profile has no
    # account to scope to" and "this operator's shell lacks the export" as the same fact.  Measured on
    # the running demo loop minutes after a flatten had engaged both paths: `--release` without the
    # credential printed one success line, exited 0, and left the account-scoped switch in place - the
    # book stayed stopped while the operator had been told it was released.  Seven of the seventeen are
    # the comment saying why the two directions are deliberately not symmetric: release refuses before
    # touching anything, because it must not report a success it did not achieve; engage still writes
    # what it can and only then exits non-zero, because a kill switch fails toward stopping.
    "beidou_cli": 6_178,
    # +67 beidou_data: `write_parquet_atomically` for the three stores (the same fsync the live state
    # file was missing, applied to 4.1 GB of archive), and `membership_summary`'s optional dead-slot
    # count.  The measurement it exists for: 109 of 35,899 member-slots (0.30%) had no bar behind them,
    # and 81 of those 109 are six symbols whose 1h archive was never backfilled - a sync gap wearing a
    # universe defect's clothes.  Only LUNAUSDT's 28 refreshes are the phenomenon, and a single merged
    # share hides that, so the reading is per symbol.
    # +7 more beidou_data (5_432 -> 5_439): the `backoff` seam `MetricsArchiveClient` was missing while
    # `onchain.CommunityClient` had carried it all along, with the comment saying why.  Its two 5xx
    # tests each really slept 1+2+4 seconds, which is 14s on the laptop and ~42s on CI - and `Types`
    # had been red for 23 pushes, so the test step never ran and `suite_duration.py` never saw them.
    "beidou_data": 5_467,
    # +103 beidou_exchange, on a 611-line package: `_paged` stepped to `last + 1` after a full page, so
    # rows sharing that page's final millisecond were dropped - and one funding settlement writes one row
    # per held symbol on an identical `fundingTime`, so the rows most likely to share a millisecond are
    # the ones the attribution ledger is made of.  The walk now overlaps and dedupes by id, and says out
    # loud when it stopped asking rather than when the window ended.  The rest is the two write-only
    # counters finally being read (a warning, never a sleep - the loop is holding positions) and the
    # correction to `ARCHITECTURE.md`, which had claimed 限频 this package does not do.
    "beidou_exchange": 714,
    "beidou_shared": 289,
    # +14 beidou_governance: `read_gate`'s four numeric fields narrowed one at a time instead of through
    # an `all(isinstance(...))` generator that mypy 2.x stopped reading - part of the 30 type errors that
    # had kept CI red for 23 consecutive pushes over four days, with the test step never running once.
    # +31 more beidou_governance (3_657 -> 3_688), 2026-09-14: R8's rungs re-derived for the -70%
    # budget the operator declared when `vol_target` went to 0.60, plus POLICY_VERSION 0.3.3 recording
    # why.  Two numbers changed; the rest is the paragraph saying that the ruler fix and the rescale
    # are one decision - fixing the ruler alone would have made the OLD rungs bite for the first time,
    # silently buying a brake measured at 20.5pp of CAGR that nobody chose, and rescaling alone would
    # have re-tuned something that never fires.  The options were priced before the operator picked.
    "beidou_governance": 3_940,
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
