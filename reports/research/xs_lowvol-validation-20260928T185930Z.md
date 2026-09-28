# Validation: xs_lowvol — FAIL

## Range

| key | value |
| --- | --- |
| start | 2021-01-31 01:00:00+00:00 |
| end | 2026-09-28 16:00:00+00:00 |
| bars | 49600 |

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
| window | 720 |
| entry_threshold | 0.3000 |
| min_symbols | 3 |

## Full sample

| key | value |
| --- | --- |
| bars | 49600 |
| gross_return | 1.0932 |
| net_return | 0.4488 |
| annualized_sharpe | 0.4734 |
| annualized_sharpe_gross | 0.8598 |
| max_drawdown | -0.3492 |
| average_absolute_exposure | 0.7357 |
| turnover_units | 121.5 |
| nonzero_target_bars | 49600 |
| hit_rate | 0.5109 |
| cost_share_of_gross | 0.4493 |
| skew | -0.3470 |
| kurtosis | 22.7105 |
| sortino | 0.6575 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=0; daily_loss_pause_bars=0; bars=49600; min_margin_buffer=144.9; liquidation_touches=0 |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 0.5352 |
| oos_windows | 63 |
| oos_window_sharpe_q10 | -4.6750 |
| oos_return | 0.4814 |
| oos_max_drawdown | -0.3492 |
| oos_bars | 45600 |
| oos_t_stat | 1.1837 |
| oos_t_lags | 15 |
| fold_sharpes | -0.9715, 2.1290, 0.8342, 1.9424, -1.0092 |
| fold_consistency | 0.6000 |
| selection_consistent | yes |
| oos_is_full_sample_tail | yes |
| best_key_oos_sharpe | 0.5352 |
| selection_exercised | NO fold had a choice to make (single configuration, or every fold picked the same one), so the OOS Sharpe above is the TAIL OF ONE FULL-SAMPLE SERIES, not a selection's out-of-sample record.  D-043 caps such a report at WEAK_PASS: the number stands, the claim that a selection survived out of sample does not.  `registry.py` still admits WEAK_PASS to live use. |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 0.4458 |
| oos_sharpe_q05 | -0.7768 |
| oos_sharpe_min | -1.1014 |
| fraction_negative | 0.2667 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 4 |
| grid_trials | 4 |
| grid_effective_trials | 2.0000 |
| prior_trials | 0 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 0.4734 |
| expected_max_sharpe_annual | 0.2762 |
| dsr | 0.6804 |
| dsr_p_value | 0.3196 |
| pbo | 0.0516 |
| pbo_combinations | 5000 |
| degradation_slope | -0.9900 |
| prob_oos_loss | 0.1686 |
| noise_null | sharpe_variance_period=0.0000; expected_max_sharpe_annual=0.4426; dsr_p_value=0.4708 |
| ledger_trials | 0 |
| pbo_is_informative | enforced: grid_trials=4 >= 4 |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 45600 |
| n_trials | 4 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 0.9802 |
| expected_max_annual | 0.4616 |
| p_family | 0.3762 |
| oos_sharpe_annual | 0.5352 |
| caliber | ENFORCED at N=4 (the per-strategy bucket): threshold 0.98, margin -0.44.  REPORTED and never enforced at N=21648 (the whole library): threshold 2.01, margin -1.47, p_family 1.0000.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4387 |
| gate (max of the two halves) | 1.0000  [pass_line binds] |
|   D-028 selection threshold | 0.9802 at N=4, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 50.0% |
| P(clear | true annual Sharpe = 1.2) | 67.6% |
| P(clear | true annual Sharpe = 1.5) | 87.3% |
| P(clear | true annual Sharpe = 2.0) | 98.9% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 45600 |
| n_trials | 21648 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 2.0077 |
| expected_max_annual | 1.7750 |
| p_family | 1.0000 |
| oos_sharpe_annual | 0.5352 |
| power | n_trials=21648; alpha=0.0500; se_annual=0.4387; pass_line_annual=1.0000; selection_threshold_annual=2.0077; gate_annual=2.0077; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0108, true_sharpe_annual=1.2000; power=0.0328, true_sharpe_annual=1.5000; power=0.1236, true_sharpe_annual=2.0000; power=0.4930; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=0.54 threshold=0.98 margin=-0.44 clears=0.00 |
| x1.5 | oos=0.49 threshold=0.98 margin=-0.49 clears=0.00 |
| x2 | oos=0.45 threshold=0.98 margin=-0.53 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip2 | oos=0.54 threshold=0.98 margin=-0.44 clears=0.00 |
| slip4.43 | oos=0.50 threshold=0.98 margin=-0.48 clears=0.00 |
| slip5.5 | oos=0.49 threshold=0.98 margin=-0.49 clears=0.00 |
| slip9.2 | oos=0.44 threshold=0.98 margin=-0.54 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | -0.1974, 1.6246, 1.2188, -0.4241 |
| worst_neighbour_degradation | 0.2302 |
| parameter_neighbourhood | entry_threshold=down=0.36 base=0.47 up=0.47; window=down=0.50 base=0.47 up=0.71 |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.71) | sharpe=0.83  bars=15200 |
| mid (annualised vol 0.71-0.88) | sharpe=0.42  bars=15200 |
| high (annualised vol 0.88-2.23) | sharpe=0.37  bars=15200 |
| basis: series | walk_forward oos_returns, the series time_split_sharpes splits |
| basis: state | annualised std, over vol_window_days, of benchmark_returns: equal-weight, zero cost, over the symbols the book could hold at each bar (pit members; every panel symbol if static) |
| basis: vol_window_days | 30 |
| basis: label_on_bar_t_reads | benchmark bars through t-1: what was known when bar t's position was decided |
| basis: cut_points | this sample's own terciles: the state is ex-ante, the cut points are not.  Volatility trends over years, so a tercile is partly a calendar period: read it against time_split_sharpes |

## Grid (full-sample Sharpe per configuration)

- entry_threshold=0.3, window=720: sharpe=0.47
- entry_threshold=0.2, window=720: sharpe=0.39
- entry_threshold=0.3, window=168: sharpe=0.11
- entry_threshold=0.2, window=168: sharpe=-0.10

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 0.4734 |
| x1.5 | 0.4288 |
| x2 | 0.3841 |
| break-even multiple m* (never enforced) | full_sample=6.30  oos=6.96  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip2 | 0.4734 |
| slip4.43 | 0.4424 |
| slip5.5 | 0.4288 |
| slip9.2 | 0.3816 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 0.4841 |

## Verdict

| key | value |
| --- | --- |
| verdict | FAIL |
| reasons | oos_sharpe 0.54 < the deflated threshold 0.98 at 4 trials, p_family=0.3762, cpcv fraction_negative 0.27 > 0.1 |
