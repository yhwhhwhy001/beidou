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
CEILING = {
    "beidou_alpha": 5_838,
    "beidou_live": 5_735,
    "beidou_cli": 3_447,
    "beidou_data": 1_805,
    "beidou_exchange": 611,
    "beidou_shared": 289,
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
