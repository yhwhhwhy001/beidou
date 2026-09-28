# Validation: lsr_timing — FAIL

## Range

| key | value |
| --- | --- |
| start | 2021-01-31 01:00:00+00:00 |
| end | 2026-09-27 16:00:00+00:00 |
| bars | 49576 |

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
| window | 168 |
| scale | 0.5000 |
| entry_threshold | 0.2000 |

## Full sample

| key | value |
| --- | --- |
| bars | 49576 |
| gross_return | 2.1144 |
| net_return | 1.8151 |
| annualized_sharpe | 1.4100 |
| annualized_sharpe_gross | 1.5408 |
| max_drawdown | -0.2017 |
| average_absolute_exposure | 0.2169 |
| turnover_units | 183.2 |
| nonzero_target_bars | 48918 |
| hit_rate | 0.4986 |
| cost_share_of_gross | 0.0850 |
| skew | 0.8806 |
| kurtosis | 23.1129 |
| sortino | 2.1084 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=0; daily_loss_pause_bars=4; bars=49576; min_margin_buffer=276.3; liquidation_touches=0 |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 1.2774 |
| oos_windows | 63 |
| oos_window_sharpe_q10 | -2.9863 |
| oos_return | 1.4056 |
| oos_max_drawdown | -0.2017 |
| oos_bars | 45576 |
| oos_t_stat | 2.8172 |
| oos_t_lags | 15 |
| fold_sharpes | 1.8091, 1.3897, 2.6160, 1.3338, -0.3879 |
| fold_consistency | 0.8000 |
| selection_consistent | no |
| oos_is_full_sample_tail | no |
| best_key_oos_sharpe | 1.4206 |
| selection_exercised | folds exercised a choice: this OOS is a selection procedure's out-of-sample record |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 1.1230 |
| oos_sharpe_q05 | 0.1682 |
| oos_sharpe_min | -0.0542 |
| fraction_negative | 0.0667 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 662 |
| grid_trials | 4 |
| grid_effective_trials | 3.0000 |
| prior_trials | 658 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 1.4100 |
| expected_max_sharpe_annual | 1.1448 |
| dsr | 0.7371 |
| dsr_p_value | 0.2629 |
| pbo | 0.2958 |
| pbo_combinations | 5000 |
| degradation_slope | -0.8405 |
| prob_oos_loss | 0.0542 |
| noise_null | sharpe_variance_period=0.0000; expected_max_sharpe_annual=1.3103; dsr_p_value=0.4057 |
| ledger_trials | 0 |
| pbo_is_informative | enforced: grid_trials=4 >= 4 |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 45576 |
| n_trials | 662 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.6533 |
| expected_max_annual | 1.3705 |
| p_family | 0.6830 |
| oos_sharpe_annual | 1.2774 |
| caliber | ENFORCED at N=662 (the per-strategy bucket): threshold 1.65, margin -0.38.  REPORTED and never enforced at N=21644 (the whole library): threshold 2.00, margin -0.72, p_family 1.0000.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4370 |
| gate (max of the two halves) | 1.6533  [selection binds] |
|   D-028 selection threshold | 1.6533 at N=662, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 6.7% |
| P(clear | true annual Sharpe = 1.2) | 15.0% |
| P(clear | true annual Sharpe = 1.5) | 36.3% |
| P(clear | true annual Sharpe = 2.0) | 78.6% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 45576 |
| n_trials | 21644 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.9998 |
| expected_max_annual | 1.7680 |
| p_family | 1.0000 |
| oos_sharpe_annual | 1.2774 |
| power | n_trials=21644; alpha=0.0500; se_annual=0.4370; pass_line_annual=1.0000; selection_threshold_annual=1.9998; gate_annual=1.9998; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0111, true_sharpe_annual=1.2000; power=0.0336, true_sharpe_annual=1.5000; power=0.1264, true_sharpe_annual=2.0000; power=0.5001; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=1.42 threshold=1.65 margin=-0.23 clears=0.00 |
| x1.5 | oos=1.34 threshold=1.65 margin=-0.31 clears=0.00 |
| x2 | oos=1.26 threshold=1.65 margin=-0.39 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip2 | oos=1.42 threshold=1.65 margin=-0.23 clears=0.00 |
| slip4.43 | oos=1.36 threshold=1.65 margin=-0.29 clears=0.00 |
| slip5.5 | oos=1.34 threshold=1.65 margin=-0.31 clears=0.00 |
| slip9.2 | oos=1.25 threshold=1.65 margin=-0.40 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | 1.6963, 1.5242, 2.3150, -0.0646 |
| worst_neighbour_degradation | 0.1312 |
| parameter_neighbourhood | scale=down=1.42 base=1.41 up=1.22; window=down=1.51 base=1.41 up=1.60 |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.71) | sharpe=0.95  bars=15192 |
| mid (annualised vol 0.71-0.88) | sharpe=1.04  bars=15192 |
| high (annualised vol 0.88-2.23) | sharpe=2.27  bars=15192 |
| basis: series | walk_forward oos_returns, the series time_split_sharpes splits |
| basis: state | annualised std, over vol_window_days, of benchmark_returns: equal-weight, zero cost, over the symbols the book could hold at each bar (pit members; every panel symbol if static) |
| basis: vol_window_days | 30 |
| basis: label_on_bar_t_reads | benchmark bars through t-1: what was known when bar t's position was decided |
| basis: cut_points | this sample's own terciles: the state is ex-ante, the cut points are not.  Volatility trends over years, so a tercile is partly a calendar period: read it against time_split_sharpes |

## Grid (full-sample Sharpe per configuration)

- scale=0.5, window=168: sharpe=1.41
- scale=0.5, window=72: sharpe=1.10
- scale=1.0, window=168: sharpe=0.98
- scale=1.0, window=72: sharpe=0.53

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 1.4100 |
| x1.5 | 1.3267 |
| x2 | 1.2433 |
| break-even multiple m* (never enforced) | full_sample=9.48  oos=9.68  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip2 | 1.4100 |
| slip4.43 | 1.3521 |
| slip5.5 | 1.3267 |
| slip9.2 | 1.2385 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 1.3915 |

## Verdict

| key | value |
| --- | --- |
| verdict | FAIL |
| reasons | oos_sharpe 1.28 < the deflated threshold 1.65 at 662 trials, p_family=0.6830 |
