# Validation: tsmom — WEAK_PASS

## Range

| key | value |
| --- | --- |
| start | 2021-01-31 01:00:00+00:00 |
| end | 2026-10-19 00:00:00+00:00 |
| bars | 50088 |

## Holdout (KILL-006)

| key | value |
| --- | --- |
| months | 0 |
| note | no tail reserved: every bar was available to this run |

## Book guards / exits (the layers the loop applies)

| key | value |
| --- | --- |
| guards | max_weight=0.1500; max_gross=2.0000; daily_loss_pause=-0.0500 |
| exits | stop_loss=6.0000; stop_loss_price_cap=0.0000; trailing_stop=0.0000; trailing_activate=0.0000; take_profit=6.0000; cooldown_bars=24; vol_halflife=48; bars_per_day=24; min_unit=0.0050; unit_mode=entry; regime_window=0; regime_er_cut=0.0500; regime_tp_scale=0.5000; regime_side=low; stale_carry_bars=2 |
| margin_buffer | structural bound, not a measurement: buffer = (1 + r - c) / (gross * maintenance_margin_rate), and gross <= max_gross, so at mmr 0.005 and max_gross 2.0 it cannot fall below about 100.  Reaching the liquidation line at 1.0 would take one bar losing ~99%, so `liquidation_touches: 0` is arithmetic rather than evidence.  The channel that can actually liquidate this account is collateral repricing (52% non-USDT, KILL-AR-05) and this replay models zero collateral. |

## Best params (full sample)

| key | value |
| --- | --- |
| horizons | 168, 336, 720 |
| horizon_weights | 0.2000, 0.3000, 0.5000 |
| return_weight | 0.4000 |
| slope_weight | 0.2500 |
| persistence_weight | 0.1000 |
| return_scale | 0.2000 |
| slope_scale | 0.0100 |
| entry_threshold | 0.2000 |
| vol_window | 400 |
| crowding_window | 72 |
| crowding_cut | 0.7000 |
| crowding_penalty | 0.5000 |
| momentum_mode | fixed |
| conviction_mode | sign |

## Full sample

| key | value |
| --- | --- |
| bars | 50088 |
| gross_return | 5.7221 |
| net_return | 4.3832 |
| annualized_sharpe | 1.8523 |
| annualized_sharpe_gross | 2.0857 |
| max_drawdown | -0.1039 |
| average_absolute_exposure | 0.5075 |
| turnover_units | 298.2 |
| nonzero_target_bars | 49897 |
| hit_rate | 0.5102 |
| cost_share_of_gross | 0.1119 |
| skew | 0.9826 |
| kurtosis | 28.3420 |
| sortino | 2.7323 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=0; daily_loss_pause_bars=0; bars=50088; min_margin_buffer=107.3; liquidation_touches=0 |
| cagr_full_sample | 0.3423 |
| calmar_full_sample | 3.2959 |
| payoff_ratio_bar | 1.0190 |
| cagr_caliber | full-sample drift of the selected book, not the honest-drift bootstrap CAGR median the k decision reads beside vol_target in config/live.demo.yaml |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 1.7591 |
| oos_windows | 64 |
| oos_window_sharpe_q10 | -1.8167 |
| oos_return | 3.4268 |
| oos_max_drawdown | -0.1039 |
| oos_bars | 46088 |
| oos_t_stat | 3.9761 |
| oos_t_lags | 15 |
| fold_sharpes | 2.1653, 1.0270, 2.4089, 1.1620, 2.0968 |
| fold_consistency | 1.0000 |
| selection_consistent | yes |
| oos_is_full_sample_tail | yes |
| oos_cagr | 0.3268 |
| oos_calmar | 3.1465 |
| best_key_oos_sharpe | 1.7591 |
| selection_exercised | NO fold had a choice to make (single configuration, or every fold picked the same one), so the OOS Sharpe above is the TAIL OF ONE FULL-SAMPLE SERIES, not a selection's out-of-sample record.  D-043 caps such a report at WEAK_PASS: the number stands, the claim that a selection survived out of sample does not.  `registry.py` still admits WEAK_PASS to live use. |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 1.8536 |
| oos_sharpe_q05 | 1.5494 |
| oos_sharpe_min | 1.4375 |
| fraction_negative | 0.0000 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 359 |
| grid_trials | 2 |
| grid_effective_trials | 2.0000 |
| prior_trials | 172 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 1.8523 |
| expected_max_sharpe_annual | 1.6342 |
| dsr | 0.7005 |
| dsr_p_value | 0.2995 |
| pbo | 0.0274 |
| pbo_combinations | 5000 |
| degradation_slope | -0.9994 |
| prob_oos_loss | 0.0000 |
| noise_null | sharpe_variance_period=0.0000; expected_max_sharpe_annual=1.2239; dsr_p_value=0.0649 |
| ledger_trials | 185 |
| pbo_is_informative | NOT informative and NOT enforced at grid_trials=2: CSCV ranks configurations against each other, and with fewer than four it only says that two curves traded places across sub-periods.  `verdict.decide` skips the gate below four, so a move in this number is not a cost or a gain either. |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 46088 |
| n_trials | 359 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.5701 |
| expected_max_annual | 1.2774 |
| p_family | 0.0086 |
| oos_sharpe_annual | 1.7591 |
| caliber | ENFORCED at N=359 (the per-strategy bucket): threshold 1.57, margin +0.19.  REPORTED and never enforced at N=21668 (the whole library): threshold 1.98, margin -0.22, p_family 0.4063.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4328 |
| gate (max of the two halves) | 1.5701  [selection binds] |
|   D-028 selection threshold | 1.5701 at N=359, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 9.4% |
| P(clear | true annual Sharpe = 1.2) | 19.6% |
| P(clear | true annual Sharpe = 1.5) | 43.6% |
| P(clear | true annual Sharpe = 2.0) | 84.0% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 46088 |
| n_trials | 21668 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.9805 |
| expected_max_annual | 1.7509 |
| p_family | 0.4063 |
| oos_sharpe_annual | 1.7591 |
| power | n_trials=21668; alpha=0.0500; se_annual=0.4328; pass_line_annual=1.0000; selection_threshold_annual=1.9805; gate_annual=1.9805; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0117, true_sharpe_annual=1.2000; power=0.0357, true_sharpe_annual=1.5000; power=0.1334, true_sharpe_annual=2.0000; power=0.5180; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=1.76 threshold=1.57 margin=0.19 clears=1.00 |
| x1.5 | oos=1.65 threshold=1.57 margin=0.08 clears=1.00 |
| x2 | oos=1.54 threshold=1.57 margin=-0.03 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip2 | oos=1.76 threshold=1.57 margin=0.19 clears=1.00 |
| slip4.43 | oos=1.68 threshold=1.57 margin=0.11 clears=1.00 |
| slip5.5 | oos=1.65 threshold=1.57 margin=0.08 clears=1.00 |
| slip9.2 | oos=1.53 threshold=1.57 margin=-0.04 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | 1.5807, 2.0208, 1.5363, 1.8840 |
| worst_neighbour_degradation | 0.1226 |
| parameter_neighbourhood | crowding_window=down=1.76 base=1.85 up=1.83; entry_threshold=down=1.63 base=1.85 up=1.63; return_scale=down=1.74 base=1.85 up=1.77; vol_window=down=1.85 base=1.85 up=1.85 |
| parameter_neighbourhood_not_perturbed | horizons |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.70) | sharpe=2.74  bars=15363  window_q10=-0.67 (24 windows) |
| mid (annualised vol 0.70-0.88) | sharpe=1.58  bars=15362  window_q10=-2.67 (16 windows) |
| high (annualised vol 0.88-2.23) | sharpe=0.80  bars=15363  window_q10=-1.59 (24 windows) |
| basis: series | walk_forward oos_returns, the series time_split_sharpes splits |
| basis: state | annualised std, over vol_window_days, of benchmark_returns: equal-weight, zero cost, over the symbols the book could hold at each bar (pit members; every panel symbol if static) |
| basis: vol_window_days | 30 |
| basis: label_on_bar_t_reads | benchmark bars through t-1: what was known when bar t's position was decided |
| basis: cut_points | this sample's own terciles: the state is ex-ante, the cut points are not.  Volatility trends over years, so a tercile is partly a calendar period: read it against time_split_sharpes |
| basis: mean_annual, vol_annual | the tercile's bars' mean x bars_per_year and std (ddof 1) x sqrt(bars_per_year): a mix of terciles is priced from these, since Sharpe ratios do not mix (D-049) |
| basis: window_sharpe_q10 | q10 of the whole non-overlapping 30-day OOS windows that START in the tercile (walk_forward's oos_windows, each filed by its first bar's label); null below 10 windows |

## Against its own basket, out of sample (N2; reported, never enforced)

| key | value |
| --- | --- |
| bars | 46088 |
| book (fold-selected, out of sample) | return=+342.681%  cagr=+32.679%  sharpe=1.76  max_drawdown=-10.386% |
| basket (pit members at each bar, equal weight, zero cost, rebalanced every bar) | return=-91.297%  cagr=-37.128%  sharpe=-0.11  max_drawdown=-97.357% |
| BTCUSDT buy and hold | return=+156.460%  cagr=+19.603%  sharpe=0.61  max_drawdown=-77.271% |
| constant: book ~ basket (passive market exposure) | beta=-0.02 (t -3.47)  alpha=0.337 bps/bar (t 3.92)  NW lags 48 |
| conditional: book ~ exposure x basket (timing counts as beta here) | beta=0.73 (t 56.40)  alpha=0.237 bps/bar (t 4.36)  NW lags 48 |
| signal state | all long 12.5%  all short 8.8%  two-sided 78.6%  flat 0.1%  net -0.01  gross 0.53 |
| basis: returns | simple per-bar returns; the live D-045 page regresses log returns |
| basis: basket | equal weight, zero cost, rebalanced every bar: a volatile basket's compounded return is mostly drag, so compare Sharpe, not return |
| basis: conditional | exposure is the book's own net exposure, so this fit counts the signal's timing as beta; whether the book is passive market exposure is the constant fit |
| basis: signal_state | signs of the executed positions, flat bars included; the live page counts the signal's +/-1 |

## Concentration by symbol (N3; reported, never enforced)

| key | value |
| --- | --- |
| full sample | top1 +10.175%  top3 +24.126%  of summed net returns +1.7624  (largest: BTCUSDT +0.1793, BNBUSDT +0.1317, SOLUSDT +0.1142) |
| out of sample | top1 +10.747%  top3 +24.066%  of summed net returns +1.5626  (largest: BTCUSDT +0.1679, SOLUSDT +0.1122, BNBUSDT +0.0959) |
| OOS Sharpe without its most important name | 1.68 without APEUSDT (book 1.76) |
| basis: units | sums of per-bar simple net returns, not compounded; a share exceeds 100% when other names lost |
| basis: leave_one_out | one symbol's net column dropped; nothing re-priced, weights not renormalised, guards and exits not replayed, so the error has no known sign |

## Grid (full-sample Sharpe per configuration)

- crowding_window=72: sharpe=1.85
- crowding_window=0: sharpe=1.68

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 1.8523 |
| x1.5 | 1.7424 |
| x2 | 1.6325 |
| break-even multiple m* (never enforced) | full_sample=9.44  oos=8.93  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip2 | 1.8523 |
| slip4.43 | 1.7760 |
| slip5.5 | 1.7424 |
| slip9.2 | 1.6263 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 1.8236 |

## Verdict

| key | value |
| --- | --- |
| verdict | WEAK_PASS |
| reasons | oos_is_full_sample_tail: no fold had a choice to make (single configuration, or every fold picked the same one), so this OOS is the tail of one full-sample series |
