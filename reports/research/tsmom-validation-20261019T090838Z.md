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
| gross_return | 88.5470 |
| net_return | 59.6145 |
| annualized_sharpe | 1.7126 |
| annualized_sharpe_gross | 1.8520 |
| max_drawdown | -0.3357 |
| average_absolute_exposure | 1.2081 |
| turnover_units | 520.4 |
| nonzero_target_bars | 50088 |
| hit_rate | 0.5107 |
| cost_share_of_gross | 0.0754 |
| skew | 0.3297 |
| kurtosis | 23.1397 |
| sortino | 2.4707 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=2252; daily_loss_pause_bars=265; bars=50088; min_margin_buffer=97.8945; liquidation_touches=0 |
| cagr_full_sample | 1.0500 |
| calmar_full_sample | 3.1282 |
| payoff_ratio_bar | 1.0115 |
| cagr_caliber | full-sample drift of the selected book, not the honest-drift bootstrap CAGR median the k decision reads beside vol_target in config/live.demo.yaml |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 1.6465 |
| oos_windows | 64 |
| oos_window_sharpe_q10 | -1.7974 |
| oos_return | 35.0847 |
| oos_max_drawdown | -0.3357 |
| oos_bars | 46088 |
| oos_t_stat | 3.7832 |
| oos_t_lags | 15 |
| fold_sharpes | 1.9515, 0.5787, 2.2692, 1.3317, 2.0380 |
| fold_consistency | 1.0000 |
| selection_consistent | yes |
| oos_is_full_sample_tail | yes |
| oos_cagr | 0.9770 |
| oos_calmar | 2.9106 |
| best_key_oos_sharpe | 1.6465 |
| selection_exercised | NO fold had a choice to make (single configuration, or every fold picked the same one), so the OOS Sharpe above is the TAIL OF ONE FULL-SAMPLE SERIES, not a selection's out-of-sample record.  D-043 caps such a report at WEAK_PASS: the number stands, the claim that a selection survived out of sample does not.  `registry.py` still admits WEAK_PASS to live use. |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 1.6708 |
| oos_sharpe_q05 | 1.4162 |
| oos_sharpe_min | 1.3870 |
| fraction_negative | 0.0000 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 361 |
| grid_trials | 2 |
| grid_effective_trials | 2.0000 |
| prior_trials | 172 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 1.7126 |
| expected_max_sharpe_annual | 1.6360 |
| dsr | 0.5728 |
| dsr_p_value | 0.4272 |
| pbo | 0.2080 |
| pbo_combinations | 5000 |
| degradation_slope | -1.0053 |
| prob_oos_loss | 0.0000 |
| noise_null | sharpe_variance_period=0.0000; expected_max_sharpe_annual=1.2325; dsr_p_value=0.1250 |
| ledger_trials | 187 |
| pbo_is_informative | NOT informative and NOT enforced at grid_trials=2: CSCV ranks configurations against each other, and with fewer than four it only says that two curves traded places across sub-periods.  `verdict.decide` skips the gate below four, so a move in this number is not a cost or a gain either. |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 46088 |
| n_trials | 361 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.5788 |
| expected_max_annual | 1.2847 |
| p_family | 0.0274 |
| oos_sharpe_annual | 1.6465 |
| caliber | ENFORCED at N=361 (the per-strategy bucket): threshold 1.58, margin +0.07.  REPORTED and never enforced at N=21670 (the whole library): threshold 1.99, margin -0.34, p_family 0.8109.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4350 |
| gate (max of the two halves) | 1.5788  [selection binds] |
|   D-028 selection threshold | 1.5788 at N=361, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 9.2% |
| P(clear | true annual Sharpe = 1.2) | 19.2% |
| P(clear | true annual Sharpe = 1.5) | 42.8% |
| P(clear | true annual Sharpe = 2.0) | 83.4% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 46088 |
| n_trials | 21670 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.9907 |
| expected_max_annual | 1.7600 |
| p_family | 0.8109 |
| oos_sharpe_annual | 1.6465 |
| power | n_trials=21670; alpha=0.0500; se_annual=0.4350; pass_line_annual=1.0000; selection_threshold_annual=1.9907; gate_annual=1.9907; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0114, true_sharpe_annual=1.2000; power=0.0346, true_sharpe_annual=1.5000; power=0.1296, true_sharpe_annual=2.0000; power=0.5085; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=1.65 threshold=1.58 margin=0.07 clears=1.00 |
| x1.5 | oos=1.58 threshold=1.58 margin=0.00 clears=1.00 |
| x2 | oos=1.51 threshold=1.58 margin=-0.07 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip2 | oos=1.65 threshold=1.58 margin=0.07 clears=1.00 |
| slip4.43 | oos=1.60 threshold=1.58 margin=0.02 clears=1.00 |
| slip5.5 | oos=1.58 threshold=1.58 margin=0.00 clears=1.00 |
| slip9.2 | oos=1.51 threshold=1.58 margin=-0.07 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | 1.4578, 1.8230, 1.5992, 1.7333 |
| worst_neighbour_degradation | 0.0774 |
| parameter_neighbourhood | crowding_window=down=1.64 base=1.71 up=1.66; entry_threshold=down=1.58 base=1.71 up=1.58; return_scale=down=1.64 base=1.71 up=1.69; vol_window=down=1.71 base=1.71 up=1.71 |
| parameter_neighbourhood_not_perturbed | horizons |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.70) | sharpe=2.88  bars=15363  window_q10=-0.45 (24 windows) |
| mid (annualised vol 0.70-0.88) | sharpe=1.55  bars=15362  window_q10=-2.42 (16 windows) |
| high (annualised vol 0.88-2.23) | sharpe=0.52  bars=15363  window_q10=-1.76 (24 windows) |
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
| book (fold-selected, out of sample) | return=+3508.473%  cagr=+97.698%  sharpe=1.65  max_drawdown=-33.566% |
| basket (pit members at each bar, equal weight, zero cost, rebalanced every bar) | return=-91.297%  cagr=-37.128%  sharpe=-0.11  max_drawdown=-97.357% |
| BTCUSDT buy and hold | return=+156.460%  cagr=+19.603%  sharpe=0.61  max_drawdown=-77.271% |
| constant: book ~ basket (passive market exposure) | beta=-0.06 (t -3.03)  alpha=0.906 bps/bar (t 3.80)  NW lags 48 |
| conditional: book ~ exposure x basket (timing counts as beta here) | beta=0.81 (t 76.24)  alpha=0.539 bps/bar (t 4.10)  NW lags 48 |
| signal state | all long 10.7%  all short 2.5%  two-sided 86.9%  flat 0.0%  net -0.02  gross 1.25 |
| basis: returns | simple per-bar returns; the live D-045 page regresses log returns |
| basis: basket | equal weight, zero cost, rebalanced every bar: a volatile basket's compounded return is mostly drag, so compare Sharpe, not return |
| basis: conditional | exposure is the book's own net exposure, so this fit counts the signal's timing as beta; whether the book is passive market exposure is the constant fit |
| basis: signal_state | signs of the executed positions, flat bars included; the live page counts the signal's +/-1 |

## Concentration by symbol (N3; reported, never enforced)

| key | value |
| --- | --- |
| full sample | top1 +8.125%  top3 +24.166%  of summed net returns +4.7873  (largest: DOGEUSDT +0.3889, SOLUSDT +0.3878, BNBUSDT +0.3802) |
| out of sample | top1 +9.029%  top3 +25.370%  of summed net returns +4.2053  (largest: SOLUSDT +0.3797, DOGEUSDT +0.3493, BTCUSDT +0.3379) |
| OOS Sharpe without its most important name | 1.58 without APEUSDT (book 1.65) |
| basis: units | sums of per-bar simple net returns, not compounded; a share exceeds 100% when other names lost |
| basis: leave_one_out | one symbol's net column dropped; nothing re-priced, weights not renormalised, guards and exits not replayed, so the error has no known sign |

## Grid (full-sample Sharpe per configuration)

- crowding_window=72: sharpe=1.71
- crowding_window=0: sharpe=1.60

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 1.7126 |
| x1.5 | 1.6477 |
| x2 | 1.5825 |
| break-even multiple m* (never enforced) | full_sample=14.15  oos=13.37  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip2 | 1.7126 |
| slip4.43 | 1.6676 |
| slip5.5 | 1.6477 |
| slip9.2 | 1.5788 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 1.6941 |

## Verdict

| key | value |
| --- | --- |
| verdict | WEAK_PASS |
| reasons | oos_is_full_sample_tail: no fold had a choice to make (single configuration, or every fold picked the same one), so this OOS is the tail of one full-sample series |
