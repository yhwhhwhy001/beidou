# Validation: carry_hedged — FAIL

## Range

| key | value |
| --- | --- |
| start | 2021-01-31 01:00:00+00:00 |
| end | 2026-09-26 16:00:00+00:00 |
| bars | 49552 |

## Holdout (KILL-006)

| key | value |
| --- | --- |
| months | 0 |
| note | no tail reserved: every bar was available to this run |

## Book guards / exits (the layers the loop applies)

| key | value |
| --- | --- |
| guards | max_weight=0.1500; max_gross=2.0000; daily_loss_pause=-0.0500 |
| exits | n/a |
| margin_buffer | structural bound, not a measurement: buffer = (1 + r - c) / (gross * maintenance_margin_rate), and gross <= max_gross, so at mmr 0.005 and max_gross 2.0 it cannot fall below about 100.  Reaching the liquidation line at 1.0 would take one bar losing ~99%, so `liquidation_touches: 0` is arithmetic rather than evidence.  The channel that can actually liquidate this account is collateral repricing (52% non-USDT, KILL-AR-05) and this replay models zero collateral. |

## Best params (full sample)

| key | value |
| --- | --- |
| lookback_days | 30 |
| threshold_per_day | 0.0003 |
| decision_hour_utc | 0 |

## Full sample

| key | value |
| --- | --- |
| bars | 49552 |
| gross_return | -0.0319 |
| net_return | 0.2635 |
| annualized_sharpe | 1.4213 |
| annualized_sharpe_gross | -0.1815 |
| max_drawdown | -0.0576 |
| average_absolute_exposure | 0.2045 |
| turnover_units | 45.4237 |
| nonzero_target_bars | 18648 |
| hit_rate | 0.5355 |
| cost_share_of_gross | n/a |
| skew | -61.3447 |
| kurtosis | 10227.2 |
| sortino | 1.7259 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=0; daily_loss_pause_bars=0; bars=49552; min_margin_buffer=286.2; liquidation_touches=0 |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 0.6826 |
| oos_windows | 49 |
| oos_window_sharpe_q10 | -3.9774 |
| oos_return | 0.0771 |
| oos_max_drawdown | -0.0578 |
| oos_bars | 45552 |
| oos_t_stat | 2.5566 |
| oos_t_lags | 15 |
| fold_sharpes | 1.7491, -1.0522, 2.7040, 1.1593, -0.3217 |
| fold_consistency | 0.6000 |
| selection_consistent | no |
| oos_is_full_sample_tail | no |
| best_key_oos_sharpe | 0.9213 |
| selection_exercised | folds exercised a choice: this OOS is a selection procedure's out-of-sample record |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 1.4231 |
| oos_sharpe_q05 | -0.8514 |
| oos_sharpe_min | -0.9914 |
| fraction_negative | 0.3333 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 4 |
| grid_trials | 4 |
| grid_effective_trials | 2.0000 |
| prior_trials | 0 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 1.4213 |
| expected_max_sharpe_annual | 0.6188 |
| dsr | 0.8853 |
| dsr_p_value | 0.1147 |
| pbo | 0.0468 |
| pbo_combinations | 5000 |
| degradation_slope | -0.6974 |
| prob_oos_loss | 0.0658 |
| noise_null | sharpe_variance_period=0.0001; expected_max_sharpe_annual=0.7024; dsr_p_value=0.1408 |
| ledger_trials | 0 |
| pbo_is_informative | enforced: grid_trials=4 >= 4 |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 45552 |
| n_trials | 4 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.0319 |
| expected_max_annual | 0.4860 |
| p_family | 0.2511 |
| oos_sharpe_annual | 0.6826 |
| caliber | ENFORCED at N=4 (the per-strategy bucket): threshold 1.03, margin -0.35.  REPORTED and never enforced at N=21640 (the whole library): threshold 2.11, margin -1.43, p_family 1.0000.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4619 |
| gate (max of the two halves) | 1.0319  [selection binds] |
|   D-028 selection threshold | 1.0319 at N=4, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 47.2% |
| P(clear | true annual Sharpe = 1.2) | 64.2% |
| P(clear | true annual Sharpe = 1.5) | 84.5% |
| P(clear | true annual Sharpe = 2.0) | 98.2% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 45552 |
| n_trials | 21640 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 2.1137 |
| expected_max_annual | 1.8687 |
| p_family | 1.0000 |
| oos_sharpe_annual | 0.6826 |
| power | n_trials=21640; alpha=0.0500; se_annual=0.4619; pass_line_annual=1.0000; selection_threshold_annual=2.1137; gate_annual=2.1137; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0080, true_sharpe_annual=1.2000; power=0.0240, true_sharpe_annual=1.5000; power=0.0920, true_sharpe_annual=2.0000; power=0.4028; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=0.92 threshold=1.13 margin=-0.20 clears=0.00 |
| x1.5 | oos=0.50 threshold=1.05 margin=-0.55 clears=0.00 |
| x2 | oos=0.09 threshold=0.99 margin=-0.90 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip4 | oos=0.92 threshold=1.13 margin=-0.20 clears=0.00 |
| slip8.86 | oos=0.70 threshold=1.08 margin=-0.38 clears=0.00 |
| slip11 | oos=0.61 threshold=1.07 margin=-0.45 clears=0.00 |
| slip18.4 | oos=0.29 threshold=1.01 margin=-0.73 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | 0.9052, -0.0319, 5.1944, -0.2878 |
| worst_neighbour_degradation | 0.0005 |
| parameter_neighbourhood | lookback_days=down=1.42 base=1.42 up=1.48; threshold_per_day=down=1.53 base=1.42 up=1.46 |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.71) | sharpe=0.14  bars=15184 |
| mid (annualised vol 0.71-0.88) | sharpe=0.63  bars=15184 |
| high (annualised vol 0.88-2.23) | sharpe=1.22  bars=15184 |
| basis: series | walk_forward oos_returns, the series time_split_sharpes splits |
| basis: state | annualised std, over vol_window_days, of benchmark_returns: equal-weight, zero cost, over the symbols the book could hold at each bar (pit members; every panel symbol if static) |
| basis: vol_window_days | 30 |
| basis: label_on_bar_t_reads | benchmark bars through t-1: what was known when bar t's position was decided |
| basis: cut_points | this sample's own terciles: the state is ex-ante, the cut points are not.  Volatility trends over years, so a tercile is partly a calendar period: read it against time_split_sharpes |

## Grid (full-sample Sharpe per configuration)

- lookback_days=30, threshold_per_day=0.0003: sharpe=1.42
- lookback_days=30, threshold_per_day=0.0: sharpe=0.97
- lookback_days=7, threshold_per_day=0.0003: sharpe=0.94
- lookback_days=7, threshold_per_day=0.0: sharpe=0.02

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 1.4213 |
| x1.5 | 1.1562 |
| x2 | 0.8918 |
| break-even multiple m* (never enforced) | full_sample=3.74  oos=2.11  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip4 | 1.4213 |
| slip8.86 | 1.2857 |
| slip11 | 1.2259 |
| slip18.4 | 1.0196 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 1.5345 |

## Verdict

| key | value |
| --- | --- |
| verdict | FAIL |
| reasons | oos_sharpe 0.68 < the deflated threshold 1.03 at 4 trials, p_family=0.2511, cpcv fraction_negative 0.33 > 0.1 |
